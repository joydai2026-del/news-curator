"""JSON Feed 1.1 adapter with bounded parsing and normalized output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..models import Item
from ..normalize import canonical_url, clean_title, safe_url
from .base import (
    SourceContext,
    SourceParseError,
    SourceResult,
    SourceSpec,
    bounded_text,
    enforce_body_bound,
    option_int,
    parse_bounded_json,
    require_success,
    success_result,
    validate_option_keys,
)


_JSON_MIME_TYPES = ("application/json", "application/feed+json")
_OPTION_KEYS = {
    "max_items",
    "max_string_chars",
    "max_json_nodes",
    "max_json_depth",
    "description_chars",
}


class JsonFeedAdapter:
    type_key = "json_feed"

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        values = validate_option_keys(spec, _OPTION_KEYS)
        return {
            "max_items": option_int(
                values, "max_items", 500, minimum=1, maximum=2000, source_id=spec.id
            ),
            "max_string_chars": option_int(
                values,
                "max_string_chars",
                50_000,
                minimum=100,
                maximum=500_000,
                source_id=spec.id,
            ),
            "max_json_nodes": option_int(
                values,
                "max_json_nodes",
                30_000,
                minimum=10,
                maximum=200_000,
                source_id=spec.id,
            ),
            "max_json_depth": option_int(
                values, "max_json_depth", 32, minimum=2, maximum=64, source_id=spec.id
            ),
            "description_chars": option_int(
                values,
                "description_chars",
                600,
                minimum=0,
                maximum=2000,
                source_id=spec.id,
            ),
        }

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        response = context.transport.get(
            spec.id,
            spec.url,
            allowed_mime_types=_JSON_MIME_TYPES,
            user_agent=context.user_agent,
        )
        payload = require_success(response)
        now = context.now()
        items = parse_json_feed(payload, spec, now)
        return success_result(spec, items, now)


def parse_json_feed(payload: bytes, spec: SourceSpec, now: datetime) -> list[Item]:
    enforce_body_bound(payload, spec)
    options = spec.options
    document = parse_bounded_json(
        payload,
        max_depth=int(options["max_json_depth"]),
        max_nodes=int(options["max_json_nodes"]),
        max_string_chars=int(options["max_string_chars"]),
    )
    if not isinstance(document, Mapping):
        raise SourceParseError("malformed_json_feed")
    version = _json_text(document, "version")
    if version not in {
        "https://jsonfeed.org/version/1",
        "https://jsonfeed.org/version/1.1",
    }:
        raise SourceParseError("unsupported_json_feed_version")
    rows = document.get("items")
    if not isinstance(rows, list):
        raise SourceParseError("malformed_json_feed")
    if len(rows) > int(options["max_items"]):
        raise SourceParseError("item_limit_exceeded")

    maximum = int(options["max_string_chars"])
    current = now.astimezone(timezone.utc)
    items: list[Item] = []
    for native_rank, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        title = clean_title(bounded_text(_json_text(row, "title"), maximum=maximum))
        link = safe_url(bounded_text(_json_text(row, "url"), maximum=maximum))
        if not title or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue
        published = _iso_datetime(
            bounded_text(_json_text(row, "date_published"), maximum=maximum)
        )
        estimated = False
        if published is None:
            published = _iso_datetime(
                bounded_text(_json_text(row, "date_modified"), maximum=maximum)
            )
            estimated = published is not None
        if published is None:
            continue
        image = (
            safe_url(
                bounded_text(
                    _json_text(row, "image") or _json_text(row, "banner_image"),
                    maximum=maximum,
                )
            )
            or ""
        )
        description = _description(
            bounded_text(_json_text(row, "summary"), maximum=maximum),
            int(options["description_chars"]),
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
                time_is_estimated=estimated,
                image_url=image,
                description=description,
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


def _description(raw: str, limit: int) -> str:
    if limit == 0:
        return ""
    text = clean_title(raw)
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space].rstrip()
    return cut.rstrip(".,;:!?-–") + "…"


def _json_text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceParseError("malformed_json_feed")
    return value
