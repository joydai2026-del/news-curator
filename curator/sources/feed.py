"""RSS and Atom adapter preserving the current publisher-feed semantics."""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

import feedparser

from ..models import Item
from ..normalize import canonical_url, clean_title, safe_url
from .base import (
    SourceContext,
    SourceParseError,
    SourceResult,
    SourceSpec,
    enforce_body_bound,
    enforce_xml_bounds,
    option_bool,
    option_int,
    require_success,
    success_result,
    validate_option_keys,
)


MAX_DESCRIPTION_CHARS = 600
_IMAGE_ENCLOSURE_TYPE = "image/"
_XML_MIME_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/rdf+xml",
    "application/xml",
    "text/xml",
)
_MISLABELED_HTML_MIME_TYPE = "text/html"
_OPTION_KEYS = {
    "allow_mislabeled_html_mime",
    "max_items",
    "max_string_chars",
    "max_xml_nodes",
    "max_xml_depth",
    "description_chars",
}


class FeedAdapter:
    type_key = "feed"

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        values = validate_option_keys(spec, _OPTION_KEYS)
        return {
            "allow_mislabeled_html_mime": option_bool(
                values,
                "allow_mislabeled_html_mime",
                False,
                source_id=spec.id,
            ),
            "max_items": option_int(
                values, "max_items", 500, minimum=1, maximum=2000, source_id=spec.id
            ),
            "max_string_chars": option_int(
                values,
                "max_string_chars",
                20_000,
                minimum=100,
                maximum=200_000,
                source_id=spec.id,
            ),
            "max_xml_nodes": option_int(
                values,
                "max_xml_nodes",
                20_000,
                minimum=10,
                maximum=100_000,
                source_id=spec.id,
            ),
            "max_xml_depth": option_int(
                values, "max_xml_depth", 64, minimum=2, maximum=128, source_id=spec.id
            ),
            "description_chars": option_int(
                values,
                "description_chars",
                MAX_DESCRIPTION_CHARS,
                minimum=0,
                maximum=2000,
                source_id=spec.id,
            ),
        }

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        allowed_mime_types = _XML_MIME_TYPES
        if bool(spec.options["allow_mislabeled_html_mime"]):
            allowed_mime_types += (_MISLABELED_HTML_MIME_TYPE,)
        response = context.transport.get(
            spec.id,
            spec.url,
            allowed_mime_types=allowed_mime_types,
            user_agent=context.user_agent,
        )
        payload = require_success(response)
        enforce_body_bound(payload, spec)
        options = spec.options
        enforce_xml_bounds(
            payload,
            max_depth=int(options["max_xml_depth"]),
            max_nodes=int(options["max_xml_nodes"]),
            max_string_chars=int(options["max_string_chars"]),
            # Publisher feeds commonly embed full article bodies that this
            # adapter never consumes. The decoded response byte cap plus XML
            # node/depth bounds contain parser work; limits on fields we use
            # are applied below instead of rejecting the entire source.
            enforce_text_limit=False,
        )
        parsed = feedparser.parse(payload)
        entries = list(parsed.entries[: int(options["max_items"])])
        if bool(options["allow_mislabeled_html_mime"]) and not _is_known_feed(
            parsed
        ):
            raise SourceParseError("malformed_feed")
        if getattr(parsed, "bozo", False) and not entries:
            raise SourceParseError("malformed_feed")
        now = context.now()
        items = _rss_items(entries, spec, now)
        if getattr(parsed, "bozo", False):
            return success_result(
                spec,
                items,
                now,
                status_hint="malformed",
                reason_code="malformed_salvaged",
                note="malformed_salvaged",
            )
        if (
            not spec.echo_eligible
            and (urlsplit(spec.url).hostname or "").casefold() == "news.google.com"
        ):
            return success_result(
                spec,
                items,
                now,
                status_hint="link_resolution_degraded",
                reason_code="google_news_url_retained_non_corroborating",
                note="google_news_url_retained_non_corroborating",
            )
        return success_result(spec, items, now)


class RssAdapter(FeedAdapter):
    """Configuration alias for existing ``type: rss`` rows."""

    type_key = "rss"


class AtomAdapter(FeedAdapter):
    """Explicit Atom discriminator with the same feedparser semantics."""

    type_key = "atom"


def parse_feed_document(payload: bytes, spec: SourceSpec, now: datetime) -> list[Item]:
    """Parse an already captured feed using the same bounded code path."""

    options = spec.options
    enforce_body_bound(payload, spec)
    enforce_xml_bounds(
        payload,
        max_depth=int(options["max_xml_depth"]),
        max_nodes=int(options["max_xml_nodes"]),
        max_string_chars=int(options["max_string_chars"]),
        enforce_text_limit=False,
    )
    parsed = feedparser.parse(payload)
    entries = list(parsed.entries[: int(options["max_items"])])
    if getattr(parsed, "bozo", False) and not entries:
        raise SourceParseError("malformed_feed")
    return _rss_items(entries, spec, now.astimezone(timezone.utc))


def _timestamp(entry: Mapping[str, Any]) -> tuple[datetime, bool] | None:
    for key, estimated in (
        ("published_parsed", False),
        ("created_parsed", False),
        ("updated_parsed", True),
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(
                    timegm(parsed), tz=timezone.utc
                ), estimated
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _is_known_feed(parsed: Any) -> bool:
    """Require a recognized RSS or Atom format for a MIME-exception source."""

    version = str(getattr(parsed, "version", "") or "").casefold()
    return version.startswith("rss") or version.startswith("atom")


def _rss_items(entries: list[Any], spec: SourceSpec, now: datetime) -> list[Item]:
    items: list[Item] = []
    maximum = int(spec.options["max_string_chars"])
    for native_rank, entry in enumerate(entries):
        title = clean_title(_limited_text(entry.get("title"), maximum))
        link = safe_url(_limited_text(entry.get("link"), maximum))
        if not title or link is None:
            continue
        canonical = canonical_url(link)
        stamped = _timestamp(entry)
        if canonical is None or stamped is None:
            continue
        published, estimated = stamped
        items.append(
            Item(
                title=title,
                url=link,
                canonical_url=canonical,
                source_id=spec.id,
                source_name=spec.name,
                platform=spec.platform,
                published_at=min(published, now),
                source_weight=spec.weight,
                is_aggregator=spec.is_aggregator,
                time_is_estimated=estimated,
                image_url=_entry_image(entry, maximum),
                description=_entry_summary(
                    entry, maximum, int(spec.options["description_chars"])
                ),
                language=spec.language,
                echo_eligible=spec.echo_eligible,
                # Feed order is meaningful only for a source explicitly
                # configured as the Trending list. A position in a publisher's
                # general/category feed is not comparable with HN Trending and
                # must not leak through an exact-link merge.
                native_rank=native_rank if spec.category == "trending" else None,
                native_categories={spec.category} if spec.category else set(),
            )
        )
    return items


def _entry_image(entry: Mapping[str, Any], maximum: int) -> str:
    candidates: list[str] = []
    for media in entry.get("media_content") or []:
        if not isinstance(media, Mapping):
            continue
        url = media.get("url")
        medium = str(media.get("medium") or "").lower()
        media_type = str(media.get("type") or "").lower()
        if url and (
            medium == "image"
            or media_type.startswith("image/")
            or (not medium and not media_type)
        ):
            candidate = _limited_text(url, maximum)
            if candidate:
                candidates.append(candidate)
    for thumbnail in entry.get("media_thumbnail") or []:
        if isinstance(thumbnail, Mapping) and thumbnail.get("url"):
            candidate = _limited_text(thumbnail["url"], maximum)
            if candidate:
                candidates.append(candidate)
    for link in entry.get("links") or []:
        if not isinstance(link, Mapping):
            continue
        if link.get("rel") == "enclosure" and str(
            link.get("type") or ""
        ).lower().startswith(_IMAGE_ENCLOSURE_TYPE):
            if link.get("href"):
                candidate = _limited_text(link["href"], maximum)
                if candidate:
                    candidates.append(candidate)
    for candidate in candidates:
        cleaned = safe_url(candidate)
        if cleaned:
            return cleaned
    return ""


def _entry_summary(entry: Mapping[str, Any], maximum: int, limit: int) -> str:
    if limit == 0:
        return ""
    raw = str(entry.get("summary") or entry.get("description") or "")[:maximum]
    text = clean_title(raw)
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space].rstrip()
    return cut.rstrip(".,;:!?-–") + "…"


def _limited_text(value: object, maximum: int) -> str:
    """Return a used feed field only when it fits the semantic field cap."""

    text = str(value or "")
    return text if len(text) <= maximum else ""
