"""The durable per-source checkpoint store (Gate 0c gap, Phase 3 first slice).

Greenfield: nothing in ``curator/sources/`` persists a poll cursor today
(``SourceContext.durable_store`` is declared and never read or written). This
module is the first concrete ``CheckpointStore`` for
``SourceCheckpoint`` (``curator/contracts/source_plugin.py``).

The hourly collector runs inside a GitHub Actions job with no database, so the
first durable backend is a JSON artifact file handed between runs, borrowing
the shape of the newsletter lane's watermark (``curator/newsletter/state.py``):
advance only after settlement, atomic write via temp file + rename, tolerate a
missing file as a fresh start, and never silently reset on a corrupt file.

This module does not wire into the fetch loop or the pipeline. It only
provides the store.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

from ..contracts.enums import CheckpointState
from ..contracts.source_plugin import SourceCheckpoint

SCHEMA_VERSION = 1


class CheckpointStoreError(ValueError):
    """A safe, typed checkpoint-store failure. Never wraps a raw exception."""


class CheckpointCorruptError(CheckpointStoreError):
    """The checkpoint file exists but its content cannot be trusted.

    Raised instead of silently starting over, because a silent reset is
    exactly the replay-loss failure the plan forbids (a checkpoint that
    resets to uninitialized would re-poll from scratch and reprocess
    already-settled items as if they were new).
    """


class CheckpointSchemaVersionError(CheckpointStoreError):
    """The checkpoint file's schema version is not one this store understands."""


class CheckpointNotSettledError(CheckpointStoreError):
    """``advance`` was called with a checkpoint that has not settled.

    The plan's rule: a checkpoint advances only after the durable normalized
    writes for that batch settle. Passing an ``advancing`` or ``blocked``
    checkpoint to ``advance`` is a caller bug, not a state to persist.
    """


class CheckpointRegressionError(CheckpointStoreError):
    """``advance`` was called with a cursor/watermark that moves backward.

    Rejected unless the caller passes ``reset=True`` explicitly, in which case
    the regression is allowed and recorded on the returned checkpoint rather
    than happening silently.
    """


class CheckpointStore(Protocol):
    """Durable per-source checkpoint access. No I/O details leak through it."""

    def load(self, source_id: str) -> SourceCheckpoint | None: ...

    def advance(
        self, checkpoint: SourceCheckpoint, *, reset: bool = False
    ) -> SourceCheckpoint: ...

    def all(self) -> Mapping[str, SourceCheckpoint]: ...


def _validate_advance(
    previous: SourceCheckpoint | None,
    checkpoint: SourceCheckpoint,
    *,
    reset: bool,
) -> SourceCheckpoint:
    if checkpoint.state != CheckpointState.SETTLED:
        raise CheckpointNotSettledError(
            f"source {checkpoint.source_id}: advance requires a settled "
            f"checkpoint, got {checkpoint.state.value!r}"
        )
    if not checkpoint.cursor:
        raise CheckpointNotSettledError(
            f"source {checkpoint.source_id}: a settled checkpoint requires a cursor"
        )
    if not checkpoint.health_receipt_id:
        raise CheckpointNotSettledError(
            f"source {checkpoint.source_id}: a settled checkpoint requires a health_receipt_id"
        )

    if previous is None or previous.state == CheckpointState.UNINITIALIZED:
        return checkpoint

    regressed = False
    if checkpoint.watermark is not None and previous.watermark is not None:
        if checkpoint.watermark < previous.watermark:
            regressed = True
    if checkpoint.cursor < previous.cursor:
        regressed = True

    if regressed and not reset:
        raise CheckpointRegressionError(
            f"source {checkpoint.source_id}: checkpoint would move backward "
            f"(cursor {previous.cursor!r} -> {checkpoint.cursor!r}); pass "
            f"reset=True to allow an explicit reset"
        )
    if regressed and reset:
        return replace(checkpoint, consecutive_failures=0)
    return checkpoint


class MemoryCheckpointStore:
    """Deterministic in-memory ``CheckpointStore``, for tests only."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, SourceCheckpoint] = {}

    def load(self, source_id: str) -> SourceCheckpoint | None:
        return self._checkpoints.get(source_id)

    def advance(self, checkpoint: SourceCheckpoint, *, reset: bool = False) -> SourceCheckpoint:
        previous = self._checkpoints.get(checkpoint.source_id)
        written = _validate_advance(previous, checkpoint, reset=reset)
        self._checkpoints[checkpoint.source_id] = written
        return written

    def all(self) -> Mapping[str, SourceCheckpoint]:
        return dict(self._checkpoints)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: object, *, source_id: str, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise CheckpointCorruptError(f"source {source_id}: {field_name} is missing or not a string")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckpointCorruptError(f"source {source_id}: {field_name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_iso(raw: object, *, source_id: str, field_name: str) -> datetime | None:
    if raw is None:
        return None
    return _parse_iso(raw, source_id=source_id, field_name=field_name)


def _checkpoint_to_dict(checkpoint: SourceCheckpoint) -> dict:
    return {
        "plugin_id": checkpoint.plugin_id,
        "source_id": checkpoint.source_id,
        "tenant_id": checkpoint.tenant_id,
        "state": checkpoint.state.value,
        "cursor": checkpoint.cursor,
        "watermark": _iso(checkpoint.watermark) if checkpoint.watermark is not None else None,
        "last_settled_run_id": checkpoint.last_settled_run_id,
        "health_receipt_id": checkpoint.health_receipt_id,
        "updated_at": _iso(checkpoint.updated_at),
        "etag": checkpoint.etag,
        "last_modified": checkpoint.last_modified,
        "consecutive_failures": checkpoint.consecutive_failures,
        "backoff_until": _iso(checkpoint.backoff_until) if checkpoint.backoff_until is not None else None,
    }


def _checkpoint_from_dict(raw: object, *, source_id_hint: str) -> SourceCheckpoint:
    if not isinstance(raw, dict):
        raise CheckpointCorruptError(f"source {source_id_hint}: checkpoint entry must be an object")

    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise CheckpointCorruptError(f"source {source_id_hint}: source_id is missing or not a string")

    for key in ("plugin_id", "tenant_id", "cursor", "last_settled_run_id", "health_receipt_id"):
        if not isinstance(raw.get(key), str):
            raise CheckpointCorruptError(f"source {source_id}: {key} is missing or not a string")

    state_raw = raw.get("state")
    try:
        state = CheckpointState(state_raw)
    except ValueError as exc:
        raise CheckpointCorruptError(f"source {source_id}: state {state_raw!r} is not valid") from exc

    watermark = _parse_optional_iso(raw.get("watermark"), source_id=source_id, field_name="watermark")
    updated_at = _parse_iso(raw.get("updated_at"), source_id=source_id, field_name="updated_at")
    backoff_until = _parse_optional_iso(raw.get("backoff_until"), source_id=source_id, field_name="backoff_until")

    consecutive_failures = raw.get("consecutive_failures", 0)
    if not isinstance(consecutive_failures, int) or isinstance(consecutive_failures, bool):
        raise CheckpointCorruptError(f"source {source_id}: consecutive_failures must be an integer")

    return SourceCheckpoint(
        plugin_id=raw["plugin_id"],
        source_id=source_id,
        tenant_id=raw["tenant_id"],
        state=state,
        cursor=raw["cursor"],
        watermark=watermark,
        last_settled_run_id=raw["last_settled_run_id"],
        health_receipt_id=raw["health_receipt_id"],
        updated_at=updated_at,
        etag=raw.get("etag", "") if isinstance(raw.get("etag", ""), str) else "",
        last_modified=raw.get("last_modified", "") if isinstance(raw.get("last_modified", ""), str) else "",
        consecutive_failures=consecutive_failures,
        backoff_until=backoff_until,
    )


class JsonFileCheckpointStore:
    """File-backed ``CheckpointStore``: one JSON artifact for every source.

    Atomic write via temp file + rename (never a partial write survives a
    crash). ``load`` tolerates a missing file as a fresh start (every source
    at its initial, unloaded state). A corrupt file or an unknown schema
    version is refused with a typed error rather than silently starting
    over, because starting over would silently discard a real cursor.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._cache: dict[str, SourceCheckpoint] | None = None

    def _load_all(self) -> dict[str, SourceCheckpoint]:
        if self._cache is not None:
            return self._cache

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._cache = {}
            return self._cache

        try:
            payload = json.loads(raw_text)
        except ValueError as exc:
            raise CheckpointCorruptError(
                f"checkpoint file {self._path}: not valid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise CheckpointCorruptError(f"checkpoint file {self._path}: top level must be an object")

        version = payload.get("version")
        if version != SCHEMA_VERSION:
            raise CheckpointSchemaVersionError(
                f"checkpoint file {self._path}: unknown schema version {version!r} "
                f"(expected {SCHEMA_VERSION})"
            )

        checkpoints_raw = payload.get("checkpoints")
        if not isinstance(checkpoints_raw, dict):
            raise CheckpointCorruptError(f"checkpoint file {self._path}: checkpoints must be an object")

        loaded: dict[str, SourceCheckpoint] = {}
        for key, entry in checkpoints_raw.items():
            checkpoint = _checkpoint_from_dict(entry, source_id_hint=str(key))
            if checkpoint.source_id != key:
                raise CheckpointCorruptError(
                    f"checkpoint file {self._path}: key {key!r} does not match "
                    f"source_id {checkpoint.source_id!r}"
                )
            loaded[key] = checkpoint

        self._cache = loaded
        return self._cache

    def load(self, source_id: str) -> SourceCheckpoint | None:
        return self._load_all().get(source_id)

    def advance(self, checkpoint: SourceCheckpoint, *, reset: bool = False) -> SourceCheckpoint:
        current = dict(self._load_all())
        previous = current.get(checkpoint.source_id)
        written = _validate_advance(previous, checkpoint, reset=reset)

        current[checkpoint.source_id] = written
        payload = {
            "version": SCHEMA_VERSION,
            "checkpoints": {
                key: _checkpoint_to_dict(value) for key, value in current.items()
            },
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

        self._cache = current
        return written

    def all(self) -> Mapping[str, SourceCheckpoint]:
        return dict(self._load_all())
