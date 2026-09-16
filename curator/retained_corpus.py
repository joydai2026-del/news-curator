"""Canonical retained candidates, independent of a rendered edition."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .config import Category
from .dedup import dedupe
from .filter import topic_match
from .grouping import GroupingCandidate, GroupingPolicy, assign_event_groups
from .identity import story_id_for_item
from .models import Item
from .normalize import fold_text


@dataclass(frozen=True)
class RetainedCandidate:
    story_id: str
    item: Item
    observed_at: datetime
    category_ids: frozenset[str]
    # Tier-1 cross-language grouping, computed at ingest. None means the story
    # joined no group, which makes it a group of one by construction.
    event_group_id: str | None = None
    # Translated title/summary keyed by language. Empty is the normal state for
    # a story nobody needs translated.
    title_translations: Mapping[str, str] = field(default_factory=dict)
    summary_translations: Mapping[str, str] = field(default_factory=dict)


def retain(items: Iterable[Item], *, categories: Iterable[Category], observed_at: datetime,
           grouping: GroupingPolicy | None = None) -> tuple[RetainedCandidate, ...]:
    """Normalize through the existing deduper, then keep every canonical item.

    This deliberately runs before publication selection. A category assignment is
    recomputed from configured terms so retained search and feed candidates use
    the same inclusion rule as collection.
    """
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    configured = tuple(categories)
    retained: dict[str, RetainedCandidate] = {}
    for item in dedupe(list(items)):
        ids = frozenset(c.id for c in configured if topic_match(item, c) is not None)
        story_id = story_id_for_item(item)
        if story_id in retained:
            # M1 retains language variants. The corpus has one durable row per
            # URL identity; coalesce here without changing M1 display behavior.
            retained[story_id] = replace(retained[story_id],
                category_ids=retained[story_id].category_ids | ids)
        else:
            retained[story_id] = RetainedCandidate(
                story_id=story_id, item=item,
                observed_at=observed_at.astimezone(timezone.utc), category_ids=ids)
    groups = assign_event_groups(
        [GroupingCandidate(story_id=row.story_id, language=row.item.language, title=row.item.title,
                           summary=row.item.description or "", published_at=row.item.published_at)
         for row in retained.values()],
        policy=grouping or GroupingPolicy(),
    )
    for story_id, group_id in groups.items():
        retained[story_id] = replace(retained[story_id], event_group_id=group_id)
    return tuple(sorted(retained.values(), key=lambda row: (row.item.published_at, row.story_id), reverse=True))


def regroup_with_corpus(rows, existing, *, policy: GroupingPolicy | None = None):
    """Group this batch against rows ALREADY in the corpus, not only itself.

    The ingest runs about twelve times an hour, so an English story from an
    earlier run must still be able to join this run's Chinese story. Without
    this, a covered story looks language exclusive, which is the reported bug
    inverted. An id a row already carries is never downgraded.
    """

    rows = list(rows)
    batch_ids = {row.story_id for row in rows}
    candidates = [GroupingCandidate(story_id=row.story_id, language=row.item.language,
                                    title=row.item.title, summary=row.item.description or "",
                                    published_at=row.item.published_at) for row in rows]
    candidates.extend(row for row in existing if row.story_id not in batch_ids)
    groups = assign_event_groups(candidates, policy=policy or GroupingPolicy())
    return tuple(
        replace(row, event_group_id=row.event_group_id or groups.get(row.story_id))
        if groups.get(row.story_id) or row.event_group_id else row
        for row in rows
    )


def language_exclusive_story_ids(rows: Iterable[RetainedCandidate], *, display_language: str,
                                 corpus: Iterable[GroupingCandidate] = ()) -> tuple[str, ...]:
    """Stories no outlet in the reader's display language carried.

    Exclusivity is stated against the reader's display language, never against
    "is Chinese", so the mirror direction is a config flip and not a rewrite.
    """

    rows = list(rows)
    covered = {row.event_group_id for row in rows
               if row.event_group_id and row.item.language == display_language}
    # A story the display language already covered may live in an EARLIER batch.
    covered |= {row.event_group_id for row in corpus
                if getattr(row, "event_group_id", None) and row.language == display_language}
    return tuple(row.story_id for row in rows
                 if row.item.language != display_language
                 and (row.event_group_id is None or row.event_group_id not in covered))


def apply_translations(rows: Iterable[RetainedCandidate], overlays: Mapping[str, object]) -> tuple[RetainedCandidate, ...]:
    """Attach ingest translations. A story without one is never removed."""

    result = []
    for row in rows:
        overlay = overlays.get(row.story_id)
        if overlay is None:
            result.append(row)
            continue
        result.append(replace(row,
            title_translations=dict(getattr(overlay, "title_translations", {})),
            summary_translations=dict(getattr(overlay, "summary_translations", {}))))
    return tuple(result)


def candidates(rows: Iterable[RetainedCandidate], *, category_id: str | None = None, query: str | None = None) -> tuple[RetainedCandidate, ...]:
    """Return retained main/category/search candidates without a page-size cap.

    Search uses Unicode folded substring matching. This keeps configured Chinese
    text searchable without pretending an English tokenizer understands CJK.
    Ranking and pagination are separate request-time responsibilities.
    """
    query_folded = fold_text(query or "").casefold()
    result = []
    for row in rows:
        if category_id is not None and category_id not in row.category_ids:
            continue
        haystack = fold_text(f"{row.item.title}\n{row.item.description}").casefold()
        if query_folded and query_folded not in haystack:
            continue
        result.append(row)
    return tuple(result)


def candidate_response(rows: Iterable[RetainedCandidate]) -> dict[str, object]:
    """Versioned adapter payload for the new M2 service, never an M1 card."""
    return {
        "schema_version": 1,
        "candidates": [
            {
                "story_id": row.story_id,
                "title": row.item.title,
                "summary": row.item.description,
                "language": row.item.language,
                "canonical_url": row.item.canonical_url,
                "source_id": row.item.source_id,
                "source_name": row.item.source_name,
                "published_at": row.item.published_at.astimezone(timezone.utc).isoformat(),
                "source_observed_at": row.observed_at.isoformat(),
                "category_ids": sorted(row.category_ids),
                "event_group_id": row.event_group_id,
                "title_translations": dict(row.title_translations),
                "summary_translations": dict(row.summary_translations),
            }
            for row in rows
        ],
    }


def public_ingest_rows(rows: Iterable[RetainedCandidate], *, allowed_source_ids: set[str]) -> list[dict[str, object]]:
    """The only artifact shape accepted by the public-only ingest RPC."""
    result = []
    for row in rows:
        if row.item.is_newsletter:
            raise ValueError("newsletter items cannot enter retained corpus")
        if row.item.source_id not in allowed_source_ids:
            raise ValueError("item is not an eligible public source")
        if not isinstance(row.item.is_aggregator, bool):
            raise ValueError("item aggregator attribution must be boolean")
        payload = {
            "story_id": row.story_id, "origin_class": "public_outlet", "source_kind": "outlet", "canonical_url": row.item.canonical_url,
            "title": row.item.title, "summary": row.item.description,
            "language": row.item.language, "source_id": row.item.source_id,
            "source_name": row.item.source_name,
            "source_is_aggregator": row.item.is_aggregator,
            "published_at": row.item.published_at.astimezone(timezone.utc).isoformat(),
            "source_observed_at": row.observed_at.isoformat(),
            "category_ids": sorted(row.category_ids),
        }
        if row.event_group_id:
            payload["event_group_id"] = row.event_group_id
        if row.title_translations:
            payload["title_translations"] = dict(row.title_translations)
        if row.summary_translations:
            payload["summary_translations"] = dict(row.summary_translations)
        result.append(payload)
    return result
