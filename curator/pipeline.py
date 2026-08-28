"""Wire the stages together and write the site. This is the entry point.

    fetch -> age filter -> dedupe -> keyword filter -> rank -> render

Failure policy, corrected after review: an individual tier failing is logged,
noted on the page, and the run continues. The publish guard runs on the number
of rows that will actually be VISIBLE, not on the number of items fetched. The
earlier version passed the guard whenever any tier returned anything, which
meant one irrelevant successful fetch could overwrite a good page with an empty
one.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Category, Config, ConfigError, load_config
from .dedup import dedupe
from .filter import assign_categories
from .images import ImageCache, enrich
from .models import Item, TierResult
from .rank import rank_items
from .render import render_site

log = logging.getLogger("curator")

IMAGE_CACHE_FILE = "image_cache.json"

# The newsletter lane's pseudo-category. Constructed here, never in
# topics.yaml: config validation rightly rejects a category with no keywords
# and no feeds, but this one matches by the `newsletters` native tag that the
# lane stamps on its items, so it can never be empty while the lane is lit.
NEWSLETTER_CATEGORY_ID = "newsletters"
NEWSLETTER_CATEGORY_NAME = "Newsletters"


def collect(cfg: Config, *, offline: bool = False) -> list[TierResult]:
    if offline:
        return [TierResult(tier="offline", items=[], ok=True, note="offline mode, no network")]

    from .fetchers import hn, reddit, rss

    results: list[TierResult] = []
    for name, call in (
        ("hackernews", lambda: hn.fetch(cfg, cfg.topics)),
        ("rss", lambda: rss.fetch(cfg)),
        ("reddit", lambda: reddit.fetch(cfg)),
    ):
        try:
            results.append(call())
        except Exception:
            # A fetcher raising is a bug in the fetcher, not a reason to lose
            # the other two tiers. Detail to the log, generic note to the page.
            log.exception("tier %s raised", name)
            results.append(TierResult(tier=name, ok=False, note="unavailable this run"))
    return results


def load_newsletter_artifact(path: Path) -> tuple[list[Item], TierResult, dict]:
    """The fetch job's artifact, as items + a health line + advance() metadata.

    The artifact was produced by `python -m curator.newsletter` in a separate,
    secrets-scoped job (see that module's docstring for why). Everything in it
    is already sanitized; this function's job is reconstruction, not trust:
    every URL still passes through the same output-boundary validation as any
    other item once it reaches the renderer.

    Newsletter items are built `is_aggregator=True` on purpose: TLDR and
    friends write their OWN headline for someone else's article, which is
    exactly the case that flag documents. When the publisher's feed carried
    the same link, the publisher's title wins the merge and the newsletter
    copy rides along as provenance, not as the display row.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    items: list[Item] = []
    for record in raw.get("items", []):
        try:
            published = datetime.fromisoformat(record["published_at"])
        except (KeyError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        items.append(
            Item(
                title=str(record.get("title") or ""),
                url=str(record.get("url") or ""),
                canonical_url=str(record.get("canonical_url") or ""),
                source_id=str(record.get("source_id") or "newsletter"),
                source_name=str(record.get("source_name") or "Newsletter"),
                platform=str(record.get("platform") or "newsletter"),
                published_at=published,
                description=str(record.get("description") or ""),
                is_newsletter=True,
                is_aggregator=True,
                newsletter_sender=str(record.get("newsletter_sender") or ""),
                image_url="",  # PRIVACY RULE: never an image, whatever the artifact says
                native_categories={NEWSLETTER_CATEGORY_ID},
            )
        )

    dark = bool(raw.get("dark", True))
    ok = bool(raw.get("ok", False)) and not dark
    status = raw.get("status") or {}
    # Per-sender hit rates always go to the LOG (and live in the artifact);
    # the page's health note carries only PROBLEMS, because `TierResult`
    # renders any note as "degraded" and a clean run reporting "tldr 3/3"
    # must not read as one. An allowlisted sender whose adapter extracted
    # nothing IS a problem, and gets its hit rate as context.
    for adapter_id, s in sorted(status.items()):
        if s.get("seen"):
            log.info("newsletter adapter %-12s %d/%d stories, %d links dropped",
                     adapter_id, s.get("extracted", 0), s.get("seen", 0),
                     s.get("dropped_links", 0))
    bits = []
    pending = sorted(a for a, s in status.items() if s.get("state") == "pending")
    if pending:
        bits.append(f"pending adapters: {', '.join(pending)}")
    if raw.get("unmatched_messages"):
        bits.append(f"{raw['unmatched_messages']} messages from senders without an adapter")
    # The fetch job's "what this run did NOT see" counters. A short batch and
    # an authentication rejection are exactly the states that must not hide
    # behind a healthy item count.
    if raw.get("truncated"):
        # Bodies are read oldest-first and the watermark stops at the newest
        # processed message, so the remainder is a backlog that drains, never
        # a tail that is skipped. The wording says what happens.
        bits.append("short batch; backlog remains, read next run")
    if raw.get("unreadable_messages"):
        bits.append(f"{raw['unreadable_messages']} messages unreadable")
    rejected = int(raw.get("unauthenticated_messages") or 0) + int(raw.get("unauthenticated_missing") or 0)
    if rejected:
        bits.append(f"{rejected} messages failed sender authentication")
    note = str(raw.get("note") or "") if dark else "; ".join(bits)
    tier = TierResult(tier="newsletters", items=items, ok=ok, note=note)

    meta = {
        "dark": dark,
        "ok": bool(raw.get("ok", False)),
        "watermark": raw.get("watermark"),
        "hashes": list(raw.get("hashes") or []),
    }
    return items, tier, meta


def build(
    cfg: Config,
    results: list[TierResult],
    now: datetime,
    *,
    newsletter_on: bool = False,
) -> dict[str, list[Item]]:
    raw: list[Item] = [i for r in results for i in r.items]

    categories = list(cfg.categories)
    if newsletter_on:
        # The tab is present whenever the lane is lit, even on a quiet window.
        # A dark lane adds no tab at all, which is the "no empty tab" rule.
        categories.append(Category(name=NEWSLETTER_CATEGORY_NAME, id=NEWSLETTER_CATEGORY_ID))

    cutoff = now - timedelta(hours=cfg.max_age_hours)
    fresh = [i for i in raw if i.published_at >= cutoff]
    log.info("collected %d items, %d within %sh", len(raw), len(fresh), cfg.max_age_hours)

    # Dedupe BEFORE topic assignment so cross-source echo counts are computed
    # once, globally, rather than recomputed per topic.
    deduped = dedupe(
        fresh,
        threshold=float(cfg.dedup.get("title_similarity_threshold", 0.85)),
        time_bucket_hours=float(cfg.dedup.get("time_bucket_hours", 36)),
    )
    log.info("deduped to %d unique items", len(deduped))

    buckets = assign_categories(deduped, categories)
    newsletter_cap = int(cfg.newsletter.get("max_items", 50) or 50)
    ranked: dict[str, list[Item]] = {}
    for category in categories:
        items = rank_items(buckets[category.name], category, now, cfg.ranking)
        # The lane has its own cap and no effect on category caps.
        cap = newsletter_cap if category.id == NEWSLETTER_CATEGORY_ID else cfg.max_items_per_topic
        ranked[category.name] = items[:cap]
        native = sum(1 for i in ranked[category.name] if category.id in i.native_categories)
        log.info(
            "category %-22s %3d items (%d from its own feeds)",
            category.name, len(ranked[category.name]), native,
        )
    return ranked


def _default_repo_url(cfg: Config) -> str | None:
    """Config first, then the Actions environment. Never a hardcoded owner.

    A fork must not advertise the upstream repo in its own footer.
    """
    if cfg.repo_url:
        return cfg.repo_url
    server = os.environ.get("GITHUB_SERVER_URL")
    slug = os.environ.get("GITHUB_REPOSITORY")
    return f"{server}/{slug}" if server and slug else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="curator", description="Build the news page.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="where topics.yaml lives")
    parser.add_argument("--out", type=Path, default=None, help="output dir (default: <root>/site)")
    parser.add_argument("--offline", action="store_true", help="skip all network calls")
    parser.add_argument("--site-name", default=None)
    parser.add_argument(
        "--image-cache",
        type=Path,
        default=None,
        help=f"preview-image cache file (default: <root>/{IMAGE_CACHE_FILE})",
    )
    parser.add_argument(
        "--newsletter-artifact",
        type=Path,
        default=None,
        help="JSON artifact written by `python -m curator.newsletter` in the "
        "secrets-scoped fetch job; absent means the lane is dark this run",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="write the page even if no story matched (used by the render smoke test)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    try:
        cfg = load_config(args.root)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    now = datetime.now(timezone.utc)
    results = collect(cfg, offline=args.offline)

    # The newsletter lane arrives pre-fetched as an artifact from its own
    # secrets-scoped job. A missing or unreadable artifact is a dark lane and
    # a note in the log, never a failed build of the six healthy tabs.
    newsletter_meta: dict = {"dark": True, "ok": False}
    if args.newsletter_artifact and args.newsletter_artifact.is_file():
        try:
            nl_items, nl_tier, newsletter_meta = load_newsletter_artifact(args.newsletter_artifact)
        except (ValueError, OSError):
            log.warning("newsletter artifact unreadable; lane dark this run")
        else:
            results.append(nl_tier)
            log.info("newsletter lane: %d items (%s)", len(nl_items), nl_tier.note or "ok")
    elif args.newsletter_artifact:
        log.warning("newsletter artifact %s missing; lane dark this run", args.newsletter_artifact)

    ranked = build(cfg, results, now, newsletter_on=not newsletter_meta.get("dark", True))
    visible = sum(len(v) for v in ranked.values())

    # The guard is on VISIBLE rows, and it runs after filtering, because that is
    # the only number that describes what a reader would actually get. It is not
    # conditioned on topics being configured either: an empty topics list still
    # produces an empty page, and that page would still overwrite a good one.
    if visible == 0 and not args.allow_empty:
        log.error(
            "no story matched any topic. Refusing to overwrite the published page "
            "with an empty one. Re-run with --allow-empty to override."
        )
        return 1

    # Preview images are resolved AFTER ranking and truncation, so the only
    # article heads fetched are the ones a reader will actually see. That is
    # what keeps an hourly job bounded: the ceiling is the number of visible
    # rows, not the number of headlines collected.
    cache_path = args.image_cache or (args.root / IMAGE_CACHE_FILE)
    cache = ImageCache.load(cache_path)
    # Newsletter items are excluded here AND refused inside enrich(): the
    # privacy rule (no article fetch, no cache entry for newsletter-derived
    # URLs) should survive either guard being refactored away.
    stats = enrich(
        [i for rows in ranked.values() for i in rows if not i.is_newsletter],
        cache,
        now,
        user_agent=cfg.user_agent,
        # `--offline` means no network, and that has to include this. Feed-borne
        # images and cache hits still apply, because neither touches the wire.
        config={**cfg.images, "enabled": False} if args.offline else cfg.images,
    )
    with_image = sum(1 for rows in ranked.values() for i in rows if i.image_url)
    log.info(
        "images: %d/%d rows have one (%d from feeds, %d cached, %d fetched, "
        "%d declare none, %d unavailable)",
        with_image, stats["total"], stats["from_feed"], stats["from_cache"],
        stats["fetched"], stats["no_image"], stats["errors"],
    )
    # Hitting the cap or the budget is a degraded run, not a quiet one, and it
    # has to be visible the way the Hacker News tier already makes its own cap
    # visible. Otherwise a run that silently abandoned 40 lookups reads exactly
    # like a run that had nothing to do.
    if stats["capped"] or stats["budget_hit"]:
        log.warning(
            "images: %d lookups deferred by the per-run cap, %d by the time budget; "
            "they resolve on a later run",
            stats["capped"], stats["budget_hit"],
        )
    # `or 45` would have turned an explicit `retain_days: 0` into 45, silently
    # ignoring the one value a person would set to mean "keep nothing".
    retain = cfg.images.get("retain_days")
    cache.prune(now, retain_days=float(45 if retain is None else retain))
    if cache.save():
        log.info("image cache written to %s (%d entries)", cache_path, len(cache.entries))

    out_dir = args.out or (args.root / "site")
    path = render_site(
        ranked,
        results,
        now,
        out_dir,
        site_name=args.site_name or cfg.site_name,
        repo_url=_default_repo_url(cfg),
        cname_source=args.root / "CNAME",
    )
    log.info("wrote %s (%d rows across %d topics)", path, visible, len(ranked))

    # The cursor moves ONLY here, after the page is on disk and the publish
    # guard passed. A run that fetched mail and then died re-reads that mail
    # next hour (the overlap window and the salted hashes make that harmless);
    # a run that published moves the watermark so mail is never re-shown.
    if newsletter_meta.get("ok") and not newsletter_meta.get("dark") and newsletter_meta.get("watermark"):
        from .newsletter import state as newsletter_state

        try:
            watermark = datetime.fromisoformat(str(newsletter_meta["watermark"]))
            state_path = args.root / newsletter_state.STATE_FILENAME
            st = newsletter_state.load(state_path, now=now)
            newsletter_state.advance(
                state_path, st,
                watermark=watermark,
                new_hashes=list(newsletter_meta.get("hashes") or []),
            )
            log.info("newsletter cursor advanced to %s", watermark.isoformat())
        except (ValueError, OSError) as exc:
            # A cursor that failed to advance means one hour of re-read mail,
            # not lost mail. Say so and finish the run as a success.
            log.warning("newsletter cursor not advanced (%s)", type(exc).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
