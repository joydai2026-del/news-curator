"""Receipt contract: the common envelope plus the receipts with no other home.

Declarative only. No behavior, no I/O.
Freezes: plan `ranking_receipts`, `deletion_receipts`, the Cloudflare budget
tripwire receipt, and the SC-11A inventory receipt. Mirror and output receipts
live in their own contracts and carry the same envelope fields.

Covers SC-05, SC-08, SC-11A, SC-28.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .candidate import BandResult, ScoredCandidate, SlateEntry
from .enums import BandVerdict, Lane, ReceiptState


@dataclass(frozen=True)
class ReceiptEnvelope:
    """Fields every receipt in the system carries, whatever it proves."""

    receipt_id: str
    tenant_id: str
    kind: str
    state: ReceiptState
    created_at: datetime
    policy_revision: int
    actor_id: str = ""
    reason_code: str = ""
    settled_at: datetime | None = None


@dataclass(frozen=True)
class RankingReceipt:
    """The reproducible record of one ranking run.

    SC-08: the transparent score set is present and authoritative in EVERY
    settled receipt. A shadow set may be stored, marked non-authoritative, and
    can never have produced the final order.

    SC-24: primary lane assignment must replay from ``lane_scores``,
    ``lane_priority``, and story id alone, with no generator rerun.
    """

    envelope: ReceiptEnvelope
    run_id: str
    edition_date: str
    profile_snapshot_id: str | None
    profile_version: int | None
    pre_rank_candidate_ids: tuple[str, ...]
    lane_scores: tuple[tuple[str, Lane, float], ...]
    lane_priority: tuple[Lane, ...]
    primary_lane_by_story: tuple[tuple[str, Lane], ...]
    secondary_lane_reasons: tuple[tuple[str, Lane, str], ...]
    transparent_scores: tuple[ScoredCandidate, ...]
    final_order: tuple[SlateEntry, ...]
    bands: tuple[BandResult, ...]
    lane_quotas: tuple[tuple[Lane, int], ...]
    verifier_verdict: BandVerdict
    shadow_scores: tuple[ScoredCandidate, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProjectionResolution:
    """One derived projection's resolution during a deletion.

    An unresolved row forces the whole deletion receipt to PARTIAL. A green
    receipt that leaves content sitting on an external target is a false
    deletion, so an external copy that cannot be retracted is recorded as a
    user-visible disclosure rather than quietly omitted.
    """

    projection: str
    resolved: bool
    resolution_kind: str
    target_ref: str = ""
    user_visible_disclosure: str = ""


@dataclass(frozen=True)
class DeletionReceipt:
    """Proves effective removal without erasing the audit chain."""

    envelope: ReceiptEnvelope
    target_kind: str
    target_ids: tuple[str, ...]
    correction_watermark: datetime
    invalidated_snapshot_ids: tuple[str, ...]
    rebuild_id: str
    zero_contribution_verdict: bool
    projections: tuple[ProjectionResolution, ...]
    mirrored_targets: tuple[str, ...] = field(default_factory=tuple)
    audit_chain_queryable: bool = True


@dataclass(frozen=True)
class MeterReading:
    """One host meter. Missing or stale data is unknown, never zero."""

    meter: str
    # Cumulative budgets can be shed. Per-invocation ceilings cannot: shedding
    # never rescues a single invocation that exceeds a runtime hard limit.
    meter_kind: str
    value: float | None
    unit: str
    freshness_verdict: str
    sampled_at: datetime | None = None
    warning_threshold: float | None = None
    hard_stop_threshold: float | None = None
    breached: bool = False


@dataclass(frozen=True)
class LimitReceipt:
    """One pilot day's host-budget record."""

    envelope: ReceiptEnvelope
    meter_source: str
    attributed_operation_class: str
    readings: tuple[MeterReading, ...]
    shed_actions: tuple[str, ...] = field(default_factory=tuple)
    final_state: str = ""


@dataclass(frozen=True)
class ImportInventoryReceipt:
    """SC-11A / SC-40: a source stays import-disabled without a complete receipt."""

    envelope: ReceiptEnvelope
    source_kind: str
    credential_verified: bool
    coverage_window_start: datetime | None
    coverage_window_end: datetime | None
    sampled_record_count: int
    available_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    evidence_grade: str
    import_enabled: bool = False
