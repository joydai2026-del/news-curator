"""Evidence contract: raw imports and normalized provenance-linked evidence.

Declarative only. No behavior, no I/O.
Freezes: plan "Core records" (`raw_imports`, `evidence_items`), the data-source
policy table, and SC-02, SC-03, SC-04, SC-21.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import (
    ConfidenceBand,
    EvidenceClass,
    EvidenceOrigin,
    RetentionState,
)


@dataclass(frozen=True)
class RawImport:
    """A restricted snapshot of one imported source file. Never a profile input.

    Import writes this and changes no profile state. Idempotency is by
    (tenant_id, source_kind, checksum) plus source_item_id downstream, so a
    re-import of the same bytes creates zero duplicate evidence.
    """

    raw_import_id: str
    tenant_id: str
    owner_actor_id: str
    # A source KIND, never a provider brand: "newsletter_archive",
    # "assistant_chat_export", "browser_history", "mailbox_state", "url_list".
    source_kind: str
    checksum: str
    schema_version: str
    storage_reference: str
    imported_at: datetime
    consent_version: str
    retention_state: RetentionState
    exported_at: datetime | None = None
    byte_size: int = 0


@dataclass(frozen=True)
class EvidenceItem:
    """One normalized, provenance-linked observation.

    ``evidence_class`` is SC-04's four-value axis. ``origin`` is a separate axis
    so imported history can never masquerade as a live explicit action, and
    ``confidence`` carries the strong/medium/weak strength the ranking policy
    reads.
    """

    evidence_id: str
    tenant_id: str
    raw_import_id: str | None
    source_item_id: str
    occurred_at: datetime
    recorded_at: datetime
    evidence_class: EvidenceClass
    origin: EvidenceOrigin
    confidence: ConfidenceBand
    # Weight is read from the active policy revision, and persisted here so a
    # past snapshot stays reproducible after the policy changes.
    weight: float
    policy_revision: int
    story_id: str | None = None
    canonical_url: str = ""
    entity_ids: tuple[str, ...] = field(default_factory=tuple)
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    # True only after the deterministic corroboration policy promoted a weak
    # imported row. Never set at import time.
    corroborated: bool = False
    corroborating_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    # Set when a correction or retraction event supersedes this row. The row
    # itself is never edited or deleted.
    retracted_by_event_id: str | None = None


@dataclass(frozen=True)
class ProfileSnapshot:
    """A rebuildable derived state. Ranking reads exactly one settled snapshot.

    A partial or failed rebuild never replaces the last settled snapshot
    (SC-06), which is why ``settled_at`` is required rather than nullable: an
    unsettled build is not a snapshot row at all.
    """

    snapshot_id: str
    tenant_id: str
    version: int
    evidence_watermark: datetime
    build_version: str
    policy_revision: int
    settled_at: datetime
    # Weighted entries, not bare strings: (feature_id, weight, provenance).
    # A bare string can record THAT a topic is an affinity and nothing about
    # how strongly, so `less_like_this` and `more_like_this` would land in
    # the same place and decay would have no numeric input to read.
    # `provenance` is a stable reference (an evidence id, or a comma-joined
    # list of them) so the weight is traceable to the events that produced it.
    topic_affinities: tuple[tuple[str, float, str], ...] = field(default_factory=tuple)
    entity_affinities: tuple[tuple[str, float, str], ...] = field(default_factory=tuple)
    source_affinities: tuple[tuple[str, float, str], ...] = field(default_factory=tuple)
    knowledge_gaps: tuple[str, ...] = field(default_factory=tuple)
    novelty_tolerance: float = 0.0
