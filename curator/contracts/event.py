"""Event contract: the append-only learning-event ledger.

Declarative only. No behavior, no I/O.
Freezes: plan "Event semantics", `learning_events`, `correction_events`, and
SC-11, SC-11A, SC-11B, SC-18.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import (
    ActorKind,
    ConfidenceBand,
    CorrectionAction,
    EvidenceClass,
    EvidenceOrigin,
    EventType,
)


@dataclass(frozen=True)
class LearningEvent:
    """One append-only behavioral record. Never updated in place.

    ``idempotency_key`` is required, not optional, because the human surface and
    the agent API must be able to submit the same intent twice and produce one
    row (SC-18).
    """

    event_id: str
    tenant_id: str
    actor_id: str
    actor_kind: ActorKind
    event_type: EventType
    occurred_at: datetime
    recorded_at: datetime
    surface: str
    idempotency_key: str
    evidence_class: EvidenceClass
    origin: EvidenceOrigin
    confidence: ConfidenceBand
    policy_revision: int
    story_id: str | None = None
    story_cluster_id: str | None = None
    artifact_id: str | None = None
    conversation_id: str | None = None
    session_id: str = ""
    # Dwell and scroll carry duration. It is supporting evidence only and can
    # never on its own mark a story read.
    duration_ms: int | None = None
    retracted_by_event_id: str | None = None


@dataclass(frozen=True)
class EventSemantics:
    """One frozen row of the event-semantics table.

    The initial weights live in the ranking policy revision, not in code. This
    record is what a policy row is validated against: an event type with no
    semantics row is rejected rather than defaulted.
    """

    event_type: EventType
    default_evidence_class: EvidenceClass
    default_confidence: ConfidenceBand
    profile_effect: str
    # Explicit negative feedback lowers matched features. It must never create
    # a global source block, so that outcome is named here as forbidden.
    creates_global_source_block: bool = False
    # True only for events that may independently mark a story consumed.
    can_mark_read: bool = False
    # A weak imported row may rise to medium only through the deterministic
    # corroboration policy.
    promotable_by_corroboration: bool = False


@dataclass(frozen=True)
class CorrectionEvent:
    """An immutable correction or retraction. It never edits the original row."""

    event_id: str
    tenant_id: str
    actor_id: str
    action: CorrectionAction
    target_kind: str
    target_id: str
    reason_code: str
    occurred_at: datetime
    # Every snapshot whose watermark covers the target must be invalidated.
    invalidated_snapshot_ids: tuple[str, ...] = field(default_factory=tuple)
