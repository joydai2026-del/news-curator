"""Stable contracts and resource bounds for source adapters.

The source package stops at normalized ``Item`` records and safe health data.
It does not know about filtering, ranking, translation, or rendering.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence
from xml.parsers import expat

from ..models import Item, SourceHealth
from ..normalize import safe_url
from .errors import SafeTransportError
from .transport import SafeHttpResponse, SafeHttpTransport

if TYPE_CHECKING:
    from .registry import SourceRegistry


_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_SOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_COMMON_KEYS = frozenset(
    {
        "type",
        "id",
        "name",
        "url",
        "enabled",
        "language",
        "category",
        "max_age_hours",
        "weight",
        "aggregator",
        "is_aggregator",
        "platform",
        "echo_eligible",
        "request_timeout_seconds",
        "max_response_bytes",
        "per_host_concurrency",
        "options",
    }
)


class SourceValidationError(ValueError):
    """A safe configuration error that never contains credentials or bodies."""


class SourceParseError(ValueError):
    """A safe parser failure represented by a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SourceSpec:
    """Validated common source fields plus adapter-owned validated options."""

    type: str
    id: str
    name: str
    url: str
    enabled: bool = True
    language: str = "en"
    category: str = ""
    max_age_hours: float = 48.0
    weight: float = 1.0
    is_aggregator: bool = False
    platform: str = ""
    echo_eligible: bool = True
    request_timeout_seconds: float = 15.0
    max_response_bytes: int = 8 * 1024 * 1024
    per_host_concurrency: int = 4
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not _SOURCE_TYPE.fullmatch(self.type):
            raise SourceValidationError("source type is invalid")
        if not _SOURCE_ID.fullmatch(self.id):
            raise SourceValidationError("source id is invalid")
        if (
            not self.name
            or len(self.name) > 200
            or any(ord(ch) < 32 for ch in self.name)
        ):
            raise SourceValidationError(
                f"source {self.id}: name is required and must be at most 200 characters"
            )
        if safe_url(self.url) != self.url or len(self.url) > 8192:
            raise SourceValidationError(
                f"source {self.id}: url must be an absolute http(s) URL without userinfo"
            )
        if not isinstance(self.enabled, bool):
            raise SourceValidationError(f"source {self.id}: enabled must be a boolean")
        if not _LANGUAGE.fullmatch(self.language):
            raise SourceValidationError(f"source {self.id}: language is invalid")
        if len(self.category) > 80 or any(ord(ch) < 32 for ch in self.category):
            raise SourceValidationError(f"source {self.id}: category is invalid")
        _positive_number(self.max_age_hours, self.id, "max_age_hours")
        _finite_number(self.weight, self.id, "weight")
        if not isinstance(self.is_aggregator, bool) or not isinstance(
            self.echo_eligible, bool
        ):
            raise SourceValidationError(
                f"source {self.id}: aggregator and echo_eligible must be booleans"
            )
        if not self.platform:
            object.__setattr__(self, "platform", self.id)
        if len(self.platform) > 80 or any(ord(ch) < 32 for ch in self.platform):
            raise SourceValidationError(f"source {self.id}: platform is invalid")
        _positive_number(
            self.request_timeout_seconds, self.id, "request_timeout_seconds"
        )
        if not isinstance(self.max_response_bytes, int) or isinstance(
            self.max_response_bytes, bool
        ):
            raise SourceValidationError(
                f"source {self.id}: max_response_bytes must be an integer"
            )
        if not 1 <= self.max_response_bytes <= 8 * 1024 * 1024:
            raise SourceValidationError(
                f"source {self.id}: max_response_bytes must be between 1 and 8388608"
            )
        if not isinstance(self.per_host_concurrency, int) or isinstance(
            self.per_host_concurrency, bool
        ):
            raise SourceValidationError(
                f"source {self.id}: per_host_concurrency must be an integer"
            )
        if not 1 <= self.per_host_concurrency <= 16:
            raise SourceValidationError(
                f"source {self.id}: per_host_concurrency must be between 1 and 16"
            )
        if not isinstance(self.options, Mapping):
            raise SourceValidationError(f"source {self.id}: options must be a mapping")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SourceSpec":
        if not isinstance(raw, Mapping):
            raise SourceValidationError("source entry must be a mapping")
        unknown = set(raw) - _COMMON_KEYS
        if unknown:
            raise SourceValidationError(
                f"source entry has unknown common fields: {', '.join(sorted(unknown))}"
            )
        source_id = _required_text(raw.get("id"), "source id", 80)
        source_type = _required_text(raw.get("type"), f"source {source_id}: type", 48)
        if source_type == "hacker_news":
            source_type = "hackernews"
        name = _required_text(
            raw.get("name") or source_id, f"source {source_id}: name", 200
        )
        url = _required_text(raw.get("url"), f"source {source_id}: url", 8192)
        aggregator = raw.get("is_aggregator", raw.get("aggregator", False))
        if (
            "aggregator" in raw
            and "is_aggregator" in raw
            and raw["aggregator"] != raw["is_aggregator"]
        ):
            raise SourceValidationError(
                f"source {source_id}: aggregator and is_aggregator disagree"
            )
        platform = _optional_text(raw.get("platform"), 80) or source_id
        options = raw.get("options", {})
        if options is None:
            options = {}
        return cls(
            type=source_type,
            id=source_id,
            name=name,
            url=url,
            enabled=raw.get("enabled", True),
            language=_optional_text(raw.get("language"), 40) or "en",
            category=_optional_text(raw.get("category"), 80),
            max_age_hours=_number(
                raw.get("max_age_hours", 48.0), source_id, "max_age_hours"
            ),
            weight=_number(raw.get("weight", 1.0), source_id, "weight"),
            is_aggregator=aggregator,
            platform=platform,
            echo_eligible=raw.get("echo_eligible", True),
            request_timeout_seconds=_number(
                raw.get("request_timeout_seconds", 15.0),
                source_id,
                "request_timeout_seconds",
            ),
            max_response_bytes=_integer(
                raw.get("max_response_bytes", 8 * 1024 * 1024),
                source_id,
                "max_response_bytes",
            ),
            per_host_concurrency=_integer(
                raw.get("per_host_concurrency", 4), source_id, "per_host_concurrency"
            ),
            options=options,
        )

    def with_options(self, options: Mapping[str, Any]) -> "SourceSpec":
        return replace(self, options=MappingProxyType(dict(options)))


@dataclass(frozen=True)
class SourceQuery:
    """Typed category/query context for adapters such as Hacker News."""

    category_id: str
    terms: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.category_id or len(self.category_id) > 80:
            raise SourceValidationError("query category id is invalid")
        cleaned = tuple(_required_text(term, "query term", 200) for term in self.terms)
        object.__setattr__(self, "terms", cleaned)


EnvironmentReader = Callable[[str], str | None]
UtcClock = Callable[[], datetime]


@dataclass(frozen=True)
class SourceContext:
    """All runtime capabilities are injected. Nothing registers globally."""

    registry: "SourceRegistry"
    transport: SafeHttpTransport
    clock: UtcClock
    environment: EnvironmentReader
    # One validated application identity for every built-in source request.
    # Adapters pass it to SafeHttpTransport, which applies the final wire-level
    # validation and never lets it override a caller-supplied header.
    user_agent: str | None = None
    queries: tuple[SourceQuery, ...] = ()
    default_max_age_hours: float = 48.0
    durable_store: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", tuple(self.queries))
        if any(not isinstance(query, SourceQuery) for query in self.queries):
            raise SourceValidationError("source queries must be SourceQuery values")
        _positive_number(self.default_max_age_hours, "context", "default_max_age_hours")
        if self.user_agent is not None and (
            not self.user_agent
            or len(self.user_agent) > 256
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in self.user_agent)
        ):
            raise SourceValidationError("source user agent is invalid")

    def now(self) -> datetime:
        current = self.clock()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise SourceValidationError(
                "source clock must return a timezone-aware datetime"
            )
        return current.astimezone(timezone.utc)


@dataclass(frozen=True)
class SourceResult:
    """Exactly one configured source's normalized items and health record."""

    source_id: str
    items: tuple[Item, ...]
    health: SourceHealth
    note: str = ""

    def __post_init__(self) -> None:
        if self.health.source_id != self.source_id:
            raise ValueError("source result and health ids must match")
        if any(item.source_id != self.source_id for item in self.items):
            raise ValueError("source result items must belong to the configured source")


class SourceAdapter(Protocol):
    type_key: str

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]: ...

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult: ...


def success_result(
    spec: SourceSpec,
    items: Sequence[Item],
    now: datetime,
    *,
    status_hint: str = "",
    reason_code: str = "",
    note: str = "",
) -> SourceResult:
    stable_items = tuple(items)
    return SourceResult(
        source_id=spec.id,
        items=stable_items,
        health=_health(
            spec, stable_items, now, status=status_hint, reason_code=reason_code
        ),
        note=note,
    )


def guarded_fetch(
    adapter: SourceAdapter, spec: SourceSpec, context: SourceContext
) -> SourceResult:
    """Convert one adapter failure into one low-information source result."""
    if not spec.enabled:
        now = context.now()
        return SourceResult(
            spec.id,
            (),
            _health(spec, (), now, status="disabled", reason_code="disabled_by_config"),
            "disabled by config",
        )
    try:
        result = adapter.fetch(spec, context)
    except SafeTransportError as exc:
        return SourceResult(
            spec.id,
            (),
            _health(
                spec,
                (),
                context.now(),
                status="unavailable",
                reason_code=exc.reason_code,
            ),
            exc.reason_code,
        )
    except SourceParseError as exc:
        status = (
            "malformed" if exc.reason_code.startswith("malformed") else "unavailable"
        )
        return SourceResult(
            spec.id,
            (),
            _health(
                spec, (), context.now(), status=status, reason_code=exc.reason_code
            ),
            exc.reason_code,
        )
    except Exception:
        return SourceResult(
            spec.id,
            (),
            _health(
                spec,
                (),
                context.now(),
                status="unavailable",
                reason_code="adapter_failed",
            ),
            "adapter_failed",
        )
    if result.source_id != spec.id or result.health.source_id != spec.id:
        return SourceResult(
            spec.id,
            (),
            _health(
                spec,
                (),
                context.now(),
                status="unavailable",
                reason_code="invalid_adapter_result",
            ),
            "invalid_adapter_result",
        )
    return result


def require_success(response: SafeHttpResponse) -> bytes:
    if not 200 <= response.status_code < 300:
        raise SourceParseError("http_status_error")
    return response.body


def enforce_body_bound(payload: bytes, spec: SourceSpec) -> None:
    if len(payload) > spec.max_response_bytes:
        raise SourceParseError("response_too_large")


def parse_bounded_json(
    payload: bytes,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_chars: int,
) -> Any:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourceParseError("malformed_json") from None
    nodes = 0
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise SourceParseError("json_node_limit_exceeded")
        if depth > max_depth:
            raise SourceParseError("json_depth_limit_exceeded")
        if isinstance(value, str):
            if len(value) > max_string_chars:
                raise SourceParseError("string_limit_exceeded")
        elif isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > max_string_chars:
                    raise SourceParseError("string_limit_exceeded")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return data


def enforce_xml_bounds(
    payload: bytes,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_chars: int,
    enforce_text_limit: bool = True,
) -> None:
    upper = payload.upper()
    null_stripped_upper = upper.replace(b"\x00", b"")
    if (
        b"<!DOCTYPE" in upper
        or b"<!ENTITY" in upper
        or b"<!DOCTYPE" in null_stripped_upper
        or b"<!ENTITY" in null_stripped_upper
    ):
        raise SourceParseError("malformed_xml_dtd_or_entity")

    parser = expat.ParserCreate()
    depth = 0
    nodes = 0
    current_text = 0

    def start(_name: str, attrs: Mapping[str, str]) -> None:
        nonlocal depth, nodes, current_text
        depth += 1
        nodes += 1
        current_text = 0
        if depth > max_depth:
            raise SourceParseError("xml_depth_limit_exceeded")
        if nodes > max_nodes:
            raise SourceParseError("xml_node_limit_exceeded")
        if any(
            len(key) > max_string_chars or len(value) > max_string_chars
            for key, value in attrs.items()
        ):
            raise SourceParseError("string_limit_exceeded")

    def end(_name: str) -> None:
        nonlocal depth, current_text
        depth -= 1
        current_text = 0

    def text(value: str) -> None:
        nonlocal current_text
        if not enforce_text_limit:
            return
        current_text += len(value)
        if current_text > max_string_chars:
            raise SourceParseError("string_limit_exceeded")

    def reject_declaration(*_args: object) -> None:
        # Parser callbacks work after character decoding, so UTF-16/UTF-32
        # declarations cannot bypass the no-DTD/no-entity boundary.
        raise SourceParseError("malformed_xml_dtd_or_entity")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.UnparsedEntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    try:
        parser.Parse(payload, True)
    except SourceParseError:
        raise
    except expat.ExpatError:
        # Expat stops at the first syntax error. Continuing with a tolerant
        # parser would leave the unparsed suffix outside the node, depth, and
        # attribute checks above, so malformed XML is always fail-closed.
        raise SourceParseError("malformed_xml") from None


def validate_option_keys(spec: SourceSpec, allowed: set[str]) -> dict[str, Any]:
    unknown = set(spec.options) - allowed
    if unknown:
        raise SourceValidationError(
            f"source {spec.id}: unknown {spec.type} options: {', '.join(sorted(unknown))}"
        )
    return dict(spec.options)


def option_int(
    options: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    source_id: str,
) -> int:
    value = _integer(options.get(key, default), source_id, f"options.{key}")
    if not minimum <= value <= maximum:
        raise SourceValidationError(
            f"source {source_id}: options.{key} must be between {minimum} and {maximum}"
        )
    return value


def option_float(
    options: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    source_id: str,
) -> float:
    value = _number(options.get(key, default), source_id, f"options.{key}")
    if not minimum <= value <= maximum:
        raise SourceValidationError(
            f"source {source_id}: options.{key} must be between {minimum:g} and {maximum:g}"
        )
    return value


def option_bool(
    options: Mapping[str, Any], key: str, default: bool, *, source_id: str
) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise SourceValidationError(
            f"source {source_id}: options.{key} must be a boolean"
        )
    return value


def bounded_text(
    value: object, *, maximum: int, reason_code: str = "string_limit_exceeded"
) -> str:
    text = str(value or "")
    if len(text) > maximum:
        raise SourceParseError(reason_code)
    return text


def _health(
    spec: SourceSpec,
    items: Sequence[Item],
    now: datetime,
    *,
    status: str = "",
    reason_code: str = "",
) -> SourceHealth:
    newest = max((item.published_at for item in items), default=None)
    age = max(0.0, (now - newest).total_seconds() / 3600.0) if newest else None
    if newest is None:
        if status not in {"disabled", "unavailable", "malformed"}:
            status = "empty"
            reason_code = reason_code or "no_usable_items"
    elif age is not None and age > spec.max_age_hours:
        status = "stale"
        reason_code = "newest_item_too_old"
    elif not status:
        status = "fresh"
    return SourceHealth(
        source_id=spec.id,
        status=status,
        usable_items=len(items),
        newest_at=newest,
        age_hours=age,
        max_age_hours=spec.max_age_hours,
        language=spec.language,
        source_type=spec.type,
        echo_eligible=spec.echo_eligible,
        reason_code=reason_code,
    )


def _required_text(value: object, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(ch) < 32 for ch in text):
        raise SourceValidationError(
            f"{label} is required and must be at most {maximum} characters"
        )
    return text


def _optional_text(value: object, maximum: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > maximum or any(ord(ch) < 32 for ch in text):
        raise SourceValidationError(f"text value must be at most {maximum} characters")
    return text


def _number(value: object, source_id: str, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SourceValidationError(
            f"source {source_id}: {label} must be a number"
        ) from None
    if not math.isfinite(number):
        raise SourceValidationError(f"source {source_id}: {label} must be finite")
    return number


def _integer(value: object, source_id: str, label: str) -> int:
    if isinstance(value, bool):
        raise SourceValidationError(f"source {source_id}: {label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SourceValidationError(
            f"source {source_id}: {label} must be an integer"
        ) from None
    if isinstance(value, float) and not value.is_integer():
        raise SourceValidationError(f"source {source_id}: {label} must be an integer")
    return number


def _positive_number(value: object, source_id: str, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise SourceValidationError(
            f"source {source_id}: {label} must be a finite positive number"
        )


def _finite_number(value: object, source_id: str, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise SourceValidationError(
            f"source {source_id}: {label} must be a finite number"
        )
