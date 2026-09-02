"""Hacker News Algolia adapter with typed query context and run caps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

from ..models import Item
from ..normalize import canonical_url, clean_title, safe_url
from .base import (
    SourceContext,
    SourceParseError,
    SourceResult,
    SourceSpec,
    SourceValidationError,
    bounded_text,
    enforce_body_bound,
    option_bool,
    option_float,
    option_int,
    parse_bounded_json,
    require_success,
    success_result,
    validate_option_keys,
)
from .errors import SafeTransportError


HN_ITEM = "https://news.ycombinator.com/item?id="
MAX_REQUESTS_PER_RUN = 60
DEFAULT_BUDGET_SECONDS = 120.0
_JSON_MIME_TYPES = ("application/json",)
_OPTION_KEYS = {
    "min_points_ranked",
    "include_by_date",
    "hits_per_page",
    "max_requests",
    "budget_seconds",
    "front_page_hits_per_page",
    "front_page_max_age_hours",
    "front_page_category",
    "max_json_nodes",
    "max_json_depth",
    "max_string_chars",
}


class HackerNewsAdapter:
    type_key = "hackernews"

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        values = validate_option_keys(spec, _OPTION_KEYS)
        raw_front_category = values.get("front_page_category", "trending")
        if not isinstance(raw_front_category, str):
            raise SourceValidationError(
                f"source {spec.id}: options.front_page_category is invalid"
            )
        front_category = raw_front_category.strip()
        if (
            not front_category
            or len(front_category) > 80
            or any(ord(ch) < 32 for ch in front_category)
        ):
            raise SourceValidationError(
                f"source {spec.id}: options.front_page_category is invalid"
            )
        return {
            "min_points_ranked": option_int(
                values,
                "min_points_ranked",
                20,
                minimum=0,
                maximum=1_000_000,
                source_id=spec.id,
            ),
            "include_by_date": option_bool(
                values, "include_by_date", True, source_id=spec.id
            ),
            "hits_per_page": option_int(
                values, "hits_per_page", 40, minimum=1, maximum=100, source_id=spec.id
            ),
            "max_requests": option_int(
                values,
                "max_requests",
                MAX_REQUESTS_PER_RUN,
                minimum=1,
                maximum=200,
                source_id=spec.id,
            ),
            "budget_seconds": option_float(
                values,
                "budget_seconds",
                DEFAULT_BUDGET_SECONDS,
                minimum=1,
                maximum=900,
                source_id=spec.id,
            ),
            "front_page_hits_per_page": option_int(
                values,
                "front_page_hits_per_page",
                30,
                minimum=1,
                maximum=100,
                source_id=spec.id,
            ),
            "front_page_max_age_hours": option_float(
                values,
                "front_page_max_age_hours",
                12,
                minimum=1,
                maximum=168,
                source_id=spec.id,
            ),
            "front_page_category": front_category,
            "max_json_nodes": option_int(
                values,
                "max_json_nodes",
                20_000,
                minimum=10,
                maximum=100_000,
                source_id=spec.id,
            ),
            "max_json_depth": option_int(
                values, "max_json_depth", 24, minimum=2, maximum=64, source_id=spec.id
            ),
            "max_string_chars": option_int(
                values,
                "max_string_chars",
                20_000,
                minimum=100,
                maximum=200_000,
                source_id=spec.id,
            ),
        }

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        options = spec.options
        now = context.now()
        cutoff = int(now.timestamp() - spec.max_age_hours * 3600)
        plans = _query_plans(
            context,
            cutoff=cutoff,
            minimum_points=int(options["min_points_ranked"]),
            include_by_date=bool(options["include_by_date"]),
        )
        max_requests = int(options["max_requests"])
        capped = len(plans) > max_requests
        plans = plans[:max_requests]
        notes: list[str] = []
        items: list[Item] = []
        failures = 0

        front_limit = int(options["front_page_hits_per_page"])
        front_failed = False
        try:
            front_hits = self._query(
                spec,
                context,
                "search",
                {"tags": "front_page", "hitsPerPage": front_limit},
                max_items=front_limit,
            )
        except (SafeTransportError, SourceParseError):
            front_hits = []
            front_failed = True
            notes.append("front_page_unavailable")
        for native_rank, hit in enumerate(front_hits):
            item = _to_item(
                hit,
                spec,
                native_category=str(options["front_page_category"]),
                native_rank=native_rank,
            )
            if item is not None:
                items.append(item)
        if not front_failed and not front_hits:
            notes.append("front_page_empty")
        front_items = tuple(items)
        newest_front = max((item.published_at for item in front_items), default=None)
        if newest_front is not None:
            front_age = max(0.0, (now - newest_front).total_seconds() / 3600.0)
            if front_age > float(options["front_page_max_age_hours"]):
                notes.append("front_page_stale")

        started = context.now()
        exhausted = False
        hits_per_page = int(options["hits_per_page"])
        for term, endpoint, numeric_filters in plans:
            if (context.now() - started).total_seconds() > float(
                options["budget_seconds"]
            ):
                exhausted = True
                break
            try:
                hits = self._query(
                    spec,
                    context,
                    endpoint,
                    {
                        "query": term,
                        "tags": "story",
                        "hitsPerPage": hits_per_page,
                        "numericFilters": numeric_filters,
                    },
                    max_items=hits_per_page,
                )
            except (SafeTransportError, SourceParseError):
                failures += 1
                continue
            for hit in hits:
                item = _to_item(hit, spec)
                if item is not None:
                    items.append(item)

        if failures:
            notes.append(f"query_failures:{failures}")
        if capped:
            notes.append(f"query_cap:{max_requests}")
        if exhausted:
            notes.append("time_budget_exhausted")

        if not items and (front_failed or failures):
            return success_result(
                spec,
                (),
                now,
                status_hint="unavailable",
                reason_code="request_failed",
                note=";".join(notes),
            )
        if notes:
            return success_result(
                spec,
                items,
                now,
                status_hint="degraded",
                reason_code=notes[0],
                note=";".join(notes),
            )
        return success_result(spec, items, now)

    def _query(
        self,
        spec: SourceSpec,
        context: SourceContext,
        endpoint: str,
        params: Mapping[str, object],
        *,
        max_items: int,
    ) -> list[Mapping[str, Any]]:
        url = f"{spec.url.rstrip('/')}/{endpoint}?{urlencode(params)}"
        response = context.transport.get(
            spec.id,
            url,
            allowed_mime_types=_JSON_MIME_TYPES,
            user_agent=context.user_agent,
        )
        payload = require_success(response)
        enforce_body_bound(payload, spec)
        document = parse_bounded_json(
            payload,
            max_depth=int(spec.options["max_json_depth"]),
            max_nodes=int(spec.options["max_json_nodes"]),
            max_string_chars=int(spec.options["max_string_chars"]),
        )
        if not isinstance(document, Mapping) or not isinstance(
            document.get("hits"), list
        ):
            raise SourceParseError("malformed_hackernews_response")
        hits = document["hits"]
        if len(hits) > max_items:
            raise SourceParseError("item_limit_exceeded")
        return [row for row in hits if isinstance(row, Mapping)]


def _query_plans(
    context: SourceContext,
    *,
    cutoff: int,
    minimum_points: int,
    include_by_date: bool,
) -> list[tuple[str, str, str]]:
    per_category = [list(dict.fromkeys(query.terms)) for query in context.queries]
    ordered_terms: list[str] = []
    for index in range(max((len(terms) for terms in per_category), default=0)):
        for terms in per_category:
            if index < len(terms):
                ordered_terms.append(terms[index])
    plans: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for term in ordered_terms:
        folded = term.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        plans.append(
            (term, "search", f"created_at_i>{cutoff},points>={minimum_points}")
        )
        if include_by_date:
            plans.append((term, "search_by_date", f"created_at_i>{cutoff}"))
    return plans


def _to_item(
    hit: Mapping[str, Any],
    spec: SourceSpec,
    *,
    native_category: str = "",
    native_rank: int | None = None,
) -> Item | None:
    maximum = int(spec.options["max_string_chars"])
    raw_title = hit.get("title") or hit.get("story_title") or ""
    if not isinstance(raw_title, str):
        return None
    title = clean_title(bounded_text(raw_title, maximum=maximum))
    if not title:
        return None
    created = hit.get("created_at_i")
    if not created or isinstance(created, bool):
        return None
    try:
        published = datetime.fromtimestamp(int(created), tz=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None
    raw_url = hit.get("url") or ""
    if not isinstance(raw_url, str):
        return None
    url = safe_url(bounded_text(raw_url, maximum=maximum))
    if url is None:
        raw_object_id = hit.get("objectID") or ""
        if not isinstance(raw_object_id, (str, int)) or isinstance(raw_object_id, bool):
            return None
        object_id = bounded_text(raw_object_id, maximum=100).strip()
        if not object_id.isdigit():
            return None
        url = f"{HN_ITEM}{object_id}"
    canonical = canonical_url(url)
    if canonical is None:
        return None
    try:
        raw_score = hit.get("points") or 0
        score = int(raw_score) if not isinstance(raw_score, bool) else 0
    except (TypeError, ValueError):
        score = 0
    return Item(
        title=title,
        url=url,
        canonical_url=canonical,
        source_id=spec.id,
        source_name=spec.name,
        platform=spec.platform,
        published_at=published,
        source_weight=spec.weight,
        score=score,
        is_aggregator=True,
        language=spec.language,
        echo_eligible=spec.echo_eligible,
        native_rank=native_rank,
        native_categories={native_category} if native_category else set(),
    )
