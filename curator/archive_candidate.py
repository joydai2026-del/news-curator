"""Build the bounded payload archived after a successful site deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from .config import Category
from .identity import story_id_for_item
from .models import CoverageMention, Item
from .newsletter.sanitize import sanitize as sanitize_newsletter_url
from .normalize import canonical_url, safe_url
from .rank import has_effective_interest_scores, public_ranking_explanation, score_components
from .render import publishable_cards

SCHEMA_VERSION = 1
MAX_BYTES = 4_000_000
MAX_STORIES = 500
MAX_ENTRIES = 5_000
MAX_MENTIONS = 5_000
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = {
    "schema_version",
    "build_nonce",
    "commit_sha",
    "site_sha256",
    "built_at",
    "stories",
    "aliases",
    "coverage_mentions",
    "topics",
    "entries",
}


def _public_mention(mention: CoverageMention, story_id: str) -> dict | None:
    url = safe_url(mention.url) if mention.url else None
    if url and mention.source_kind == "newsletter":
        url = sanitize_newsletter_url(url)
    if not url or mention.source_kind not in {"outlet", "newsletter"}:
        return None
    if not mention.source_id or not mention.source_name:
        return None
    return {
        "mention_id": mention.mention_id,
        "story_id": story_id,
        "source_kind": mention.source_kind,
        "source_id": mention.source_id,
        "source_name": mention.source_name,
        "url": url,
        "headline": mention.headline[:2_000],
        "mentioned_at": mention.mentioned_at.isoformat(),
    }


def build_archive_candidate(
    ranked: dict[str, list[Item]],
    *,
    categories: Sequence[Category],
    ranking: dict,
    now: datetime,
    build_nonce: str,
    commit_sha: str,
    site_sha256: str,
    require_summaries: bool,
    interest_scores: Mapping[str, float] | None = None,
    coverage_mentions: Sequence[CoverageMention] = (),
) -> dict:
    """Serialize the exact rows accepted by the public renderer's final gate."""
    cards = publishable_cards(ranked, require_summaries=require_summaries)
    story_ids = {story_id_for_item(card.item) for card in cards}
    by_id = {story_id_for_item(card.item): card for card in cards}
    topic_by_name = {category.name: category for category in categories}

    mentions: dict[str, dict] = {}
    for story_id, card in by_id.items():
        for mention in card.item.coverage_mentions:
            row = _public_mention(mention, story_id)
            if row:
                mentions[row["mention_id"]] = row
    for mention in coverage_mentions:
        target_story_id = mention.story_id
        if target_story_id in by_id:
            row = _public_mention(mention, target_story_id)
            if row:
                mentions[row["mention_id"]] = row

    counts: dict[str, int] = {story_id: 0 for story_id in story_ids}
    sources: dict[str, set[tuple[str, str]]] = {story_id: set() for story_id in story_ids}
    for row in mentions.values():
        sources[row["story_id"]].add((row["source_kind"], row["source_id"]))
    for story_id in counts:
        counts[story_id] = len(sources[story_id])

    stories = []
    aliases = []
    for card in cards:
        item = card.item
        story_id = story_id_for_item(item)
        canonical = canonical_url(item.canonical_url or item.url)
        canonical = safe_url(canonical) if canonical else None
        if canonical is None:
            canonical = ""
        stories.append(
            {
                "story_id": story_id,
                "canonical_url": canonical,
                "title": item.title[:2_000],
                "summary": card.description[:8_000],
                "language": item.language,
                "published_at": item.published_at.isoformat(),
                "source_name": (item.newsletter_sender or item.source_name)[:200],
                "source_kind": "newsletter" if item.is_newsletter else "outlet",
                "distinct_coverage_source_count": counts[story_id],
            }
        )
        if canonical:
            aliases.append(
                {"normalized_url": canonical, "story_id": story_id, "match_method": "exact"}
            )

    topics = []
    entries = []
    for topic_name, items in ranked.items():
        topic = topic_by_name.get(topic_name) or Category(name=topic_name)
        preference_mode = has_effective_interest_scores(items, interest_scores)
        topics.append({"topic_id": topic.id, "name": topic.name})
        for position, item in enumerate(items, start=1):
            story_id = story_id_for_item(item)
            if story_id not in story_ids:
                continue
            components = score_components(
                item, topic, now, ranking, interest_score=0.0
            )
            components.pop("interest", None)
            components["final_score"] = sum(
                value for key, value in components.items() if key != "final_score"
            )
            if preference_mode:
                ordering_mode = "preference_then_freshness"
                ordering_key = {"published_at": item.published_at.timestamp()}
            elif topic.id == "trending" and item.native_rank is not None:
                ordering_mode = "native_rank_then_freshness"
                ordering_key = {
                    "native_rank": float(item.native_rank),
                    "published_at": item.published_at.timestamp(),
                }
            else:
                ordering_mode = "weighted_total"
                ordering_key = {"weighted_total": components["final_score"]}
            entries.append(
                {
                    "story_id": story_id,
                    "topic_id": topic.id,
                    "position": position,
                    "score_components": components,
                    "ordering_mode": ordering_mode,
                    "ordering_key": ordering_key,
                    "source_name": (item.newsletter_sender or item.source_name)[:200],
                    "source_kind": "newsletter" if item.is_newsletter else "outlet",
                    "ranking_explanation": public_ranking_explanation(
                        ordering_mode, components
                    ),
                }
            )

    topic_ranks: dict[str, dict[str, int]] = {}
    for entry in entries:
        topic_ranks.setdefault(entry["story_id"], {})[entry["topic_id"]] = entry["position"]
    for entry in entries:
        entry["topic_ranks"] = topic_ranks[entry["story_id"]]

    candidate = {
        "schema_version": SCHEMA_VERSION,
        "build_nonce": build_nonce,
        "commit_sha": commit_sha,
        "site_sha256": site_sha256,
        "built_at": now.isoformat(),
        "stories": stories,
        "aliases": aliases,
        "coverage_mentions": list(mentions.values()),
        "topics": topics,
        "entries": entries,
    }
    validate_archive_candidate(candidate)
    return candidate


def validate_archive_candidate(candidate: dict) -> None:
    if not isinstance(candidate, dict) or set(candidate) != _TOP_KEYS:
        raise ValueError("archive candidate schema is invalid")
    if candidate["schema_version"] != SCHEMA_VERSION:
        raise ValueError("archive candidate version is invalid")
    if not isinstance(candidate["build_nonce"], str) or not candidate["build_nonce"]:
        raise ValueError("archive candidate build_nonce is invalid")
    if not _SHA.fullmatch(str(candidate["commit_sha"])):
        raise ValueError("archive candidate commit_sha is invalid")
    built_at = datetime.fromisoformat(str(candidate["built_at"]))
    if built_at.tzinfo is None:
        raise ValueError("archive candidate built_at is invalid")
    if not _SHA256.fullmatch(str(candidate["site_sha256"])):
        raise ValueError("archive candidate site_sha256 is invalid")
    for key, maximum in (
        ("stories", MAX_STORIES),
        ("entries", MAX_ENTRIES),
        ("coverage_mentions", MAX_MENTIONS),
    ):
        if not isinstance(candidate[key], list) or len(candidate[key]) > maximum:
            raise ValueError(f"archive candidate {key} is invalid")
    for story in candidate["stories"]:
        if (
            not isinstance(story, dict)
            or story.get("source_kind") not in {"outlet", "newsletter"}
            or not isinstance(story.get("source_name"), str)
            or not story["source_name"]
            or len(story["source_name"]) > 200
            or len(story["source_name"].encode("utf-8")) > 1_000
        ):
            raise ValueError("archive candidate story provenance is invalid")
    for entry in candidate["entries"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("score_components"), dict)
            or not isinstance(entry.get("ordering_key"), dict)
            or "interest" in entry["score_components"]
            or "preference_score" in entry["ordering_key"]
            or entry.get("source_kind") not in {"outlet", "newsletter"}
            or not isinstance(entry.get("source_name"), str)
            or not entry["source_name"]
            or len(entry["source_name"]) > 200
            or len(entry["source_name"].encode("utf-8")) > 1_000
            or not isinstance(entry.get("ranking_explanation"), str)
            or not entry["ranking_explanation"]
            or len(entry["ranking_explanation"].encode("utf-8")) > 2_000
            or not isinstance(entry.get("topic_ranks"), dict)
            or len(entry["topic_ranks"]) > 100
            or any(
                not isinstance(topic, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", topic)
                or not isinstance(position, int)
                or isinstance(position, bool)
                or position < 1
                for topic, position in entry["topic_ranks"].items()
            )
        ):
            raise ValueError("archive candidate ranking metadata is not public-safe")
    encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("archive candidate is too large")


def write_archive_candidate(path: Path, candidate: dict) -> None:
    validate_archive_candidate(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stamp_archive_candidate_site(candidate_path: Path, site_index_path: Path) -> None:
    """Atomically bind an existing candidate to the final rendered page bytes."""

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    validate_archive_candidate(candidate)
    site_bytes = site_index_path.read_bytes()
    candidate["site_sha256"] = hashlib.sha256(site_bytes).hexdigest()
    validate_archive_candidate(candidate)
    encoded = json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=candidate_path.parent,
        prefix=f".{candidate_path.name}.", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
    temporary.replace(candidate_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a bounded archive candidate.")
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--stamp-site", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.stamp_site is not None:
        stamp_archive_candidate_site(args.candidate, args.stamp_site)
        return 0
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    validate_archive_candidate(candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
