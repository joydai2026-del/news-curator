"""LedgerStore protocol: the typed interface the learning ledger implements.

Declarative surface only; behavior lives in each implementation (memory.py for
tests and local dry runs, a Postgres-backed implementation later). Every
method is append-only: there is no update or delete on an event, a
correction, or an evidence item. A correction is itself a new row, never an
edit to the row it corrects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from curator.contracts.event import CorrectionEvent, LearningEvent
from curator.contracts.evidence import EvidenceItem
from curator.contracts.receipt import DeletionReceipt


class LedgerStore(Protocol):
    """Typed append-only store for the learning ledger."""

    def append_event(self, event: LearningEvent) -> LearningEvent:
        """Append a learning event.

        Idempotent on (tenant_id, idempotency_key): resubmitting the same
        intent twice returns the first row rather than creating a second
        (SC-18). The returned event is always the row that is now on record,
        which may not be the argument passed in.
        """
        ...

    def append_correction(self, correction: CorrectionEvent) -> CorrectionEvent:
        """Append a correction or retraction. Never edits the target row."""
        ...

    def append_evidence(self, evidence: EvidenceItem) -> EvidenceItem:
        """Append one normalized evidence item."""
        ...

    def effective_events(
        self, tenant_id: str, as_of: datetime
    ) -> tuple[LearningEvent, ...]:
        """Return the effective-event projection as of a watermark.

        The effective set is every learning event for the tenant recorded at
        or before ``as_of``, minus any event retracted by a correction whose
        own ``occurred_at`` is at or before ``as_of``. Retracted rows remain
        readable individually; they are excluded only from this projection.
        """
        ...

    def record_deletion_receipt(self, receipt: DeletionReceipt) -> DeletionReceipt:
        """Record a deletion receipt.

        A receipt whose envelope state is "settled" while any of its
        projections is unresolved is rejected: settlement requires every
        listed projection to be resolved first (mirrors the frozen
        DeletionReceipt invariant in tests/test_contract_freeze.py).
        """
        ...
