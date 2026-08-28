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
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .dedup import dedupe
from .filter import assign_topics
from .models import Item, TierResult
from .rank import rank_items
from .render import render_site

log = logging.getLogger("curator")


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


def build(cfg: Config, results: list[TierResult], now: datetime) -> dict[str, list[Item]]:
    raw: list[Item] = [i for r in results for i in r.items]

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

    buckets = assign_topics(deduped, cfg.topics)
    ranked: dict[str, list[Item]] = {}
    for topic in cfg.topics:
        items = rank_items(buckets[topic.name], topic, now, cfg.ranking)
        ranked[topic.name] = items[: cfg.max_items_per_topic]
        log.info("topic %-22s %3d items", topic.name, len(ranked[topic.name]))
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
    ranked = build(cfg, results, now)
    visible = sum(len(v) for v in ranked.values())

    # The guard is on VISIBLE rows, and it runs after filtering, because that is
    # the only number that describes what a reader would actually get.
    if visible == 0 and cfg.topics and not args.allow_empty:
        log.error(
            "no story matched any topic. Refusing to overwrite the published page "
            "with an empty one. Re-run with --allow-empty to override."
        )
        return 1

    out_dir = args.out or (args.root / "site")
    path = render_site(
        ranked,
        results,
        now,
        out_dir,
        site_name=args.site_name or cfg.site_name,
        repo_url=_default_repo_url(cfg),
    )
    log.info("wrote %s (%d rows across %d topics)", path, visible, len(ranked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
