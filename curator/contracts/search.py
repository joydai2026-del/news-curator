"""Search contract: one tenant-scoped query surface for web, API, and CLI.

Declarative only. No behavior, no I/O.
Freezes: plan "Search contract" and SC-35.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import PublicationClass, SearchOutcome, SearchResultClass


@dataclass(frozen=True)
class SearchQuery:
    """The exact request contract. Web and agent paths submit this shape.

    Identical (principal, query, filters, index_version, policy_revision) must
    yield identical IDs in identical order on every surface, which is why the
    index and policy versions are part of the request rather than ambient.
    """

    tenant_id: str
    principal_id: str
    text: str
    index_version: str
    policy_revision: int
    limit: int
    offset: int = 0
    classes: tuple[SearchResultClass, ...] = field(default_factory=tuple)
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    source_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SearchResult:
    """One hit. ``ordering_key`` is what makes ties stable across surfaces."""

    result_class: SearchResultClass
    canonical_id: str
    tenant_id: str
    publication_class: PublicationClass
    title: str
    match_reason: str
    ordering_key: str
    provenance_ref: str
    score: float


@dataclass(frozen=True)
class SearchResponse:
    """A response is OK or ERROR. A failure is never rendered as an empty list.

    ``outcome`` is required and ``error_code`` is only meaningful when it is
    ERROR, so an index or authorization failure cannot be silently displayed as
    "no results".
    """

    outcome: SearchOutcome
    index_version: str
    policy_revision: int
    total_matched: int
    results: tuple[SearchResult, ...] = field(default_factory=tuple)
    error_code: str = ""
