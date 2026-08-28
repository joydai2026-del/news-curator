"""Tier B — RSS feeds. The backbone, precisely because it is boring.

A stable, unauthenticated, publisher-published interface. One feed failing is
logged and skipped.

Three details that came out of review:

  * **Size caps are failures, not successes.** A feed truncated mid-XML parses
    into a partial entry list. Publishing half a feed while reporting the source
    as healthy is a quiet lie, so a truncated feed is discarded and reported.
  * **`bozo` is checked.** feedparser is forgiving and will happily return two
    entries from a broken document. If it flagged the document as malformed and
    we got nothing usable, that is a failure.
  * **Publish time versus update time are not the same fact.** An article edited
    in place must not leap to the top of a recency-ranked page as if it were
    new. Where only an `updated` timestamp exists, the item is marked
    `time_is_estimated` so nothing downstream treats it as a confirmed
    publication time.
"""

from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import requests

from ..config import Config, RssSource
from ..models import Item, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 8 * 1024 * 1024

# Feeds that parsed but were malformed on this run. Reported on the page so a
# degrading source is visible before it fails outright.
_MALFORMED: set[str] = set()


class FeedTruncated(Exception):
    """The feed exceeded the size cap, so what we have is not a whole document."""


def _timestamp(entry) -> tuple[datetime, bool] | None:
    """(published_at, is_estimated). Prefers a true publication time."""
    for key, estimated in (
        ("published_parsed", False),
        ("created_parsed", False),
        ("updated_parsed", True),
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc), estimated
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _fetch_one(source: RssSource, cfg: Config) -> list[Item]:
    resp = requests.get(
        source.url,
        timeout=cfg.timeout,
        headers={
            "User-Agent": cfg.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
        stream=True,
    )
    resp.raise_for_status()

    chunks, total = [], 0
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise FeedTruncated(f"exceeded {MAX_FEED_BYTES} bytes")
    finally:
        resp.close()

    parsed = feedparser.parse(b"".join(chunks))
    if getattr(parsed, "bozo", False):
        if not parsed.entries:
            raise ValueError("malformed feed document")
        # Entries parsed despite a malformed document. That is usable but not
        # clean, and "usable" should not be reported as "healthy".
        log.warning("rss %s: malformed document, salvaged %d entries", source.id, len(parsed.entries))
        _MALFORMED.add(source.id)

    now = datetime.now(timezone.utc)
    items: list[Item] = []

    for entry in parsed.entries:
        title = clean_title(entry.get("title") or "")
        link = safe_url(entry.get("link") or "")
        if not title or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue

        stamped = _timestamp(entry)
        if stamped is None:
            # No usable date. Treating it as "now" would let undated feeds
            # dominate a recency-weighted page forever, so it is dropped.
            continue
        published, estimated = stamped

        items.append(
            Item(
                title=title,
                url=link,
                canonical_url=canonical,
                source_id=source.id,
                source_name=source.name,
                platform=source.platform,
                published_at=min(published, now),  # a feed whose clock runs ahead
                source_weight=source.weight,
                is_aggregator=source.is_aggregator,
                time_is_estimated=estimated,
            )
        )
    return items


def fetch(cfg: Config) -> TierResult:
    if not cfg.rss:
        return TierResult(tier="rss", ok=True, note="no feeds configured")

    items: list[Item] = []
    failed: list[str] = []
    _MALFORMED.clear()

    for source in cfg.rss:
        try:
            got = _fetch_one(source, cfg)
            items.extend(got)
            log.info("rss %-14s %3d items", source.id, len(got))
        except Exception as exc:
            failed.append(source.id)
            # Detail goes to the log, never to the public page: a forker may
            # have put a credential in a feed URL.
            log.warning("rss %s failed: %s", source.id, exc)

    if failed and len(failed) == len(cfg.rss):
        return TierResult(tier="rss", items=[], ok=False, note="all feeds unavailable")

    notes = []
    if failed:
        notes.append(f"{len(failed)} of {len(cfg.rss)} feeds unavailable")
    if _MALFORMED:
        notes.append(f"{len(_MALFORMED)} feeds malformed but salvaged")
    return TierResult(tier="rss", items=items, ok=not notes, note="; ".join(notes))
