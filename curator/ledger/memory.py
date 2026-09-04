"""Deterministic, thread-safe in-memory learning ledger for tests and dry runs.

Mirrors the append-only and idempotency rules the migration enforces in SQL
(supabase/migrations/202609020001_learning_ledger.sql), so the two surfaces
cannot silently drift: an event store that is append-only in the database but
mutable in the reference implementation would let tests pass against
behavior the real store forbids.
"""

from __future__ import annotations

import threading

from datetime import datetime

from curator.contracts.enums import CorrectionAction
from curator.contracts.event import CorrectionEvent, LearningEvent
from curator.contracts.evidence import EvidenceItem
from curator.contracts.receipt import DeletionReceipt
from curator.ledger.ownership import ownership_violations


class LedgerError(Exception):
    """Raised when a caller attempts something the ledger contract forbids."""


def _reject_bad_ownership(record: object, *, label: str) -> None:
    """Every write path recomputes the ownership rule; none of them trusts it.

    The contract package is declarative, so nothing stops a caller from
    constructing an owned record with a blank actor or a missing subject. This
    is the gate that stops such a record being STORED.
    """
    problems = ownership_violations(record)
    if problems:
        raise LedgerError(f"{label}: " + "; ".join(problems))


class InMemoryLedgerStore:
    """Reference implementation with the same append-only guarantees as SQL."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Rows keyed by their own id. Insertion order is preserved by dict
        # ordering, which effective_events relies on for deterministic output.
        self._events: dict[str, LearningEvent] = {}
        # Keyed on (tenant_id, user_id, idempotency_key), decided 2026-09-02.
        # A tenant-scoped key alone let a same-tenant collision on the SAME
        # idempotency_key text but a DIFFERENT user_id return the OTHER user's
        # row: bob posting with alice's key silently got alice's event handed
        # back, and bob's own event was dropped on the floor. A client may
        # reuse the same key text across different users safely, because the
        # identity is scoped per user (see docs/contracts/event.md).
        self._idempotency_index: dict[tuple[str, str, str], str] = {}
        self._corrections: dict[str, CorrectionEvent] = {}
        self._evidence: dict[str, EvidenceItem] = {}
        self._deletion_receipts: dict[str, DeletionReceipt] = {}

    def append_event(self, event: LearningEvent) -> LearningEvent:
        if type(event) is not LearningEvent:
            raise LedgerError(
                f"learning event: unknown record type {type(event).__name__}; "
                "expected the exact frozen LearningEvent class"
            )
        _reject_bad_ownership(event, label="learning event")
        with self._lock:
            idem_key = (event.tenant_id, event.user_id, event.idempotency_key)
            existing_id = self._idempotency_index.get(idem_key)
            if existing_id is not None:
                return self._events[existing_id]
            if event.event_id in self._events:
                raise LedgerError(f"event_id already recorded: {event.event_id}")
            self._events[event.event_id] = event
            self._idempotency_index[idem_key] = event.event_id
            return event

    def append_correction(self, correction: CorrectionEvent) -> CorrectionEvent:
        _reject_bad_ownership(correction, label="correction event")
        with self._lock:
            if correction.event_id in self._corrections:
                raise LedgerError(
                    f"correction event_id already recorded: {correction.event_id}"
                )
            self._corrections[correction.event_id] = correction
            return correction

    def append_evidence(self, evidence: EvidenceItem) -> EvidenceItem:
        _reject_bad_ownership(evidence, label="evidence item")
        with self._lock:
            if evidence.evidence_id in self._evidence:
                raise LedgerError(
                    f"evidence_id already recorded: {evidence.evidence_id}"
                )
            self._evidence[evidence.evidence_id] = evidence
            return evidence

    def effective_events(
        self, tenant_id: str, as_of: datetime
    ) -> tuple[LearningEvent, ...]:
        with self._lock:
            retracted_ids: set[str] = set()
            for correction in self._corrections.values():
                if (
                    correction.tenant_id == tenant_id
                    and correction.action == CorrectionAction.RETRACT
                    and correction.occurred_at <= as_of
                ):
                    retracted_ids.add(correction.target_id)

            return tuple(
                event
                for event in self._events.values()
                if event.tenant_id == tenant_id
                and event.recorded_at <= as_of
                and event.event_id not in retracted_ids
            )

    def record_deletion_receipt(self, receipt: DeletionReceipt) -> DeletionReceipt:
        # The WRAPPER first: it is the only place the envelope's kind can be
        # checked against the type of receipt being stored. A DeletionReceipt
        # carrying a ``ranking`` envelope passed every ownership check before
        # this line existed, because ``ranking`` is a legal kind, just not
        # this receipt's kind.
        if type(receipt) is not DeletionReceipt:
            raise LedgerError(
                f"deletion receipt: unknown record type {type(receipt).__name__}; "
                "expected the exact frozen DeletionReceipt class"
            )
        _reject_bad_ownership(receipt, label="deletion receipt")
        _reject_bad_ownership(receipt.envelope, label="deletion receipt envelope")
        with self._lock:
            unresolved = [row for row in receipt.projections if not row.resolved]
            if receipt.envelope.state == "settled" and unresolved:
                raise LedgerError(
                    "a deletion receipt with any unresolved projection cannot settle"
                )
            if receipt.envelope.receipt_id in self._deletion_receipts:
                raise LedgerError(
                    "deletion receipt already recorded: "
                    f"{receipt.envelope.receipt_id}"
                )
            self._deletion_receipts[receipt.envelope.receipt_id] = receipt
            return receipt
