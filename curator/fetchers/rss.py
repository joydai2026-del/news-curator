"""Tier B — RSS feeds. The backbone, precisely because it is boring.

A stable, unauthenticated, publisher-published interface. One feed failing is
logged and skipped.

Feeds arrive from two places and both are fetched the same way: the shared pool
in sources.yaml, and the curated per-category lists in topics.yaml. A feed from
a category list tags its items with that category id, which is what lets a
single-subject publication fill a section without every headline having to
contain a keyword.

Four details that came out of review and measurement:

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
  * **Many feeds already carry the picture.** `media:content`, `media:thumbnail`
    and image enclosures are read here, which costs nothing and is the only way
    to get a preview image from publishers who refuse a direct article fetch.
    Measured across the shipped feed list, this covers roughly half of them
    before a single extra request is made.

Feeds are fetched in parallel because the list is long enough that doing them
one at a time made the run slower than the news. Concurrency is bounded and
configurable: this is someone else's server.
"""

from __future__ import annotations

import concurrent.futures as futures
import logging
import threading
import time
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import requests

from ..config import Config, RssSource
from ..models import Item, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 8 * 1024 * 1024

# Total transfer budget per feed, as a multiple of the per-read timeout. A
# whole feed that cannot arrive in this long is a feed that is failing.
FEED_TRANSFER_TIMEOUT_FACTOR = 4

_IMAGE_ENCLOSURE_TYPE = "image/"


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


def entry_image(entry) -> str:
    """The preview image this feed entry declares, or "".

    Checked in the order publishers actually mean them: an explicit
    `media:content` image, then a thumbnail, then an image enclosure. Everything
    goes through the same scheme allow-list as any other link, because a feed we
    do not control must not decide what ends up in a `src` on a public page.
    """
    candidates: list[str] = []

    for media in (entry.get("media_content") or []):
        if not isinstance(media, dict):
            continue
        url = media.get("url")
        medium = str(media.get("medium") or "").lower()
        mtype = str(media.get("type") or "").lower()
        # `media:content` also carries audio and video. Take it only when it
        # says it is an image, or says nothing at all (the common sloppy case).
        if url and (medium == "image" or mtype.startswith("image/") or (not medium and not mtype)):
            candidates.append(str(url))

    for thumb in (entry.get("media_thumbnail") or []):
        if isinstance(thumb, dict) and thumb.get("url"):
            candidates.append(str(thumb["url"]))

    for link in (entry.get("links") or []):
        if not isinstance(link, dict):
            continue
        if link.get("rel") == "enclosure" and str(link.get("type") or "").lower().startswith(_IMAGE_ENCLOSURE_TYPE):
            if link.get("href"):
                candidates.append(str(link["href"]))

    for candidate in candidates:
        cleaned = safe_url(candidate)
        if cleaned:
            return cleaned
    return ""


def _fetch_one(source: RssSource, cfg: Config, malformed: set[str], lock: threading.Lock) -> list[Item]:
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

    # `timeout` is per READ, not per transfer, so a server dripping bytes just
    # under it would hold this worker open indefinitely. With eight workers,
    # eight such feeds stall the whole tier until the Actions job times out.
    # This is the total-transfer deadline that bounds it.
    deadline = time.monotonic() + (cfg.timeout * FEED_TRANSFER_TIMEOUT_FACTOR)

    chunks, total = [], 0
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise FeedTruncated(f"exceeded {MAX_FEED_BYTES} bytes")
            if time.monotonic() > deadline:
                raise FeedTruncated("transfer deadline reached")
    finally:
        resp.close()

    parsed = feedparser.parse(b"".join(chunks))
    if getattr(parsed, "bozo", False):
        if not parsed.entries:
            raise ValueError("malformed feed document")
        # Entries parsed despite a malformed document. That is usable but not
        # clean, and "usable" should not be reported as "healthy".
        log.warning("rss %s: malformed document, salvaged %d entries", source.id, len(parsed.entries))
        with lock:
            malformed.add(source.id)

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
                image_url=entry_image(entry),
                native_categories={source.category} if source.category else set(),
            )
        )
    return items


def fetch(cfg: Config) -> TierResult:
    feeds = cfg.all_feeds
    if not feeds:
        return TierResult(tier="rss", ok=True, note="no feeds configured")

    items: list[Item] = []
    failed: list[str] = []
    malformed: set[str] = set()
    empty: list[str] = []
    lock = threading.Lock()

    def work(source: RssSource) -> None:
        try:
            got = _fetch_one(source, cfg, malformed, lock)
        except Exception as exc:
            with lock:
                failed.append(source.id)
            # Detail goes to the log, never to the public page: a forker may
            # have put a credential in a feed URL.
            log.warning("rss %s failed: %s", source.id, exc)
            return
        if not got:
            # A feed that answers 200, parses cleanly, and yields nothing usable
            # is the quietest way for a source to die. Fierce Biotech did
            # exactly this: 25 entries, every one dropped for an unparseable
            # <pubDate>. Without this it looks identical to a slow news day.
            with lock:
                empty.append(source.id)
            log.warning("rss %s returned no usable items", source.id)
            return
        with lock:
            items.extend(got)
        log.info("rss %-16s %3d items", source.id, len(got))

    workers = min(cfg.fetch_workers, len(feeds))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, feeds))

    if failed and len(failed) == len(feeds):
        return TierResult(tier="rss", items=[], ok=False, note="all feeds unavailable")

    notes = []
    if failed:
        notes.append(f"{len(failed)} of {len(feeds)} feeds unavailable")
    if empty:
        notes.append(f"{len(empty)} of {len(feeds)} feeds returned nothing usable")
    if malformed:
        notes.append(f"{len(malformed)} feeds malformed but salvaged")
    return TierResult(tier="rss", items=items, ok=not notes, note="; ".join(notes))
