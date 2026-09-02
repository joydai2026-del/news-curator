"""Bounded Google-style news sitemap adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from ..models import Item
from ..normalize import canonical_url, clean_title, safe_url
from .base import (
    SourceContext,
    SourceParseError,
    SourceResult,
    SourceSpec,
    bounded_text,
    enforce_body_bound,
    enforce_xml_bounds,
    option_int,
    require_success,
    success_result,
    validate_option_keys,
)


SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"
_XML_MIME_TYPES = ("application/xml", "text/xml", "application/rss+xml")
_OPTION_KEYS = {"max_items", "max_string_chars", "max_xml_nodes", "max_xml_depth"}


class NewsSitemapAdapter:
    type_key = "news_sitemap"

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        values = validate_option_keys(spec, _OPTION_KEYS)
        return {
            "max_items": option_int(
                values, "max_items", 1000, minimum=1, maximum=5000, source_id=spec.id
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
                30_000,
                minimum=10,
                maximum=100_000,
                source_id=spec.id,
            ),
            "max_xml_depth": option_int(
                values, "max_xml_depth", 32, minimum=2, maximum=128, source_id=spec.id
            ),
        }

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        response = context.transport.get(
            spec.id,
            spec.url,
            allowed_mime_types=_XML_MIME_TYPES,
            user_agent=context.user_agent,
        )
        payload = require_success(response)
        now = context.now()
        items = parse_news_sitemap(payload, spec, now)
        return success_result(spec, items, now)


def parse_news_sitemap(payload: bytes, spec: SourceSpec, now: datetime) -> list[Item]:
    enforce_body_bound(payload, spec)
    options = spec.options
    enforce_xml_bounds(
        payload,
        max_depth=int(options["max_xml_depth"]),
        max_nodes=int(options["max_xml_nodes"]),
        max_string_chars=int(options["max_string_chars"]),
    )
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise SourceParseError("malformed_news_sitemap") from None
    nodes = root.findall(f"{{{SITEMAP_NS}}}url")
    if len(nodes) > int(options["max_items"]):
        raise SourceParseError("item_limit_exceeded")
    maximum = int(options["max_string_chars"])
    current = now.astimezone(timezone.utc)
    items: list[Item] = []
    for native_rank, node in enumerate(nodes):
        loc = bounded_text(node.findtext(f"{{{SITEMAP_NS}}}loc") or "", maximum=maximum)
        news = node.find(f"{{{NEWS_NS}}}news")
        if news is None:
            continue
        title = clean_title(
            bounded_text(news.findtext(f"{{{NEWS_NS}}}title") or "", maximum=maximum)
        )
        published = _iso_datetime(
            bounded_text(
                news.findtext(f"{{{NEWS_NS}}}publication_date") or "", maximum=maximum
            )
        )
        link = safe_url(loc)
        if not title or published is None or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue
        image = (
            safe_url(
                bounded_text(
                    node.findtext(f"{{{IMAGE_NS}}}image/{{{IMAGE_NS}}}loc") or "",
                    maximum=maximum,
                )
            )
            or ""
        )
        items.append(
            Item(
                title=title,
                url=link,
                canonical_url=canonical,
                source_id=spec.id,
                source_name=spec.name,
                platform=spec.platform,
                published_at=min(published, current),
                source_weight=spec.weight,
                is_aggregator=spec.is_aggregator,
                image_url=image,
                language=spec.language,
                echo_eligible=spec.echo_eligible,
                native_rank=native_rank if spec.category == "trending" else None,
                native_categories={spec.category} if spec.category else set(),
            )
        )
    return items


def _iso_datetime(value: str) -> datetime | None:
    text = value.strip()
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
