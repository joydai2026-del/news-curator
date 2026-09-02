"""Bounded, checksummed snapshots of authoritative source collection results.

The snapshot is the handoff between the one network collection and every
downstream consumer. It contains normalized originals and safe health fields,
never credentials, response bodies, or newsletter-derived rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from .models import Item, SourceHealth, TierResult
from .normalize import clean_title, safe_url

if TYPE_CHECKING:
    from .config import Config


SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_RESULTS = 16
# A measured ordinary run already produced 3,741 originals. Keep enough count
# headroom for a busy news cycle while the independent 16 MiB byte ceiling
# remains the hard memory and artifact-size bound.
MAX_ITEMS = 20_000
MAX_HEALTH_ROWS = 1_000
DEFAULT_SOURCE_SNAPSHOT_MAX_AGE_SECONDS = 7_200
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = {
    "schema_version", "generated_at", "configuration_digest",
    "content_digest", "results",
}
_RESULT_KEYS = {"tier", "ok", "note", "items", "source_health"}
_ITEM_KEYS = {
    "title", "url", "canonical_url", "source_id", "source_name",
    "published_at", "platform", "source_weight", "score", "is_aggregator",
    "time_is_estimated", "image_url", "description", "language",
    "echo_eligible", "native_rank", "is_newsletter", "newsletter_sender",
    "echo_platforms", "native_categories", "matched_keywords", "cluster",
}
_HEALTH_KEYS = {
    "source_id", "status", "usable_items", "newest_at", "age_hours",
    "max_age_hours", "language", "source_type", "echo_eligible", "reason_code",
}


class SourceSnapshotError(ValueError):
    """Low-information validation error for an untrusted snapshot."""


@dataclass(frozen=True)
class SourceSnapshot:
    generated_at: datetime
    configuration_digest: str
    content_digest: str
    results: tuple[TierResult, ...]


def snapshot_config_digest(cfg: "Config") -> str:
    """Fingerprint only inputs that affect collection, in stable order."""

    # Imported lazily to avoid making the pipeline and artifact contract depend
    # on one another at module import time.
    from .pipeline import configured_source_specs

    specs = configured_source_specs(cfg)
    rows = []
    for spec in specs:
        rows.append(
            {
                "type": spec.type,
                "id": spec.id,
                "name": spec.name,
                "url": spec.url,
                "enabled": spec.enabled,
                "language": spec.language,
                "category": spec.category,
                "max_age_hours": spec.max_age_hours,
                "weight": spec.weight,
                "is_aggregator": spec.is_aggregator,
                "platform": spec.platform,
                "echo_eligible": spec.echo_eligible,
                "request_timeout_seconds": spec.request_timeout_seconds,
                "max_response_bytes": spec.max_response_bytes,
                "per_host_concurrency": spec.per_host_concurrency,
                "options": _json_value(dict(spec.options)),
            }
        )
    payload = {
        "request_settings": {
            # Keep the global request identity and policy in the digest even
            # when there are no enabled sources. Effective per-source values
            # below intentionally repeat the policy after inheritance.
            "user_agent": _snapshot_user_agent(cfg.user_agent),
            "request_timeout_seconds": cfg.timeout,
            "max_response_bytes": cfg.default_source_max_response_bytes,
            "per_host_concurrency": cfg.default_source_per_host_concurrency,
            "fetch_workers": cfg.fetch_workers,
            "default_source_max_age_hours": cfg.default_source_max_age_hours,
        },
        "sources": rows,
        "queries": [
            {"category_id": category.id, "terms": list(category.search_terms)}
            for category in cfg.categories
            if category.search_terms
        ],
        "default_max_age_hours": cfg.default_source_max_age_hours,
    }
    return hashlib.sha256(_safe_json(payload)).hexdigest()


def write_source_snapshot(
    results: Iterable[TierResult],
    path: Path,
    *,
    generated_at: datetime,
    configuration_digest: str,
) -> Path:
    if generated_at.tzinfo is None:
        raise SourceSnapshotError("snapshot_timestamp")
    if not _DIGEST.fullmatch(configuration_digest):
        raise SourceSnapshotError("snapshot_configuration_digest")
    stable_results = tuple(results)
    if len(stable_results) > MAX_RESULTS:
        raise SourceSnapshotError("snapshot_result_count")
    if sum(len(result.items) for result in stable_results) > MAX_ITEMS:
        raise SourceSnapshotError("snapshot_item_count")
    if sum(len(result.source_health) for result in stable_results) > MAX_HEALTH_ROWS:
        raise SourceSnapshotError("snapshot_health_count")
    rows = [_result_dict(result) for result in stable_results]
    # Validate the exact rows that will be serialized through the same schema
    # used by the loader. This prevents the writer from emitting a snapshot it
    # cannot subsequently load when a mutable Item contains an oversized or
    # otherwise invalid field.
    tuple(_load_result(row) for row in rows)
    unsigned = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": _timestamp(generated_at),
        "configuration_digest": configuration_digest,
        "results": rows,
    }
    payload = {
        **unsigned,
        "content_digest": hashlib.sha256(_safe_json(unsigned)).hexdigest(),
    }
    encoded = _safe_json(payload)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise SourceSnapshotError("snapshot_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return path


def load_source_snapshot(
    path: Path,
    *,
    expected_configuration_digest: str | None = None,
    current_time: datetime | None = None,
    max_age_seconds: int = DEFAULT_SOURCE_SNAPSHOT_MAX_AGE_SECONDS,
) -> SourceSnapshot:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise SourceSnapshotError("snapshot_too_large")
        payload = json.loads(raw.decode("utf-8"))
    except SourceSnapshotError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise SourceSnapshotError("snapshot_unreadable") from None
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        raise SourceSnapshotError("snapshot_schema")
    if payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise SourceSnapshotError("snapshot_schema_version")
    generated_at = _parse_timestamp(payload["generated_at"], "snapshot_timestamp")
    configuration_digest = payload["configuration_digest"]
    content_digest = payload["content_digest"]
    if not isinstance(configuration_digest, str) or not _DIGEST.fullmatch(configuration_digest):
        raise SourceSnapshotError("snapshot_configuration_digest")
    if expected_configuration_digest is not None and configuration_digest != expected_configuration_digest:
        raise SourceSnapshotError("snapshot_configuration_mismatch")
    if not isinstance(content_digest, str) or not _DIGEST.fullmatch(content_digest):
        raise SourceSnapshotError("snapshot_content_digest")
    unsigned = {key: payload[key] for key in _TOP_KEYS if key != "content_digest"}
    if hashlib.sha256(_safe_json(unsigned)).hexdigest() != content_digest:
        raise SourceSnapshotError("snapshot_content_mismatch")
    raw_results = payload["results"]
    if not isinstance(raw_results, list) or len(raw_results) > MAX_RESULTS:
        raise SourceSnapshotError("snapshot_result_count")
    results = tuple(_load_result(row) for row in raw_results)
    if sum(len(result.items) for result in results) > MAX_ITEMS:
        raise SourceSnapshotError("snapshot_item_count")
    if sum(len(result.source_health) for result in results) > MAX_HEALTH_ROWS:
        raise SourceSnapshotError("snapshot_health_count")
    now = current_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise SourceSnapshotError("snapshot_current_time")
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds <= 0
    ):
        raise SourceSnapshotError("snapshot_max_age")
    age_seconds = (
        now.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < 0:
        raise SourceSnapshotError("snapshot_future")
    if age_seconds > max_age_seconds:
        raise SourceSnapshotError("snapshot_stale")
    return SourceSnapshot(
        generated_at=generated_at,
        configuration_digest=configuration_digest,
        content_digest=content_digest,
        results=results,
    )


def _result_dict(result: TierResult) -> dict[str, object]:
    if any(item.is_newsletter for item in result.items):
        raise SourceSnapshotError("snapshot_newsletter_forbidden")
    return {
        "tier": _text(result.tier, 80, "snapshot_tier"),
        "ok": bool(result.ok),
        "note": _text(result.note, 500, "snapshot_note", allow_empty=True),
        "items": [_item_dict(item) for item in result.items],
        "source_health": [_health_dict(row) for row in result.source_health],
    }


def _load_result(raw: object) -> TierResult:
    if not isinstance(raw, dict) or set(raw) != _RESULT_KEYS:
        raise SourceSnapshotError("snapshot_result_schema")
    if not isinstance(raw["ok"], bool):
        raise SourceSnapshotError("snapshot_result_schema")
    items = raw["items"]
    health = raw["source_health"]
    if not isinstance(items, list) or not isinstance(health, list):
        raise SourceSnapshotError("snapshot_result_schema")
    if len(items) > MAX_ITEMS or len(health) > MAX_HEALTH_ROWS:
        raise SourceSnapshotError("snapshot_result_bounds")
    return TierResult(
        tier=_text(raw["tier"], 80, "snapshot_tier"),
        ok=raw["ok"],
        note=_text(raw["note"], 500, "snapshot_note", allow_empty=True),
        items=[_load_item(row) for row in items],
        source_health=[_load_health(row) for row in health],
    )


def _item_dict(item: Item) -> dict[str, object]:
    if item.is_newsletter:
        raise SourceSnapshotError("snapshot_newsletter_forbidden")
    return {
        "title": item.title,
        "url": item.url,
        "canonical_url": item.canonical_url,
        "source_id": item.source_id,
        "source_name": item.source_name,
        "published_at": _timestamp(item.published_at),
        "platform": item.platform,
        "source_weight": item.source_weight,
        "score": item.score,
        "is_aggregator": item.is_aggregator,
        "time_is_estimated": item.time_is_estimated,
        "image_url": item.image_url,
        "description": item.description,
        "language": item.language,
        "echo_eligible": item.echo_eligible,
        "native_rank": item.native_rank,
        "is_newsletter": False,
        "newsletter_sender": "",
        "echo_platforms": sorted(item.echo_platforms),
        "native_categories": sorted(item.native_categories),
        "matched_keywords": list(item.matched_keywords),
        "cluster": list(item.cluster),
    }


def _load_item(raw: object) -> Item:
    if not isinstance(raw, dict) or set(raw) != _ITEM_KEYS:
        raise SourceSnapshotError("snapshot_item_schema")
    if raw["is_newsletter"] is not False or raw["newsletter_sender"] != "":
        raise SourceSnapshotError("snapshot_newsletter_forbidden")
    url = _url(raw["url"], "snapshot_item_url")
    canonical_url = _url(raw["canonical_url"], "snapshot_item_url", allow_empty=True)
    image_url = _url(raw["image_url"], "snapshot_item_image", allow_empty=True)
    score = _optional_int(raw["score"], "snapshot_item_score")
    native_rank = _optional_int(raw["native_rank"], "snapshot_item_rank")
    source_weight = _finite(raw["source_weight"], "snapshot_item_weight")
    for key in ("is_aggregator", "time_is_estimated", "echo_eligible"):
        if not isinstance(raw[key], bool):
            raise SourceSnapshotError("snapshot_item_schema")
    title = clean_title(_text(raw["title"], 2_000, "snapshot_item_title"))
    if not title:
        raise SourceSnapshotError("snapshot_item_title")
    item = Item(
        title=title,
        url=url,
        canonical_url=canonical_url,
        source_id=_text(raw["source_id"], 160, "snapshot_item_source"),
        source_name=_text(raw["source_name"], 200, "snapshot_item_source"),
        published_at=_parse_timestamp(raw["published_at"], "snapshot_item_timestamp"),
        platform=_text(raw["platform"], 80, "snapshot_item_platform"),
        source_weight=source_weight,
        score=score,
        is_aggregator=raw["is_aggregator"],
        time_is_estimated=raw["time_is_estimated"],
        image_url=image_url,
        description=clean_title(_text(raw["description"], 8_000, "snapshot_item_description", allow_empty=True)),
        language=_language(raw["language"]),
        echo_eligible=raw["echo_eligible"],
        native_rank=native_rank,
        echo_platforms=set(_text_list(raw["echo_platforms"], 32, 80, "snapshot_item_echo")),
        native_categories=set(_text_list(raw["native_categories"], 32, 80, "snapshot_item_category")),
        matched_keywords=_text_list(raw["matched_keywords"], 64, 200, "snapshot_item_keyword"),
        cluster=_cluster(raw["cluster"]),
    )
    return item


def _health_dict(row: SourceHealth) -> dict[str, object]:
    return {
        "source_id": row.source_id,
        "status": row.status,
        "usable_items": row.usable_items,
        "newest_at": _timestamp(row.newest_at) if row.newest_at else None,
        "age_hours": row.age_hours,
        "max_age_hours": row.max_age_hours,
        "language": row.language,
        "source_type": row.source_type,
        "echo_eligible": row.echo_eligible,
        "reason_code": row.reason_code,
    }


def _load_health(raw: object) -> SourceHealth:
    if not isinstance(raw, dict) or set(raw) != _HEALTH_KEYS:
        raise SourceSnapshotError("snapshot_health_schema")
    usable = _nonnegative_int(raw["usable_items"], "snapshot_health_items")
    age = None if raw["age_hours"] is None else _nonnegative(raw["age_hours"], "snapshot_health_age")
    newest = None if raw["newest_at"] is None else _parse_timestamp(raw["newest_at"], "snapshot_health_timestamp")
    if not isinstance(raw["echo_eligible"], bool):
        raise SourceSnapshotError("snapshot_health_schema")
    return SourceHealth(
        source_id=_text(raw["source_id"], 160, "snapshot_health_source"),
        status=_text(raw["status"], 80, "snapshot_health_status"),
        usable_items=usable,
        newest_at=newest,
        age_hours=age,
        max_age_hours=_nonnegative(raw["max_age_hours"], "snapshot_health_max_age"),
        language=_language(raw["language"]),
        source_type=_text(raw["source_type"], 48, "snapshot_health_type"),
        echo_eligible=raw["echo_eligible"],
        reason_code=_text(raw["reason_code"], 120, "snapshot_health_reason", allow_empty=True),
    )


def _cluster(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 20:
        raise SourceSnapshotError("snapshot_item_cluster")
    rows = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"source_name", "url"}:
            raise SourceSnapshotError("snapshot_item_cluster")
        rows.append({
            "source_name": _text(raw["source_name"], 200, "snapshot_item_cluster"),
            "url": _url(raw["url"], "snapshot_item_cluster"),
        })
    return rows


def _text_list(value: object, maximum: int, chars: int, code: str) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SourceSnapshotError(code)
    return [_text(entry, chars, code) for entry in value]


def _text(value: object, maximum: int, code: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise SourceSnapshotError(code)
    if (not value and not allow_empty) or any(ord(ch) < 32 for ch in value):
        raise SourceSnapshotError(code)
    return value


def _url(value: object, code: str, *, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return ""
    text = _text(value, 8_192, code)
    if safe_url(text) != text:
        raise SourceSnapshotError(code)
    return text


def _language(value: object) -> str:
    if value not in {"en", "zh"}:
        raise SourceSnapshotError("snapshot_language")
    return str(value)


def _optional_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _integer(value, code)


def _integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceSnapshotError(code)
    return value


def _nonnegative_int(value: object, code: str) -> int:
    number = _integer(value, code)
    if number < 0:
        raise SourceSnapshotError(code)
    return number


def _finite(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceSnapshotError(code)
    number = float(value)
    if not math.isfinite(number):
        raise SourceSnapshotError(code)
    return number


def _nonnegative(value: object, code: str) -> float:
    number = _finite(value, code)
    if number < 0:
        raise SourceSnapshotError(code)
    return number


def _parse_timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise SourceSnapshotError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SourceSnapshotError(code) from None
    if parsed.tzinfo is None:
        raise SourceSnapshotError(code)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise SourceSnapshotError("snapshot_timestamp")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        raise SourceSnapshotError("snapshot_configuration")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 1_000:
        return [_json_value(entry, depth=depth + 1) for entry in value]
    if isinstance(value, Mapping) and len(value) <= 1_000:
        return {
            str(key): _json_value(entry, depth=depth + 1)
            for key, entry in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    raise SourceSnapshotError("snapshot_configuration")


def _snapshot_user_agent(value: object) -> str:
    selected = str(value)
    if (
        not selected
        or len(selected) > 256
        or any(ord(ch) < 32 or ord(ch) > 126 for ch in selected)
    ):
        raise SourceSnapshotError("snapshot_configuration")
    return selected


def _safe_json(payload: object) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").encode("utf-8")
