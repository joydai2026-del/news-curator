"""Controlled protocol tests for the local import rehearsal, never import evidence."""
from __future__ import annotations

import json
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import scripts.verify_import_rehearsal as rehearsal


def test_local_socket_validation_rejects_network_and_accepts_bound_socket() -> None:
    assert_raises = False
    try:
        rehearsal.validate_local_socket("localhost", 5432)
    except ValueError:
        assert_raises = True
    assert assert_raises
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ncis-") as directory:
        socket_dir = Path(directory)
        path = socket_dir / ".s.PGSQL.5432"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        try:
            assert rehearsal.validate_local_socket(str(socket_dir), 5432) == socket_dir
        finally:
            server.close()


def test_selector_excludes_fresh_and_uses_deterministic_bounded_public_rows(monkeypatch, tmp_path: Path) -> None:
    # Controlled protocol rows, not news records and not NC2-A10 evidence.
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    item = lambda story, age: SimpleNamespace(source_id="public", is_newsletter=False, published_at=now - timedelta(hours=age), canonical_url=story, story=story)
    snapshot = SimpleNamespace(generated_at=now, configuration_digest="config", content_digest="content", results=(SimpleNamespace(items=(item("old-a", 48), item("old-b", 30), item("fresh", 1))),))
    monkeypatch.setattr(rehearsal, "load_config", lambda root: SimpleNamespace(categories=()))
    monkeypatch.setattr(rehearsal, "load_source_snapshot", lambda path: snapshot)
    monkeypatch.setattr(rehearsal, "snapshot_config_digest", lambda cfg: "config")
    monkeypatch.setattr(rehearsal, "configured_source_specs", lambda cfg: (SimpleNamespace(id="public"),))
    monkeypatch.setattr(rehearsal, "retain", lambda rows, **kwargs: tuple(rows))
    monkeypatch.setattr(rehearsal, "public_ingest_rows", lambda rows, **kwargs: [{"story_id": row.story, "published_at": row.published_at.isoformat()} for row in rows])
    artifact = tmp_path / "snapshot.json"; artifact.write_text("{}", encoding="utf-8")
    metadata, selected, unaffected = rehearsal.select_rows(artifact, root=tmp_path, limit=1)
    assert [row["story_id"] for row in selected] == ["old-a"]
    assert [row["story_id"] for row in unaffected] == ["old-b"]
    assert metadata["selection"]["excluded"]["not_older_than_24_hours"] == 1


def test_transaction_uses_epoch_timestamp_comparison_and_rollback() -> None:
    sql = rehearsal._transaction_sql("[]", "unaffected", ["selected"], {"selected": "2026-09-14T00:00:00Z"})
    assert "extract(epoch from o.published_at)" in sql
    assert "begin;" in sql and "rollback;" in sql
    assert "delete from public.retained_corpus_observations" in sql


def test_timestamp_normalization_compares_instants_not_postgres_display_format() -> None:
    assert rehearsal.normalize_timestamp("2026-09-14T00:00:00+00:00") == "2026-09-14T00:00:00Z"
    assert rehearsal.normalize_timestamp("2026-09-13T20:00:00-04:00") == "2026-09-14T00:00:00Z"


def test_command_shape_is_local_socket_psql_without_shell(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(rehearsal.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0, stdout="t\n", stderr=""))
    assert rehearsal._run_sql("psql", "isolated", "select true;", host=tmp_path, port=5432) == "t"
    assert calls[0][0] == ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-h", str(tmp_path), "-p", "5432", "isolated"]


def test_invalid_host_writes_failure_receipt_without_database(monkeypatch, tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["verify_import_rehearsal.py", "--snapshot", str(tmp_path / "unused.json"), "--receipt", str(receipt), "--authorization-reference", "approved-test", "--host", "remote.example"])
    assert rehearsal.main() == 1
    result = json.loads(receipt.read_text())
    assert result["status"] == "fail" and result["database_created"] is False and result["error_class"] == "ValueError"
