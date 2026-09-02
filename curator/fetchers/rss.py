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
  * **Many feeds already carry the blurb too.** The entry's own `summary` (or
    `description`) is the publisher's sentence about their own story, and it
    goes through exactly the same cleaning path as the title: tags out of the
    RAW text first, then entities unescaped once. Doing it the other way round
    deletes real content, which is the bug `clean_title` exists to prevent, so
    the summary reuses that function rather than reimplementing it slightly
    differently. It is stored truncated and displayed clamped, and it is never
    written or rewritten by us.

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
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import feedparser
import requests

from ..config import Config, RssSource
from ..models import Item, SourceHealth, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 8 * 1024 * 1024

# Total transfer budget per feed, as a multiple of the per-read timeout. A
# whole feed that cannot arrive in this long is a feed that is failing.
FEED_TRANSFER_TIMEOUT_FACTOR = 4

_IMAGE_ENCLOSURE_TYPE = "image/"

# How much of a publisher's summary we keep. The card clamps to two lines, so
# this is about not shipping a whole article body into a static page that some
# feeds put in `description`. It is a storage cap, not an editorial one.
MAX_DESCRIPTION_CHARS = 600

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"


class FeedTruncated(Exception):
    """The feed exceeded the size cap, so what we have is not a whole document."""


class MalformedDocument(ValueError):
    """The response arrived but cannot be safely parsed as its configured type."""


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


def entry_summary(entry, *, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """The publisher's own blurb for this entry, cleaned, or "".

    `summary` is preferred over `description` because feedparser maps the two
    onto the same field for RSS but keeps them distinct for Atom, where
    `summary` is the human sentence and `content` is the article. We want the
    sentence.

    Cleaning is `clean_title` verbatim, and that is deliberate rather than lazy:
    it is the one function in this codebase that strips markup from the RAW text
    before unescaping entities, which is the order that does not eat `2 &lt; 3`.
    A second, subtly different cleaner for summaries would be a second place for
    that bug to come back.

    Truncation cuts at a word boundary and marks the cut with an ellipsis, so a
    clamped card never implies the publisher wrote a sentence that stops mid
    word. Nothing else is added, removed or rephrased: an empty summary stays
    empty and the card renders without one.
    """
    raw = entry.get("summary") or entry.get("description") or ""
    text = clean_title(raw)
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space].rstrip()
    return cut.rstrip(".,;:!?-–—") + "…"


def _reject_unsafe_xml(payload: bytes) -> None:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise MalformedDocument("DTD/entity declarations are not allowed")


def _iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _rss_items(parsed, source: RssSource, now: datetime) -> list[Item]:
    items: list[Item] = []
    for native_rank, entry in enumerate(parsed.entries):
        title = clean_title(entry.get("title") or "")
        link = safe_url(entry.get("link") or "")
        if not title or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue

        stamped = _timestamp(entry)
        if stamped is None:
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
                published_at=min(published, now),
                source_weight=source.weight,
                is_aggregator=source.is_aggregator,
                time_is_estimated=estimated,
                image_url=entry_image(entry),
                description=entry_summary(entry),
                language=source.language,
                echo_eligible=source.echo_eligible,
                native_rank=native_rank,
                native_categories={source.category} if source.category else set(),
            )
        )
    return items


def _sitemap_items(payload: bytes, source: RssSource, now: datetime) -> list[Item]:
    _reject_unsafe_xml(payload)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise MalformedDocument("malformed news sitemap") from exc

    items: list[Item] = []
    for native_rank, node in enumerate(root.findall(f"{{{SITEMAP_NS}}}url")):
        loc = node.findtext(f"{{{SITEMAP_NS}}}loc") or ""
        news = node.find(f"{{{NEWS_NS}}}news")
        if news is None:
            continue
        title = clean_title(news.findtext(f"{{{NEWS_NS}}}title") or "")
        published = _iso_datetime(news.findtext(f"{{{NEWS_NS}}}publication_date") or "")
        link = safe_url(loc)
        if not title or published is None or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue
        image = safe_url(node.findtext(f"{{{IMAGE_NS}}}image/{{{IMAGE_NS}}}loc") or "") or ""
        items.append(
            Item(
                title=title,
                url=link,
                canonical_url=canonical,
                source_id=source.id,
                source_name=source.name,
                platform=source.platform,
                published_at=min(published, now),
                source_weight=source.weight,
                is_aggregator=source.is_aggregator,
                image_url=image,
                language=source.language,
                echo_eligible=source.echo_eligible,
                native_rank=native_rank,
                native_categories={source.category} if source.category else set(),
            )
        )
    return items


def parse_document(payload: bytes, source: RssSource, now: datetime | None = None) -> list[Item]:
    """Parse a captured source document without network access."""
    current = now or datetime.now(timezone.utc)
    _reject_unsafe_xml(payload)
    if source.type == "news_sitemap":
        return _sitemap_items(payload, source, current)
    parsed = feedparser.parse(payload)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise MalformedDocument("malformed feed document")
    return _rss_items(parsed, source, current)


def source_health(
    source: RssSource,
    items: list[Item],
    now: datetime,
    *,
    default_max_age_hours: float = 48,
    status_hint: str = "",
    reason_code: str = "",
) -> SourceHealth:
    """Evaluate newest usable item time before the global 48-hour filter."""
    threshold = float(source.max_age_hours or default_max_age_hours)
    newest = max((item.published_at for item in items), default=None)
    age = max(0.0, (now - newest).total_seconds() / 3600.0) if newest else None

    if status_hint in {"unavailable", "empty"}:
        status = status_hint
        if status == "empty":
            reason_code = reason_code or "no_usable_items"
    elif status_hint == "malformed" and newest is None:
        status = "malformed"
        reason_code = reason_code or "malformed_document"
    elif newest is None:
        status = "empty"
        reason_code = reason_code or "no_usable_items"
    elif age is not None and age > threshold:
        status = "stale"
        reason_code = "newest_item_too_old"
    elif status_hint == "malformed":
        status = "malformed"
        reason_code = reason_code or "malformed_salvaged"
    elif not source.echo_eligible and (urlsplit(source.url).hostname or "").casefold() == "news.google.com":
        status = "link_resolution_degraded"
        reason_code = "google_news_url_retained_non_corroborating"
    else:
        status = "fresh"

    return SourceHealth(
        source_id=source.id,
        status=status,
        usable_items=len(items),
        newest_at=newest,
        age_hours=age,
        max_age_hours=threshold,
        language=source.language,
        source_type=source.type,
        echo_eligible=source.echo_eligible,
        reason_code=reason_code,
    )


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

    payload = b"".join(chunks)
    if source.type == "news_sitemap":
        return parse_document(payload, source)

    parsed = feedparser.parse(payload)
    if getattr(parsed, "bozo", False):
        if not parsed.entries:
            raise MalformedDocument("malformed feed document")
        log.warning("rss %s: malformed document, salvaged %d entries", source.id, len(parsed.entries))
        with lock:
            malformed.add(source.id)
    return _rss_items(parsed, source, datetime.now(timezone.utc))


def fetch(cfg: Config) -> TierResult:
    feeds = cfg.all_feeds
    if not feeds:
        return TierResult(tier="rss", ok=True, note="no feeds configured")

    items: list[Item] = []
    failed: list[str] = []
    malformed: set[str] = set()
    empty: list[str] = []
    health_by_id: dict[str, SourceHealth] = {}
    lock = threading.Lock()
    checked_at = datetime.now(timezone.utc)

    def work(source: RssSource) -> None:
        try:
            got = _fetch_one(source, cfg, malformed, lock)
        except MalformedDocument as exc:
            with lock:
                failed.append(source.id)
                health_by_id[source.id] = source_health(
                    source,
                    [],
                    checked_at,
                    default_max_age_hours=cfg.default_source_max_age_hours,
                    status_hint="malformed",
                    reason_code="malformed_document",
                )
            log.warning("rss %s malformed: %s", source.id, exc)
            return
        except Exception as exc:
            with lock:
                failed.append(source.id)
                health_by_id[source.id] = source_health(
                    source,
                    [],
                    checked_at,
                    default_max_age_hours=cfg.default_source_max_age_hours,
                    status_hint="unavailable",
                    reason_code="request_or_parse_failed",
                )
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
                health_by_id[source.id] = source_health(
                    source,
                    [],
                    checked_at,
                    default_max_age_hours=cfg.default_source_max_age_hours,
                    status_hint="empty",
                    reason_code="no_usable_items",
                )
            log.warning("rss %s returned no usable items", source.id)
            return
        with lock:
            items.extend(got)
            health_by_id[source.id] = source_health(
                source,
                got,
                checked_at,
                default_max_age_hours=cfg.default_source_max_age_hours,
                status_hint="malformed" if source.id in malformed else "",
            )
        log.info("rss %-16s %3d items", source.id, len(got))

    workers = min(cfg.fetch_workers, len(feeds))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, feeds))

    if failed and len(failed) == len(feeds):
        return TierResult(
            tier="rss",
            items=[],
            ok=False,
            note="all feeds unavailable",
            source_health=[health_by_id[s.id] for s in feeds],
        )

    notes = []
    if failed:
        notes.append(f"{len(failed)} of {len(feeds)} feeds unavailable")
    if empty:
        notes.append(f"{len(empty)} of {len(feeds)} feeds returned nothing usable")
    if malformed:
        notes.append(f"{len(malformed)} feeds malformed but salvaged")
    statuses = [health_by_id[s.id].status for s in feeds]
    stale = statuses.count("stale")
    degraded_links = statuses.count("link_resolution_degraded")
    if stale:
        notes.append(f"{stale} of {len(feeds)} feeds stale")
    if degraded_links:
        notes.append(f"{degraded_links} Google News feeds retain non-corroborating links")
    return TierResult(
        tier="rss",
        items=items,
        ok=not notes,
        note="; ".join(notes),
        source_health=[health_by_id[s.id] for s in feeds],
    )
