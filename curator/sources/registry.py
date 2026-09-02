"""Injected allowlisted source registry and deterministic collection."""

from __future__ import annotations

import concurrent.futures as futures
from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from .base import (
    SourceAdapter,
    SourceContext,
    SourceResult,
    SourceSpec,
    SourceValidationError,
    guarded_fetch,
)
from .transport import SafeHttpPolicy, SafeHttpTransport


class SourceRegistry:
    """An immutable per-composition adapter allowlist.

    There is deliberately no module-level registry and no ``register`` method.
    Tests and production each receive a fresh instance.
    """

    def __init__(
        self, adapters: Mapping[str, SourceAdapter] | Iterable[SourceAdapter]
    ) -> None:
        resolved: dict[str, SourceAdapter] = {}
        if isinstance(adapters, Mapping):
            entries = adapters.items()
        else:
            entries = ((adapter.type_key, adapter) for adapter in adapters)
        for raw_key, adapter in entries:
            key = str(raw_key)
            if key in resolved:
                raise SourceValidationError(f"duplicate source adapter type: {key}")
            if getattr(adapter, "type_key", key) != key:
                raise SourceValidationError(f"source adapter key mismatch: {key}")
            resolved[key] = adapter
        if not resolved:
            raise SourceValidationError("source registry needs at least one adapter")
        self._adapters = MappingProxyType(resolved)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def adapter_for(self, type_key: str) -> SourceAdapter:
        try:
            return self._adapters[type_key]
        except KeyError:
            raise SourceValidationError(
                f"source type is not allowlisted: {type_key}"
            ) from None

    def parse_spec(self, raw: Mapping[str, Any]) -> SourceSpec:
        spec = SourceSpec.from_mapping(raw)
        adapter = self.adapter_for(spec.type)
        return spec.with_options(adapter.validate_options(spec))

    def parse_specs(self, rows: Iterable[Mapping[str, Any]]) -> tuple[SourceSpec, ...]:
        specs = tuple(self.parse_spec(row) for row in rows)
        seen: set[str] = set()
        duplicates: set[str] = set()
        for spec in specs:
            if spec.id in seen:
                duplicates.add(spec.id)
            seen.add(spec.id)
        if duplicates:
            raise SourceValidationError(
                f"source ids must be globally unique: {', '.join(sorted(duplicates))}"
            )
        return specs

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        scoped_context = context
        if isinstance(context.transport, SafeHttpTransport):
            base = context.transport.policy
            timeout = min(spec.request_timeout_seconds, base.total_timeout_seconds)
            response_bytes = min(
                spec.max_response_bytes, base.max_wire_bytes, base.max_decoded_bytes
            )
            host_concurrency = min(
                spec.per_host_concurrency, base.per_host_concurrency
            )
            policy = SafeHttpPolicy(
                total_timeout_seconds=timeout,
                max_wire_bytes=response_bytes,
                max_decoded_bytes=response_bytes,
                max_request_bytes=base.max_request_bytes,
                max_header_bytes=base.max_header_bytes,
                max_redirects=base.max_redirects,
                max_content_encodings=base.max_content_encodings,
                per_host_concurrency=host_concurrency,
                read_chunk_bytes=min(base.read_chunk_bytes, response_bytes),
            )
            scoped_context = replace(
                context, transport=context.transport.with_policy(policy)
            )
        return guarded_fetch(self.adapter_for(spec.type), spec, scoped_context)


def collect_sources(
    specs: Iterable[SourceSpec],
    context: SourceContext,
    *,
    max_workers: int = 8,
) -> tuple[SourceResult, ...]:
    """Fetch independently and return results in configured order."""

    ordered = tuple(specs)
    if not ordered:
        return ()
    if len({spec.id for spec in ordered}) != len(ordered):
        raise SourceValidationError("source ids must be globally unique")
    workers = max(1, min(16, int(max_workers), len(ordered)))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # executor.map yields in input order even when work completes out of
        # order. guarded_fetch contains every source failure inside its result.
        return tuple(
            pool.map(lambda spec: context.registry.fetch(spec, context), ordered)
        )


def build_builtin_registry() -> SourceRegistry:
    """Create one fresh built-in allowlist for a production composition."""

    from .feed import AtomAdapter, FeedAdapter, RssAdapter
    from .hackernews import HackerNewsAdapter
    from .json_feed import JsonFeedAdapter
    from .news_sitemap import NewsSitemapAdapter

    return SourceRegistry(
        (
            FeedAdapter(),
            RssAdapter(),
            AtomAdapter(),
            NewsSitemapAdapter(),
            JsonFeedAdapter(),
            HackerNewsAdapter(),
        )
    )
