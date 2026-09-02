"""Tests for the phase-4 learning ledger: migration shape + in-memory store.

No database. Part (a) parses the migration text as data and checks it against
the frozen contract dataclasses. Part (b) exercises the in-memory reference
store deterministically.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.contracts.enums import (
    ActorKind,
    ConfidenceBand,
    CorrectionAction,
    EvidenceClass,
    EvidenceOrigin,
    EventType,
)
from curator.contracts.event import CorrectionEvent, LearningEvent
from curator.contracts.evidence import EvidenceItem, RawImport
from curator.contracts.artifact import ArtifactRelation, ArtifactVersion, KnowledgeArtifact
from curator.contracts.receipt import (
    DeletionReceipt,
    ProjectionResolution,
    ReceiptEnvelope,
)
from curator.contracts.mirror import MirrorReceipt
from curator.ledger.memory import InMemoryLedgerStore, LedgerError

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "migrations"
    / "202609020001_learning_ledger.sql"
)

APPEND_ONLY_TABLES = (
    "learning_events",
    "correction_events",
    "evidence_items",
    "artifact_versions",
)


def _migration_text() -> str:
    return MIGRATION_PATH.read_text()


def _table_block(text: str, table: str) -> str:
    marker = f"create table public.{table} ("
    start = text.index(marker)
    # Find the matching close: the next "\n);" after start (tables in this
    # migration are formatted with each ")" that closes the table on its own
    # line, immediately followed by ";").
    end = text.index("\n);", start)
    return text[start:end]


def _required_field_names(cls: type) -> set[str]:
    names = set()
    for f in dataclasses.fields(cls):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            names.add(f.name)
    return names


# ---------------------------------------------------------------------------
# (a) Migration text vs frozen contracts
# ---------------------------------------------------------------------------

TABLE_TO_DATACLASSES = {
    "learning_events": [LearningEvent],
    "correction_events": [CorrectionEvent],
    "evidence_items": [EvidenceItem],
    "raw_imports": [RawImport],
    "knowledge_artifacts": [KnowledgeArtifact],
    "artifact_versions": [ArtifactVersion],
    "artifact_relations": [ArtifactRelation],
    "mirror_receipts": [MirrorReceipt],
}


@pytest.mark.parametrize("table,classes", TABLE_TO_DATACLASSES.items())
def test_required_contract_fields_appear_in_create_table(table, classes):
    text = _migration_text()
    block = _table_block(text, table)
    for cls in classes:
        for field_name in _required_field_names(cls):
            assert field_name in block, (
                f"{table}: required field {field_name!r} from {cls.__name__} "
                "is missing from CREATE TABLE"
            )


def test_deletion_receipts_covers_envelope_and_own_required_fields():
    text = _migration_text()
    block = _table_block(text, "deletion_receipts")
    required = _required_field_names(ReceiptEnvelope) | (
        _required_field_names(DeletionReceipt) - {"envelope"}
    )
    for field_name in required:
        assert field_name in block, (
            f"deletion_receipts: required field {field_name!r} missing "
            "from CREATE TABLE"
        )


ENUM_CHECKS = {
    "learning_events": {
        "actor_kind": [m.value for m in ActorKind],
        "event_type": [m.value for m in EventType],
        "evidence_class": [m.value for m in EvidenceClass],
        "origin": [m.value for m in EvidenceOrigin],
        "confidence": [m.value for m in ConfidenceBand],
    },
    "correction_events": {"action": [m.value for m in CorrectionAction]},
    "raw_imports": {"retention_state": ["active", "retracted", "purged"]},
    "evidence_items": {
        "evidence_class": [m.value for m in EvidenceClass],
        "origin": [m.value for m in EvidenceOrigin],
        "confidence": [m.value for m in ConfidenceBand],
    },
    "knowledge_artifacts": {
        "artifact_type": ["question", "answer", "report", "insight", "save"],
        "status": ["draft", "settled", "redacted", "retracted"],
        "publication_class": ["private", "public"],
    },
    "deletion_receipts": {"state": ["settled", "partial", "failed", "unknown"]},
    "mirror_receipts": {"state": ["planned", "writing", "settled", "conflict", "unknown"]},
}


@pytest.mark.parametrize("table,columns", ENUM_CHECKS.items())
def test_closed_enums_have_check_constraints(table, columns):
    text = _migration_text()
    block = _table_block(text, table)
    for column, values in columns.items():
        check_pattern = re.compile(rf"check\s*\(\s*{column}\s+in\s*\(([^)]*)\)")
        match = check_pattern.search(block)
        assert match, f"{table}.{column}: no CHECK(... in (...)) constraint found"
        listed = match.group(1)
        for value in values:
            assert f"'{value}'" in listed, (
                f"{table}.{column}: enum value {value!r} missing from CHECK list"
            )


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_trigger_attached(table):
    text = _migration_text()
    assert re.search(
        rf"before update or delete on public\.{table}\b", text
    ), f"{table}: missing append-only trigger"
    assert re.search(
        r"execute function public\.reject_ledger_mutation\(\);", text
    )


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_no_update_or_delete_grant_on_history_tables(table):
    text = _migration_text()
    for line in text.splitlines():
        lowered = line.strip().lower()
        if not lowered.startswith("grant"):
            continue
        if f"on table public.{table}" not in lowered:
            continue
        assert "update" not in lowered, f"{table}: found a GRANT UPDATE line: {line!r}"
        assert "delete" not in lowered, f"{table}: found a GRANT DELETE line: {line!r}"
    # And the explicit revoke is present.
    assert re.search(
        rf"revoke update, delete on table public\.{table} from authenticated;", text
    ), f"{table}: missing explicit REVOKE UPDATE, DELETE"


def test_no_delete_grant_anywhere_in_the_ledger():
    # The whole point of this slice: nothing in the learning ledger is ever
    # physically deleted, not even the mutable header rows.
    text = _migration_text()
    for line in text.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("grant") and "delete" in lowered:
            pytest.fail(f"unexpected GRANT ... DELETE line in migration: {line!r}")


# ---------------------------------------------------------------------------
# (b) In-memory store behavior
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 9, 2, tzinfo=timezone.utc)


def _make_event(event_id: str, idempotency_key: str = "idem-1") -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        tenant_id="tenant-a",
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        event_type=EventType.SAVE,
        occurred_at=_now(),
        recorded_at=_now(),
        surface="web",
        idempotency_key=idempotency_key,
        evidence_class=EvidenceClass.EXPLICIT,
        origin=EvidenceOrigin.LIVE,
        confidence=ConfidenceBand.STRONG,
        policy_revision=1,
    )


def test_duplicate_idempotency_key_does_not_create_a_second_row():
    store = InMemoryLedgerStore()
    first = store.append_event(_make_event("event-1", "idem-shared"))
    second = store.append_event(_make_event("event-2", "idem-shared"))

    assert first.event_id == second.event_id == "event-1"
    assert store.effective_events("tenant-a", _now()) == (first,)


def test_retraction_excludes_event_from_effective_events_but_row_stays_readable():
    store = InMemoryLedgerStore()
    event = store.append_event(_make_event("event-1"))

    assert store.effective_events("tenant-a", _now()) == (event,)

    correction = CorrectionEvent(
        event_id="correction-1",
        tenant_id="tenant-a",
        actor_id="actor-1",
        action=CorrectionAction.RETRACT,
        target_kind="learning_event",
        target_id="event-1",
        reason_code="user_requested",
        occurred_at=_now(),
    )
    store.append_correction(correction)

    assert store.effective_events("tenant-a", _now()) == ()
    # The original row is still on record, just excluded from the projection.
    assert store._events["event-1"] == event  # noqa: SLF001 (deterministic test store)


def test_deletion_receipt_cannot_settle_with_unresolved_projection():
    store = InMemoryLedgerStore()
    envelope = ReceiptEnvelope(
        receipt_id="receipt-1",
        tenant_id="tenant-a",
        kind="deletion",
        state="settled",
        created_at=_now(),
        policy_revision=1,
    )
    receipt = DeletionReceipt(
        envelope=envelope,
        target_kind="learning_event",
        target_ids=("event-1",),
        correction_watermark=_now(),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-1",
        zero_contribution_verdict=True,
        projections=(
            ProjectionResolution(
                projection="events", resolved=True, resolution_kind="deleted"
            ),
            ProjectionResolution(
                projection="mirrors",
                resolved=False,
                resolution_kind="pending",
                user_visible_disclosure="mirror deletion still in flight",
            ),
        ),
    )

    with pytest.raises(LedgerError):
        store.record_deletion_receipt(receipt)

    # A receipt with every projection resolved is accepted.
    settled_receipt = dataclasses.replace(
        receipt,
        projections=(
            ProjectionResolution(
                projection="events", resolved=True, resolution_kind="deleted"
            ),
            ProjectionResolution(
                projection="mirrors", resolved=True, resolution_kind="deleted"
            ),
        ),
    )
    recorded = store.record_deletion_receipt(settled_receipt)
    assert recorded.envelope.receipt_id == "receipt-1"
