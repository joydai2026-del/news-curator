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
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Category, Config, ConfigError, RssSource, load_config
from .dedup import dedupe
from .filter import assign_categories
from .images import ImageCache, enrich
from .models import Item, TierResult
from .newsletter.sanitize import sanitize as sanitize_newsletter_url
from .normalize import canonical_url as normalize_canonical_url
from .normalize import fold_text
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


def _newsletter_fallback_canonical(title: str) -> str:
    """Generate a non-link identity without trusting an artifact value."""

    digest = hashlib.sha256(fold_text(title).encode("utf-8")).hexdigest()[:16]
    return f"newsletter:{digest}"


def _feed_source_row(source: RssSource, cfg: Config) -> dict:
    """Translate the legacy config object into the stable adapter contract."""

    return {
        "type": source.type,
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "enabled": source.enabled,
        "language": source.language,
        "category": source.category,
        "max_age_hours": source.max_age_hours or cfg.default_source_max_age_hours,
        "weight": source.weight,
        "is_aggregator": source.is_aggregator,
        "platform": source.platform,
        "echo_eligible": source.echo_eligible,
        "request_timeout_seconds": source.request_timeout_seconds or cfg.timeout,
        "max_response_bytes": source.max_response_bytes or cfg.default_source_max_response_bytes,
        "per_host_concurrency": source.per_host_concurrency or cfg.default_source_per_host_concurrency,
        "options": dict(source.options),
    }


def _hackernews_source_row(cfg: Config) -> dict | None:
    block = dict(cfg.hackernews)
    if not block.get("enabled", True):
        return None
    common = {
        "type", "id", "name", "url", "enabled", "language", "category",
        "max_age_hours", "weight", "aggregator", "is_aggregator", "platform",
        "echo_eligible", "request_timeout_seconds", "max_response_bytes",
        "per_host_concurrency",
    }
    options = {key: value for key, value in block.items() if key not in common}
    return {
        "type": "hackernews",
        "id": str(block.get("id") or "hackernews"),
        "name": str(block.get("name") or "Hacker News"),
        "url": str(block.get("url") or "https://hn.algolia.com/api/v1"),
        "enabled": True,
        "language": str(block.get("language") or "en"),
        "category": str(block.get("category") or ""),
        "max_age_hours": block.get("max_age_hours", cfg.default_source_max_age_hours),
        "weight": block.get("weight", 0.95),
        "is_aggregator": block.get("is_aggregator", block.get("aggregator", True)),
        "platform": str(block.get("platform") or "hackernews"),
        "echo_eligible": block.get("echo_eligible", True),
        "request_timeout_seconds": block.get("request_timeout_seconds", cfg.timeout),
        "max_response_bytes": block.get("max_response_bytes", cfg.default_source_max_response_bytes),
        "per_host_concurrency": block.get("per_host_concurrency", cfg.default_source_per_host_concurrency),
        "options": options,
    }


def configured_source_specs(cfg: Config, registry=None):
    """Return validated source specs in stable config order.

    RSS, Atom, news sitemap, and JSON Feed additions need only one config row.
    A new protocol needs one small allowlisted adapter and no pipeline rewrite.
    """

    from .sources import build_builtin_registry

    selected_registry = registry or build_builtin_registry()
    rows = [_feed_source_row(source, cfg) for source in cfg.all_feeds]
    hackernews = _hackernews_source_row(cfg)
    if hackernews is not None:
        rows.append(hackernews)
    return selected_registry.parse_specs(rows)


def collect(
    cfg: Config,
    *,
    offline: bool = False,
    registry=None,
    transport=None,
    clock=None,
) -> list[TierResult]:
    if offline:
        return [TierResult(tier="offline", items=[], ok=True, note="offline mode, no network")]

    from .sources import (
        SafeHttpPolicy,
        SafeHttpTransport,
        SourceContext,
        SourceQuery,
        build_builtin_registry,
        collect_sources,
    )

    selected_registry = registry or build_builtin_registry()
    selected_transport = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=cfg.timeout,
            max_wire_bytes=cfg.default_source_max_response_bytes,
            max_decoded_bytes=cfg.default_source_max_response_bytes,
            per_host_concurrency=cfg.default_source_per_host_concurrency,
        )
    )
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    context = SourceContext(
        registry=selected_registry,
        transport=selected_transport,
        clock=selected_clock,
        environment=os.environ.get,
        user_agent=cfg.user_agent,
        queries=tuple(
            SourceQuery(category.id, tuple(category.search_terms))
            for category in cfg.categories
            if category.search_terms
        ),
        default_max_age_hours=cfg.default_source_max_age_hours,
    )
    source_results = collect_sources(
        configured_source_specs(cfg, selected_registry),
        context,
        max_workers=cfg.fetch_workers,
    )
    items = [item for result in source_results for item in result.items]
    health = [result.health for result in source_results]
    alerts = [record for record in health if record.status not in {"fresh", "disabled"}]
    unavailable = [record for record in health if record.status in {"unavailable", "malformed"}]
    active = [record for record in health if record.status != "disabled"]
    note = f"{len(alerts)} source alert{'s' if len(alerts) != 1 else ''}" if alerts else ""
    return [
        TierResult(
            tier="sources",
            items=items,
            # A composition with no active sources is intentionally quiet. It
            # is not an unexplained outage and must not render as degraded.
            ok=not active or len(unavailable) < len(active),
            note=note,
            source_health=health,
        )
    ]


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
        # Treat the artifact as untrusted at reconstruction time. The display
        # URL and canonical identity cross separate boundaries: a bad display
        # URL becomes an unlinked headline, while a bad or missing canonical
        # value can be safely rebuilt from a valid display URL. If neither is
        # safe, a recomputed opaque key supports dedup but is never published.
        title = str(record.get("title") or "")
        article_url = sanitize_newsletter_url(str(record.get("url") or "")) or ""
        artifact_canonical_url = sanitize_newsletter_url(
            str(record.get("canonical_url") or "")
        )
        article_canonical_url = (
            normalize_canonical_url(artifact_canonical_url or "")
            or normalize_canonical_url(article_url)
            # An unlinked newsletter story still needs a stable internal key
            # for dedup. Recompute it from public title text rather than
            # accepting an artifact-provided non-HTTP scheme.
            or _newsletter_fallback_canonical(title)
        )
        items.append(
            Item(
                title=title,
                url=article_url,
                canonical_url=article_canonical_url,
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
    return build_ranked_language(
        cfg,
        results,
        now,
        language="en",
        newsletter_on=newsletter_on,
    )


def build_ranked_language(
    cfg: Config,
    results: list[TierResult],
    now: datetime,
    *,
    language: str,
    newsletter_on: bool = False,
) -> dict[str, list[Item]]:
    """Rank authoritative originals within one language boundary."""

    if language not in {"en", "zh"}:
        raise ValueError("language must be 'en' or 'zh'")
    raw: list[Item] = [i for r in results for i in r.items]

    categories = list(cfg.categories)
    if newsletter_on and language == "en":
        # The tab is present whenever the lane is lit, even on a quiet window.
        # A dark lane adds no tab at all, which is the "no empty tab" rule.
        categories.append(Category(name=NEWSLETTER_CATEGORY_NAME, id=NEWSLETTER_CATEGORY_ID))

    cutoff = now - timedelta(hours=cfg.max_age_hours)
    # Partitioning before dedup is load-bearing: a Chinese variant must never
    # erase the English original, or vice versa.
    language_items = [i for i in raw if i.language == language]
    fresh = [i for i in language_items if i.published_at >= cutoff]
    log.info(
        "collected %d items, %d %s items within %sh",
        len(raw),
        len(fresh),
        language,
        cfg.max_age_hours,
    )

    # Dedupe BEFORE topic assignment so cross-source echo counts are computed
    # once within the selected language, rather than recomputed per topic.
    deduped = build_language_view(
        raw,
        language,
        cutoff=cutoff,
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


def build_language_view(
    items: list[Item],
    language: str,
    *,
    cutoff: datetime | None = None,
    threshold: float = 0.90,
    time_bucket_hours: float = 36.0,
) -> list[Item]:
    """Pure backend seam: partition and age-filter before language-local dedup."""
    if language not in {"en", "zh"}:
        raise ValueError("language must be 'en' or 'zh'")
    selected = [item for item in items if item.language == language]
    if cutoff is not None:
        selected = [item for item in selected if item.published_at >= cutoff]
    return dedupe(
        selected,
        threshold=threshold,
        time_bucket_hours=time_bucket_hours,
    )


def _default_repo_url(cfg: Config) -> str | None:
    """Config first, then the Actions environment. Never a hardcoded owner.

    A fork must not advertise the upstream repo in its own footer.
    """
    if cfg.repo_url:
        return cfg.repo_url
    server = os.environ.get("GITHUB_SERVER_URL")
    slug = os.environ.get("GITHUB_REPOSITORY")
    return f"{server}/{slug}" if server and slug else None


def _ranked_originals(*ranked_views: dict[str, list[Item]]) -> list[Item]:
    """Return each bounded ranked original once, across all language views.

    The ranked views are already capped per category. Identity de-duplication
    here prevents one story appearing in multiple categories from consuming
    multiple image lookups, while retaining the actual Item object so every
    localized projection inherits the same publisher-declared image.
    """

    originals: list[Item] = []
    seen: set[int] = set()
    for ranked in ranked_views:
        for rows in ranked.values():
            for item in rows:
                identity = id(item)
                if identity in seen:
                    continue
                seen.add(identity)
                originals.append(item)
    return originals


def _sanitize_newsletter_projection_urls(*localized_views: dict) -> None:
    """Apply the newsletter privacy gate before public output is written."""

    seen: set[int] = set()
    for view in localized_views:
        for rows in view.values():
            for localized in rows:
                original = localized.original
                identity = id(original)
                if identity in seen or not original.is_newsletter:
                    continue
                seen.add(identity)
                original.url = sanitize_newsletter_url(original.url) or ""
                clean_canonical = sanitize_newsletter_url(original.canonical_url)
                original.canonical_url = (
                    normalize_canonical_url(clean_canonical or "")
                    or normalize_canonical_url(original.url)
                    or ""
                )


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
        "--source-snapshot",
        type=Path,
        default=None,
        help="bounded authoritative-original snapshot; when supplied, sources are never fetched again",
    )
    parser.add_argument(
        "--newsletter-artifact",
        type=Path,
        default=None,
        help="JSON artifact written by `python -m curator.newsletter` in the "
        "secrets-scoped fetch job; absent means the lane is dark this run",
    )
    parser.add_argument(
        "--translation-artifact",
        type=Path,
        default=None,
        help="validated translation overlay; absent or invalid retains original-language data",
    )
    parser.add_argument(
        "--health-report",
        type=Path,
        default=None,
        help="write safe structured per-source freshness JSON",
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
    if args.source_snapshot:
        from .source_snapshot import (
            SourceSnapshotError,
            load_source_snapshot,
            snapshot_config_digest,
        )

        try:
            snapshot = load_source_snapshot(
                args.source_snapshot,
                expected_configuration_digest=snapshot_config_digest(cfg),
                current_time=now,
                max_age_seconds=cfg.source_snapshot_max_age_seconds,
            )
        except (OSError, SourceSnapshotError):
            # An explicit snapshot is a promise that collection already
            # happened. Never hide artifact loss or tampering with a second
            # network fetch that could produce a different story set.
            log.error("source snapshot invalid; refusing an implicit source refetch")
            return 2
        results = list(snapshot.results)
        log.info("source snapshot loaded (%s)", snapshot.content_digest[:12])
    else:
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

    if args.health_report:
        from .health import write_report

        try:
            write_report(results, args.health_report, now=now)
        except OSError:
            log.exception("source health report could not be written")
            return 2

    ranked = build(cfg, results, now, newsletter_on=not newsletter_meta.get("dark", True))
    ranked_zh = build_ranked_language(cfg, results, now, language="zh")
    visible = sum(len(v) for v in ranked.values())
    visible_zh = sum(len(v) for v in ranked_zh.values())

    # Preview images are resolved AFTER ranking and truncation, so the only
    # article heads fetched are the ones a reader will actually see. That is
    # what keeps an hourly job bounded: the ceiling is the union of capped EN
    # and ZH backend rows, not the number of headlines collected. Native rows
    # in either language are enriched before localization, so a translated
    # projection inherits the image attached to its authoritative original.
    cache_path = args.image_cache or (args.root / IMAGE_CACHE_FILE)
    cache = ImageCache.load(cache_path)
    originals = _ranked_originals(ranked, ranked_zh)
    # Newsletter items are excluded here AND refused inside enrich(): the
    # privacy rule (no article fetch, no cache entry for newsletter-derived
    # URLs) should survive either guard being refactored away.
    stats = enrich(
        [item for item in originals if not item.is_newsletter],
        cache,
        now,
        user_agent=cfg.user_agent,
        # `--offline` means no network, and that has to include this. Feed-borne
        # images and cache hits still apply, because neither touches the wire.
        config={**cfg.images, "enabled": False} if args.offline else cfg.images,
    )
    with_image = sum(1 for item in originals if not item.is_newsletter and item.image_url)
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

    # Language data is a backend artifact. The visual renderer remains
    # unchanged until the separate design phase.
    out_dir = args.out or (args.root / "site")
    translations = ()
    if args.translation_artifact:
        try:
            from .localization import load_translation_artifact

            translations = load_translation_artifact(args.translation_artifact)
        except (OSError, ValueError):
            log.warning("translation artifact invalid; original-language data retained")

    from .localization import build_localized_view, write_localized_projection

    localized_en = build_localized_view(
        target_language="en",
        native_ranked=ranked,
        source_ranked=ranked_zh,
        translations=translations,
    )
    localized_zh = build_localized_view(
        target_language="zh",
        native_ranked=ranked_zh,
        source_ranked=ranked,
        translations=translations,
    )
    _sanitize_newsletter_projection_urls(localized_en, localized_zh)
    data_dir = out_dir / "data"
    en_categories = list(cfg.categories)
    if not newsletter_meta.get("dark", True):
        en_categories.append(
            Category(name=NEWSLETTER_CATEGORY_NAME, id=NEWSLETTER_CATEGORY_ID)
        )
    write_localized_projection(
        language="en",
        categories=en_categories,
        ranked=localized_en,
        path=data_dir / "news-en.json",
        generated_at=now,
    )
    write_localized_projection(
        language="zh",
        categories=cfg.categories,
        ranked=localized_zh,
        path=data_dir / "news-zh.json",
        generated_at=now,
    )
    log.info("wrote localized backend projections in %s", data_dir)

    # The visual guard is deliberately later than the backend projections. A
    # Chinese-only run is valid current content, so it renders those originals
    # instead of returning success without producing the required index.
    if visible == 0 and not args.allow_empty:
        if not visible_zh:
            log.error(
                "no story matched any topic. Backend projections were written, but the "
                "empty build was rejected and the published visual page was not overwritten"
            )
            return 1

    rendered_ranked = ranked if visible else ranked_zh
    rendered_visible = visible if visible else visible_zh
    if not visible and visible_zh:
        log.info("no English story matched any topic; rendering the current Chinese view")

    path = render_site(
        rendered_ranked,
        results,
        now,
        out_dir,
        site_name=args.site_name or cfg.site_name,
        repo_url=_default_repo_url(cfg),
        cname_source=args.root / "CNAME",
    )
    log.info(
        "wrote %s (%d rows across %d topics)",
        path,
        rendered_visible,
        len(rendered_ranked),
    )

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
