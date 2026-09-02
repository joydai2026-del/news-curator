"""Tests for the durable source checkpoint store (Gate 0c gap, Phase 3 slice 1).

Deterministic, no network. Covers both ``MemoryCheckpointStore`` (the test
double) and ``JsonFileCheckpointStore`` (the real GitHub-Actions-artifact
backend) against the same contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.contracts.enums import CheckpointState
from curator.contracts.source_plugin import SourceCheckpoint
from curator.sources.checkpoint import (
    CheckpointCorruptError,
    CheckpointNotSettledError,
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
        plugin_id="feed",
        source_id=source_id,
        tenant_id="default",
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
        plugin_id="feed",
        source_id=source_id,
        tenant_id="default",
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
