"""Canonical retained candidates, independent of a rendered edition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

from .config import Category
from .dedup import dedupe
from .filter import topic_match
from .identity import story_id_for_item
from .models import Item
from .normalize import fold_text


@dataclass(frozen=True)
class RetainedCandidate:
    story_id: str
    item: Item
    observed_at: datetime
    category_ids: frozenset[str]


def retain(items: Iterable[Item], *, categories: Iterable[Category], observed_at: datetime) -> tuple[RetainedCandidate, ...]:
    """Normalize through the existing deduper, then keep every canonical item.

    This deliberately runs before publication selection. A category assignment is
    recomputed from configured terms so retained search and feed candidates use
    the same inclusion rule as collection.
    """
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    configured = tuple(categories)
    retained: dict[str, RetainedCandidate] = {}
    for item in dedupe(list(items)):
        ids = frozenset(c.id for c in configured if topic_match(item, c) is not None)
        story_id = story_id_for_item(item)
        if story_id in retained:
            # M1 retains language variants. The corpus has one durable row per
            # URL identity; coalesce here without changing M1 display behavior.
            retained[story_id] = replace(retained[story_id],
                category_ids=retained[story_id].category_ids | ids)
        else:
            retained[story_id] = RetainedCandidate(
                story_id=story_id, item=item,
                observed_at=observed_at.astimezone(timezone.utc), category_ids=ids)
    return tuple(sorted(retained.values(), key=lambda row: (row.item.published_at, row.story_id), reverse=True))


def candidates(rows: Iterable[RetainedCandidate], *, category_id: str | None = None, query: str | None = None) -> tuple[RetainedCandidate, ...]:
    """Return retained main/category/search candidates without a page-size cap.

    Search uses Unicode folded substring matching. This keeps configured Chinese
    text searchable without pretending an English tokenizer understands CJK.
    Ranking and pagination are separate request-time responsibilities.
    """
    query_folded = fold_text(query or "").casefold()
    result = []
    for row in rows:
        if category_id is not None and category_id not in row.category_ids:
            continue
        haystack = fold_text(f"{row.item.title}\n{row.item.description}").casefold()
        if query_folded and query_folded not in haystack:
            continue
        result.append(row)
    return tuple(result)


def candidate_response(rows: Iterable[RetainedCandidate]) -> dict[str, object]:
    """Versioned adapter payload for the new M2 service, never an M1 card."""
    return {
        "schema_version": 1,
        "candidates": [
            {
                "story_id": row.story_id,
                "title": row.item.title,
                "summary": row.item.description,
                "language": row.item.language,
                "canonical_url": row.item.canonical_url,
                "source_id": row.item.source_id,
                "source_name": row.item.source_name,
                "published_at": row.item.published_at.astimezone(timezone.utc).isoformat(),
                "source_observed_at": row.observed_at.isoformat(),
                "category_ids": sorted(row.category_ids),
            }
            for row in rows
        ],
    }


def public_ingest_rows(rows: Iterable[RetainedCandidate], *, allowed_source_ids: set[str]) -> list[dict[str, object]]:
    """The only artifact shape accepted by the public-only ingest RPC."""
    result = []
    for row in rows:
        if row.item.is_newsletter:
            raise ValueError("newsletter items cannot enter retained corpus")
        if row.item.source_id not in allowed_source_ids:
            raise ValueError("item is not an eligible public source")
        if not isinstance(row.item.is_aggregator, bool):
            raise ValueError("item aggregator attribution must be boolean")
        result.append({
            "story_id": row.story_id, "origin_class": "public_outlet", "source_kind": "outlet", "canonical_url": row.item.canonical_url,
            "title": row.item.title, "summary": row.item.description,
            "language": row.item.language, "source_id": row.item.source_id,
            "source_name": row.item.source_name,
            "source_is_aggregator": row.item.is_aggregator,
            "published_at": row.item.published_at.astimezone(timezone.utc).isoformat(),
            "source_observed_at": row.observed_at.isoformat(),
            "category_ids": sorted(row.category_ids),
        })
    return result
