"""Tests for the durable source checkpoint store (Gate 0c gap, Phase 3 slice 1).

Deterministic, no network. Covers both ``MemoryCheckpointStore`` (the test
double) and ``JsonFileCheckpointStore`` (the real GitHub-Actions-artifact
backend) against the same contract.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.contracts.enums import ActorKind, CheckpointState
from curator.contracts.source_plugin import SourceCheckpoint
from curator.sources.checkpoint import (
    CheckpointCorruptError,
    CheckpointNotSettledError,
    CheckpointOwnershipError,
    CheckpointRegressionError,
    CheckpointSchemaVersionError,
    JsonFileCheckpointStore,
    MemoryCheckpointStore,
    SCHEMA_VERSION,
)

FIXTURES = Path(__file__).parent / "fixtures" / "checkpoints"

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _settled(
    *,
    source_id: str = "example-feed",
    cursor: str = "2026-08-01T00:00:00+00:00",
    watermark: datetime = NOW,
    run_id: str = "run-0001",
    health_receipt_id: str = "health-0001",
    updated_at: datetime = NOW,
    consecutive_failures: int = 0,
) -> SourceCheckpoint:
    return SourceCheckpoint(
        tenant_id="default",
        actor_id="actor-system",
        actor_kind=ActorKind.SYSTEM,
        user_id=None,
        plugin_id="feed",
        source_id=source_id,
        state=CheckpointState.SETTLED,
        cursor=cursor,
        watermark=watermark,
        last_settled_run_id=run_id,
        health_receipt_id=health_receipt_id,
        updated_at=updated_at,
        consecutive_failures=consecutive_failures,
    )


def _unsettled(*, source_id: str = "example-feed") -> SourceCheckpoint:
    return SourceCheckpoint(
        tenant_id="default",
        actor_id="actor-system",
        actor_kind=ActorKind.SYSTEM,
        user_id=None,
        plugin_id="feed",
        source_id=source_id,
        state=CheckpointState.ADVANCING,
        cursor="2026-08-01T00:00:00+00:00",
        watermark=NOW,
        last_settled_run_id="",
        health_receipt_id="",
        updated_at=NOW,
    )


# --- fresh-start load ------------------------------------------------------


def test_memory_store_fresh_start_load_returns_none():
    store = MemoryCheckpointStore()
    assert store.load("example-feed") is None
    assert store.all() == {}


def test_json_file_store_fresh_start_missing_file_returns_none(tmp_path):
    store = JsonFileCheckpointStore(tmp_path / "checkpoints.json")
    assert store.load("example-feed") is None
    assert store.all() == {}


# --- advance only when settled ---------------------------------------------


def test_memory_store_advance_accepts_settled_checkpoint():
    store = MemoryCheckpointStore()
    written = store.advance(_settled())
    assert written.state == CheckpointState.SETTLED
    assert store.load("example-feed") == written


def test_memory_store_advance_rejects_unsettled_checkpoint():
    store = MemoryCheckpointStore()
    with pytest.raises(CheckpointNotSettledError):
        store.advance(_unsettled())


def test_memory_store_advance_rejects_settled_without_health_receipt():
    store = MemoryCheckpointStore()
    bad = _settled(health_receipt_id="")
    with pytest.raises(CheckpointNotSettledError):
        store.advance(bad)


def test_json_file_store_advance_rejects_unsettled_checkpoint(tmp_path):
    store = JsonFileCheckpointStore(tmp_path / "checkpoints.json")
    with pytest.raises(CheckpointNotSettledError):
        store.advance(_unsettled())


# --- backward watermark / cursor rejected -----------------------------------


def test_memory_store_rejects_backward_cursor():
    store = MemoryCheckpointStore()
    store.advance(_settled(cursor="2026-08-05T00:00:00+00:00", watermark=NOW))
    earlier = _settled(
        cursor="2026-08-01T00:00:00+00:00",
        watermark=NOW - timedelta(days=1),
        updated_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(CheckpointRegressionError):
        store.advance(earlier)
    # the earlier, rejected advance must not have overwritten state
    assert store.load("example-feed").cursor == "2026-08-05T00:00:00+00:00"


def test_json_file_store_rejects_backward_watermark(tmp_path):
    store = JsonFileCheckpointStore(tmp_path / "checkpoints.json")
    store.advance(_settled(watermark=NOW, cursor="c2"))
    earlier = _settled(watermark=NOW - timedelta(days=1), cursor="c3", updated_at=NOW + timedelta(minutes=1))
    with pytest.raises(CheckpointRegressionError):
        store.advance(earlier)


# --- explicit reset recorded -------------------------------------------------


def test_memory_store_explicit_reset_allows_backward_move_and_is_recorded():
    store = MemoryCheckpointStore()
    store.advance(_settled(cursor="2026-08-05T00:00:00+00:00", watermark=NOW, consecutive_failures=3))
    reset_checkpoint = _settled(
        cursor="2026-08-01T00:00:00+00:00",
        watermark=NOW - timedelta(days=1),
        updated_at=NOW + timedelta(minutes=1),
        consecutive_failures=3,
    )
    written = store.advance(reset_checkpoint, reset=True)
    assert written.cursor == "2026-08-01T00:00:00+00:00"
    # the reset is recorded: consecutive_failures is cleared as part of the reset
    assert written.consecutive_failures == 0
    assert store.load("example-feed") == written


def test_json_file_store_explicit_reset_persists(tmp_path):
    path = tmp_path / "checkpoints.json"
    store = JsonFileCheckpointStore(path)
    store.advance(_settled(cursor="z-late", watermark=NOW))
    written = store.advance(
        _settled(cursor="a-early", watermark=NOW - timedelta(days=1), updated_at=NOW + timedelta(minutes=1)),
        reset=True,
    )
    reloaded = JsonFileCheckpointStore(path)
    assert reloaded.load("example-feed") == written
    assert reloaded.load("example-feed").cursor == "a-early"


# --- corrupt file refused with typed error ----------------------------------


def test_json_file_store_refuses_corrupt_file():
    store = JsonFileCheckpointStore(FIXTURES / "corrupt.json")
    with pytest.raises(CheckpointCorruptError):
        store.load("example-feed")


# --- unknown schema version refused -----------------------------------------


def test_json_file_store_refuses_unknown_schema_version():
    store = JsonFileCheckpointStore(FIXTURES / "unknown_version.json")
    with pytest.raises(CheckpointSchemaVersionError):
        store.load("example-feed")


def test_json_file_store_refuses_a_schema_version_1_file():
    """Version 1 predates the shared Ownership shape, so a v1 row carries no
    actor and no user. It is REFUSED, never silently read as if the missing
    fields were empty. Safe to refuse rather than migrate because no v1 file
    exists in production: this store is greenfield and nothing writes it yet."""
    store = JsonFileCheckpointStore(FIXTURES / "v1.json")
    with pytest.raises(CheckpointSchemaVersionError):
        store.load("example-feed")


def test_json_file_store_refuses_a_file_missing_user_id():
    """The KEY is required even though the VALUE may be null for a system
    actor. Omission is corrupt, not "acts for no human"."""
    store = JsonFileCheckpointStore(FIXTURES / "missing_user_id.json")
    with pytest.raises(CheckpointCorruptError):
        store.load("example-feed")


def test_json_file_store_refuses_a_non_system_actor_with_no_user(tmp_path):
    """The same null rule the contract freeze and the SQL check enforce,
    stated on the durable file: only a system actor may act for no human."""
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload["checkpoints"]["example-feed"]["actor_kind"] = "agent"
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")


# --- atomicity: crash between temp write and rename -------------------------


def test_json_file_store_atomic_write_leaves_original_intact_on_crash(tmp_path, monkeypatch):
    path = tmp_path / "checkpoints.json"
    store = JsonFileCheckpointStore(path)
    first = store.advance(_settled(cursor="c1", watermark=NOW))
    original_bytes = path.read_bytes()

    def _boom(*_args, **_kwargs):
        raise OSError("simulated crash between temp write and rename")

    monkeypatch.setattr("curator.sources.checkpoint.os.replace", _boom)

    # a second store instance so the crash hits a fresh, un-cached load
    second = JsonFileCheckpointStore(path)
    with pytest.raises(OSError):
        second.advance(_settled(cursor="c2", watermark=NOW + timedelta(hours=1), updated_at=NOW + timedelta(hours=1)))

    # original file on disk must be untouched by the failed write
    assert path.read_bytes() == original_bytes
    # a fresh load still sees the last successfully settled checkpoint
    reloaded = JsonFileCheckpointStore(path)
    assert reloaded.load("example-feed") == first


# --- round trip through the JSON file equals the in-memory state -----------


def test_json_file_round_trip_equals_memory_state(tmp_path):
    memory = MemoryCheckpointStore()
    checkpoint = _settled(cursor="round-trip", watermark=NOW)
    memory.advance(checkpoint)

    path = tmp_path / "checkpoints.json"
    file_store = JsonFileCheckpointStore(path)
    file_store.advance(checkpoint)

    reloaded = JsonFileCheckpointStore(path)
    assert reloaded.load("example-feed") == memory.load("example-feed")
    assert dict(reloaded.all()) == dict(memory.all())


def test_json_file_round_trip_via_fixture_matches_expected_shape():
    store = JsonFileCheckpointStore(FIXTURES / "valid.json")
    checkpoint = store.load("example-feed")
    assert checkpoint is not None
    assert checkpoint.plugin_id == "feed"
    assert checkpoint.source_id == "example-feed"
    assert checkpoint.state == CheckpointState.SETTLED
    assert checkpoint.cursor == "2026-08-01T00:00:00+00:00"
    assert checkpoint.health_receipt_id == "health-0001"
    assert checkpoint.tenant_id == "default"
    assert checkpoint.actor_id == "actor-system"
    assert checkpoint.actor_kind == ActorKind.SYSTEM
    assert checkpoint.user_id is None
    assert checkpoint.watermark == datetime(2026, 8, 1, tzinfo=timezone.utc)


# --- multi-source all() ------------------------------------------------------


def test_all_returns_every_source():
    store = MemoryCheckpointStore()
    store.advance(_settled(source_id="feed-a", cursor="a1"))
    store.advance(_settled(source_id="feed-b", cursor="b1"))
    all_checkpoints = store.all()
    assert set(all_checkpoints) == {"feed-a", "feed-b"}


def test_schema_version_constant_matches_fixture():
    with (FIXTURES / "valid.json").open() as handle:
        payload = json.load(handle)
    assert payload["version"] == SCHEMA_VERSION


# --- ownership: blank is not null, on the read path and on advance ----------


@pytest.mark.parametrize("actor_kind", ("system", "human"))
@pytest.mark.parametrize("blank", ("", "   "), ids=("empty", "whitespace"))
def test_json_file_store_refuses_a_blank_user_id(tmp_path, actor_kind, blank):
    """`not null` is not `non-blank`. Three encodings of "no human" (null, "",
    "   ") where the contract says there is one would let a deletion sweep
    filtering ``user_id is null`` miss the blank rows."""
    payload = json.loads((FIXTURES / "valid.json").read_text())
    entry = payload["checkpoints"]["example-feed"]
    entry["actor_kind"] = actor_kind
    entry["user_id"] = blank
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")


@pytest.mark.parametrize("key", ("tenant_id", "actor_id", "plugin_id"))
@pytest.mark.parametrize("blank", ("", "   "), ids=("empty", "whitespace"))
def test_json_file_store_refuses_a_blank_identity_key(tmp_path, key, blank):
    """An empty actor_id is the exact unattributed value this shape removed as
    a default. It was still a legal VALUE on this read path."""
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload["checkpoints"]["example-feed"][key] = blank
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")


ADVANCE_DEFECTS = (
    ("blank actor_id", {"actor_id": ""}),
    ("whitespace actor_id", {"actor_id": "   "}),
    ("blank tenant_id", {"tenant_id": ""}),
    ("blank user_id", {"user_id": ""}),
    ("whitespace user_id", {"user_id": "   "}),
    ("human actor with no user", {"actor_kind": ActorKind.HUMAN, "user_id": None}),
    ("wrong-cased actor_kind", {"actor_kind": "System"}),
)


@pytest.mark.parametrize(
    "defect,changes", ADVANCE_DEFECTS, ids=[d for d, _ in ADVANCE_DEFECTS]
)
@pytest.mark.parametrize("store_kind", ("memory", "file"))
def test_advance_rejects_a_broken_ownership_shape(tmp_path, defect, changes, store_kind):
    """The write path recomputes the rule instead of trusting the caller.

    ``SourceCheckpoint`` is a frozen DECLARATIVE dataclass, so it constructs
    every one of these happily; the store is what refuses to persist them.
    """
    import dataclasses

    store = (
        MemoryCheckpointStore()
        if store_kind == "memory"
        else JsonFileCheckpointStore(tmp_path / "checkpoints.json")
    )
    store.advance(_settled())  # positive control: the clean record is accepted

    with pytest.raises(CheckpointOwnershipError):
        store.advance(dataclasses.replace(_settled(source_id="other-feed"), **changes))


def test_load_derives_the_ownership_rule_instead_of_restating_it(tmp_path, monkeypatch):
    """`load` must read the frozen tiers, not a hand-written copy of them.

    `load` used to re-implement the blank checks and the null rule inline. It
    read none of the classification tuples, so the day `SourceCheckpoint`
    becomes subject-bound (a possible per-user checkpoint would change one line
    in a tuple) `load` would have kept accepting a
    null-user file that `advance`, the fixture corpus, and the `not null`
    column all reject. Reclassifying it here proves `load` follows the tuple.
    """
    from curator import ownership as ownership_module

    payload = json.loads((FIXTURES / "valid.json").read_text())
    assert payload["checkpoints"]["example-feed"]["user_id"] is None
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))

    # Positive control: subjectless today, so the null-user file loads.
    assert JsonFileCheckpointStore(path).load("example-feed") is not None

    monkeypatch.setattr(
        ownership_module,
        "_SUBJECTLESS",
        tuple(
            cls
            for cls in ownership_module._SUBJECTLESS
            if cls is not SourceCheckpoint
        ),
    )
    monkeypatch.setattr(
        ownership_module,
        "_SUBJECT_BOUND",
        (*ownership_module._SUBJECT_BOUND, SourceCheckpoint),
    )
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")


# --- canonical ownership ids (round-4 must-fix 1) ---------------------------


NONCANONICAL_IDS = (" tenant-1 ", "\ttenant-1", "tenant​1", "　")


@pytest.mark.parametrize("value", NONCANONICAL_IDS, ids=[repr(v) for v in NONCANONICAL_IDS])
@pytest.mark.parametrize("field", ("tenant_id", "actor_id"))
def test_advance_refuses_a_noncanonical_ownership_id(field, value):
    """Non-blank was never enough on a durable cursor either.

    ``" default "`` and ``"default"`` are two encodings of one tenant, so a
    checkpoint written under the padded spelling is invisible to every query
    that uses the canonical one. The rule is not restated here: ``advance``
    calls the same ``ownership_violations`` the ledger write paths call.
    """
    store = MemoryCheckpointStore()
    store.advance(_settled())  # positive control: the canonical shape is stored
    with pytest.raises(CheckpointOwnershipError):
        store.advance(dataclasses.replace(_settled(), **{field: value}))


@pytest.mark.parametrize("value", NONCANONICAL_IDS, ids=[repr(v) for v in NONCANONICAL_IDS])
def test_load_refuses_a_file_whose_ownership_id_is_not_canonical(value, tmp_path):
    """The same rule on the READ path, which is where a bad file arrives.

    A checkpoint file is written by an earlier run of a GitHub Actions job, so
    ``load`` is the boundary a hand-edited or half-migrated file crosses.
    """
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload["checkpoints"]["example-feed"]["tenant_id"] = value
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")


def test_a_noncanonical_user_id_is_refused_on_both_load_and_advance(tmp_path):
    """``user_id`` is nullable, so its canonical check has its own path."""
    store = MemoryCheckpointStore()
    with pytest.raises(CheckpointOwnershipError):
        store.advance(dataclasses.replace(_settled(), user_id=" user-1 "))
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload["checkpoints"]["example-feed"]["user_id"] = "user​1"
    path = tmp_path / "checkpoints.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError):
        JsonFileCheckpointStore(path).load("example-feed")
