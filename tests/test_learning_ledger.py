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
from curator.ownership import ownership_id_sql_check

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


OWNED_TABLES = (
    "learning_events",
    "correction_events",
    "raw_imports",
    "evidence_items",
    "knowledge_artifacts",
    "artifact_versions",
    "artifact_relations",
    "deletion_receipts",
    "mirror_receipts",
)


def test_required_field_names_reads_inherited_dataclass_fields():
    """The four Ownership fields are INHERITED, so a check that only looked at
    a class's own annotations would silently stop covering them."""
    names = _required_field_names(LearningEvent)
    assert {"tenant_id", "actor_id", "actor_kind", "user_id"} <= names
    assert "event_id" in names


def test_mirror_receipt_idempotency_is_unique_per_tenant_user_and_key():
    """The database accepts one key per user and rejects a repeated triple."""
    block = _table_block(_migration_text(), "mirror_receipts")
    assert re.search(
        r"constraint\s+mirror_receipts_tenant_user_idempotency_key_key\s+"
        r"unique\s*\(tenant_id,\s*user_id,\s*idempotency_key\)",
        block,
        re.I | re.S,
    ), "mirror receipt idempotency must be unique on tenant, user, and key"


@pytest.mark.parametrize("table", OWNED_TABLES)
def test_every_owned_table_carries_the_four_ownership_columns(table):
    block = _table_block(_migration_text(), table)
    assert re.search(r"^  tenant_id text not null,", block, re.M), f"{table}: tenant_id"
    assert re.search(r"^  actor_id text not null,", block, re.M), (
        f"{table}: actor_id must be NOT NULL with no default"
    )
    assert "default ''" not in block.split("actor_id text not null")[1].split("\n")[0]
    assert re.search(
        r"^  actor_kind text not null check \(actor_kind in \('human', 'agent', 'system'\)\),",
        block,
        re.M,
    ), f"{table}: actor_kind"
    assert re.search(r"^  user_id text not null,", block, re.M), (
        f"{table}: user_id must be NOT NULL. All nine tables are SUBJECT-BOUND "
        "(a per-person delete must find these rows), so the human subject is "
        "required whatever wrote the row."
    )


@pytest.mark.parametrize("table", OWNED_TABLES)
def test_every_owned_table_requires_a_named_human_subject(table):
    """All nine tables hold rows ABOUT a person, so user_id is unconditionally
    required. The old ``check (actor_kind = 'system' or user_id is not null)``
    form is gone: it let a normalizer-written row about one human name nobody,
    which makes "delete everything about me" impossible to execute or prove."""
    block = _table_block(_migration_text(), table)
    assert "check (actor_kind = 'system' or user_id is not null)" not in block, (
        f"{table}: the system-actor escape hatch is back on a subject-bound table"
    )
    assert re.search(r"^  user_id text not null,", block, re.M), f"{table}: user_id"


@pytest.mark.parametrize("table", OWNED_TABLES)
@pytest.mark.parametrize("column", ("tenant_id", "actor_id", "user_id"))
def test_every_ownership_column_rejects_a_blank_value(table, column):
    """`not null` is not `non-blank`, and `non-blank` is not `canonical`.

    Without the check, actor_id = '' (the exact unattributed value this shape
    removed as a column default) and user_id = '' are both legal, and a
    deletion sweep filtering ``user_id is null`` silently misses the blank
    rows. The old form was ``btrim(x) <> ''``, which strips SPACES ONLY: a live
    INSERT of a tab, a newline, U+00A0 or U+3000 was accepted by the database
    while both Python validators rejected it. The text is GENERATED from the
    frozen invisible set now, so this asserts the generated text verbatim.
    """
    block = _table_block(_migration_text(), table)
    assert ownership_id_sql_check(column) in block, (
        f"{table}.{column}: missing the canonical-id check"
    )
    assert f"check (btrim({column}) <> '')" not in block, (
        f"{table}.{column}: the spaces-only blank check is back"
    )


CHILD_PARENT_TENANT_LINKS = (
    ("evidence_items", "raw_import_id", "raw_imports"),
    ("artifact_versions", "artifact_id", "knowledge_artifacts"),
    ("artifact_relations", "artifact_id", "knowledge_artifacts"),
    ("mirror_receipts", "artifact_id", "knowledge_artifacts"),
)


@pytest.mark.parametrize("child,column,parent", CHILD_PARENT_TENANT_LINKS)
def test_no_child_row_can_name_a_parent_in_a_different_tenant(child, column, parent):
    """Every parent link is COMPOSITE on (parent_id, tenant_id).

    Referential integrity is checked by the system with row security OFF, so
    RLS does not stop a tenant-A member inserting a child whose parent lives in
    tenant B. Only the tenant column inside the key does. A single-column FK
    also leaks an existence oracle for another tenant's ids (the insert
    succeeds or fails depending on whether the id exists).
    """
    text = _migration_text()
    assert f"unique ({column}, tenant_id)" in _table_block(text, parent), (
        f"{parent}: the composite FK target key is missing"
    )
    block = _table_block(text, child)
    assert f"foreign key ({column}, tenant_id)" in block, (
        f"{child}: {column} still references its parent without the tenant"
    )
    assert f"references public.{parent} ({column}, tenant_id)" in block
    single = f"{column} text not null references public.{parent} ({column})"
    assert single not in block, f"{child}: the single-column FK is still there"


def test_membership_auth_subject_column_is_not_named_user_id():
    """The resolved name collision: tenant_members holds an AUTH subject, which
    is a different thing from the contract's provider-neutral user_id."""
    text = _migration_text()
    block = _table_block(text, "tenant_members")
    assert "auth_uid uuid not null references auth.users(id)" in block
    assert "user_id" not in block, "tenant_members must not reuse the contract's user_id name"
    assert "m.auth_uid = auth.uid()" in text, "is_tenant_member must read the renamed column"
    assert "auth.uid() = auth_uid" in text, "the membership policy must read the renamed column"


def test_artifact_versions_cannot_claim_a_different_tenant_than_its_parent():
    text = _migration_text()
    assert "unique (artifact_id, tenant_id)" in _table_block(text, "knowledge_artifacts")
    versions = _table_block(text, "artifact_versions")
    assert "foreign key (artifact_id, tenant_id)" in versions
    assert "references public.knowledge_artifacts (artifact_id, tenant_id)" in versions
    # The row's own tenant column is load-bearing in RLS, not decorative.
    assert text.count("public.is_tenant_member(tenant_id)\n  and exists (") == 2


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


def _make_event(
    event_id: str, idempotency_key: str = "idem-1", user_id: str = "user-1"
) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        tenant_id="tenant-a",
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        user_id=user_id,
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


def test_idempotency_is_scoped_per_user_not_only_per_tenant():
    """Round-6 reproduction: bob's post with alice's key must store BOB's own
    row and return it, and ``effective_events`` for the tenant must list both.

    Before this fix the index was keyed on (tenant_id, idempotency_key) alone,
    so bob posting with the SAME key text alice already used got ALICE's row
    handed back (a cross-user read inside one tenant) and bob's own event was
    silently dropped, with no way to delete it later because it was never
    stored. The identity is now scoped per user
    (curator/ledger/memory.py, docs/contracts/event.md): a client may reuse
    the same key text across different users safely.
    """
    store = InMemoryLedgerStore()
    alice_event = store.append_event(
        _make_event("event-alice", "more-like-this-story-000042", user_id="user-alice")
    )
    bob_event = store.append_event(
        _make_event("event-bob", "more-like-this-story-000042", user_id="user-bob")
    )

    assert alice_event.event_id == "event-alice"
    assert bob_event.event_id == "event-bob", (
        "bob's post must store his OWN row, not silently return alice's"
    )
    assert bob_event.user_id == "user-bob"

    effective = store.effective_events("tenant-a", _now())
    assert {event.event_id for event in effective} == {"event-alice", "event-bob"}

    # The SAME triple twice is still idempotent: a retry returns the stored row.
    retry = store.append_event(
        _make_event("event-bob-retry", "more-like-this-story-000042", user_id="user-bob")
    )
    assert retry.event_id == "event-bob"


def test_retraction_excludes_event_from_effective_events_but_row_stays_readable():
    store = InMemoryLedgerStore()
    event = store.append_event(_make_event("event-1"))

    assert store.effective_events("tenant-a", _now()) == (event,)

    correction = CorrectionEvent(
        event_id="correction-1",
        tenant_id="tenant-a",
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        user_id="user-1",
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
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        user_id="user-1",
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


# ---------------------------------------------------------------------------
# The runtime ownership guard: every write path recomputes the rule
# ---------------------------------------------------------------------------


def _make_correction() -> CorrectionEvent:
    return CorrectionEvent(
        event_id="correction-guard",
        tenant_id="tenant-a",
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        user_id="user-1",
        action=CorrectionAction.RETRACT,
        target_kind="learning_event",
        target_id="event-1",
        reason_code="user_requested",
        occurred_at=_now(),
    )


def _make_evidence() -> EvidenceItem:
    return EvidenceItem(
        tenant_id="tenant-a",
        actor_id="actor-1",
        actor_kind=ActorKind.SYSTEM,
        user_id="user-1",
        evidence_id="evidence-guard",
        raw_import_id=None,
        source_item_id="visit-1",
        occurred_at=_now(),
        recorded_at=_now(),
        evidence_class=EvidenceClass.OBSERVED,
        origin=EvidenceOrigin.LIVE,
        confidence=ConfidenceBand.WEAK,
        weight=0.1,
        policy_revision=1,
    )


def _make_deletion_receipt() -> DeletionReceipt:
    return DeletionReceipt(
        envelope=ReceiptEnvelope(
            receipt_id="receipt-guard",
            tenant_id="tenant-a",
            actor_id="actor-1",
            actor_kind=ActorKind.HUMAN,
            user_id="user-1",
            kind="deletion",
            state="settled",
            created_at=_now(),
            policy_revision=1,
        ),
        target_kind="learning_event",
        target_ids=("event-1",),
        correction_watermark=_now(),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-guard",
        zero_contribution_verdict=True,
        projections=(
            ProjectionResolution(
                projection="events", resolved=True, resolution_kind="deleted"
            ),
        ),
    )


def _write(store: InMemoryLedgerStore, record):
    if isinstance(record, LearningEvent):
        return store.append_event(record)
    if isinstance(record, CorrectionEvent):
        return store.append_correction(record)
    if isinstance(record, EvidenceItem):
        return store.append_evidence(record)
    return store.record_deletion_receipt(record)


def _break_ownership(record, **changes):
    """Apply the seeded defect, reaching into a receipt's envelope."""
    if isinstance(record, DeletionReceipt):
        return dataclasses.replace(
            record, envelope=dataclasses.replace(record.envelope, **changes)
        )
    return dataclasses.replace(record, **changes)


WRITE_PATHS = (
    ("append_event", _make_event("event-guard")),
    ("append_correction", _make_correction()),
    ("append_evidence", _make_evidence()),
    ("record_deletion_receipt", _make_deletion_receipt()),
)

OWNERSHIP_DEFECTS = (
    ("blank actor_id", {"actor_id": ""}),
    ("whitespace actor_id", {"actor_id": "   "}),
    ("blank tenant_id", {"tenant_id": ""}),
    ("subject-bound row with no human", {"actor_kind": ActorKind.SYSTEM, "user_id": None}),
    ("blank user_id", {"user_id": ""}),
    ("whitespace user_id", {"user_id": "   "}),
    ("wrong-cased actor_kind", {"actor_kind": "Human"}),
    # Round-4 must-fix 1: non-blank was never enough. Each of these is a SECOND
    # encoding of an id that already exists, so a per-person delete keyed on
    # the canonical spelling never finds the row it wrote.
    ("space-padded user_id", {"user_id": " user-000001 "}),
    ("tab-led user_id", {"user_id": "\tuser-000001"}),
    ("zero-width-space user_id", {"user_id": "user\u200b000001"}),
    ("ideographic-space tenant_id", {"tenant_id": "\u3000"}),
    ("space-padded actor_id", {"actor_id": " actor-human-owner "}),
)


@pytest.mark.parametrize("path,record", WRITE_PATHS, ids=[p for p, _ in WRITE_PATHS])
@pytest.mark.parametrize(
    "defect,changes", OWNERSHIP_DEFECTS, ids=[d for d, _ in OWNERSHIP_DEFECTS]
)
def test_every_write_path_rejects_a_broken_ownership_shape(path, record, defect, changes):
    """The contract package is DECLARATIVE, so it constructs these happily.

    This is the gate that stops one being STORED. It recomputes the rule at
    every write rather than trusting the caller, which is the same discipline
    the fixture freeze applies to the frozen corpus.
    """
    store = InMemoryLedgerStore()
    _write(store, record)  # the clean record is accepted first (positive control)

    broken_store = InMemoryLedgerStore()
    with pytest.raises(LedgerError):
        _write(broken_store, _break_ownership(record, **changes))


def test_a_subjectless_record_may_still_carry_a_null_user_id():
    """The two tiers really are two. A system-written source checkpoint names
    no human and is legal; the same shape on a learning event is not."""
    from curator.ownership import ownership_violations
    from curator.contracts.enums import CheckpointState, HealthStatus  # noqa: F401
    from curator.contracts.source_plugin import SourceCheckpoint

    checkpoint = SourceCheckpoint(
        tenant_id="tenant-a",
        actor_id="actor-system",
        actor_kind=ActorKind.SYSTEM,
        user_id=None,
        plugin_id="plugin-1",
        source_id="source-1",
        state=CheckpointState.UNINITIALIZED,
        cursor="",
        watermark=None,
        last_settled_run_id="",
        health_receipt_id="",
        updated_at=_now(),
    )
    assert ownership_violations(checkpoint) == ()

    event = dataclasses.replace(
        _make_event("event-tier"), actor_kind=ActorKind.SYSTEM, user_id=None
    )
    assert ownership_violations(event) != ()


def test_the_store_rejects_a_receipt_whose_envelope_kind_is_unknown():
    """Round-2 must-fix 1, at the write path.

    ``kind="rankng"`` reached the store before this gate existed: the envelope
    carried a non-blank user_id, so nothing ever resolved which subject tier
    the receipt belonged to, and an unclassified kind defaulted to accepted.
    """
    store = InMemoryLedgerStore()
    receipt = _make_deletion_receipt()
    store.record_deletion_receipt(receipt)  # positive control

    typo = dataclasses.replace(
        receipt, envelope=dataclasses.replace(receipt.envelope, kind="rankng")
    )
    with pytest.raises(LedgerError):
        InMemoryLedgerStore().record_deletion_receipt(typo)


def test_the_store_rejects_a_deletion_receipt_carrying_a_ranking_envelope():
    """Round-2 must-fix 2, at the write path.

    A ranking envelope on a deletion receipt is a TYPE MISMATCH, not a tier
    hole: both kinds are subject-bound and both demand a non-blank ``user_id``,
    so every ownership check passes while the receipt's type says it proves a
    deletion and its envelope says it explains a slate order. Only the wrapper
    binding can catch it.
    """
    receipt = _make_deletion_receipt()
    mismatched = dataclasses.replace(
        receipt, envelope=dataclasses.replace(receipt.envelope, kind="ranking")
    )
    with pytest.raises(LedgerError):
        InMemoryLedgerStore().record_deletion_receipt(mismatched)
