#!/usr/bin/env python3
"""Rehearse a bounded public historical import in a newly named local database.

The caller must supply the authorization reference. This script rejects remote
hosts and leaves its uniquely named isolated database intact for its receipt.
It rolls the selected rows back to the pre-import set instead of dropping a
database, preserving JJ's archive-not-delete convention.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.retained_corpus import public_ingest_rows, retain
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest


def validate_local_socket(host: str, port: int) -> Path:
    path = Path(host)
    if not host or not path.is_absolute() or not path.is_dir() or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("host must be an existing absolute local Unix-socket directory and port must be valid")
    socket_path = path / f".s.PGSQL.{port}"
    if not socket_path.exists() or not socket_path.is_socket():
        raise ValueError("requested local PostgreSQL socket is unavailable")
    return path


def _sql_literal(value: object) -> str:
    return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'"


def normalize_timestamp(value: str) -> str:
    """Canonical UTC receipt form for a real instant, never a display string."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_sql(psql: str, database: str, statement: str, *, host: Path, port: int) -> str:
    result = subprocess.run(
        [psql, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-h", str(host), "-p", str(port), database],
        input=statement, text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError("local PostgreSQL command failed")
    return result.stdout.strip()


def select_rows(snapshot_path: Path, *, root: Path, limit: int) -> tuple[dict[str, Any], list[dict[str, object]], list[dict[str, object]]]:
    """Validate the frozen source artifact then select deterministic real public rows."""
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    cfg = load_config(root)
    snapshot = load_source_snapshot(snapshot_path)
    if snapshot.configuration_digest != snapshot_config_digest(cfg):
        raise ValueError("snapshot configuration digest does not match current configured sources")
    allowed = {spec.id for spec in configured_source_specs(cfg)}
    all_items = [item for result in snapshot.results for item in result.items]
    cutoff = snapshot.generated_at - timedelta(hours=24)
    unexpected = [item for item in all_items if item.source_id not in allowed]
    historical = [item for item in all_items if item.source_id in allowed and not item.is_newsletter and item.published_at < cutoff]
    historical.sort(key=lambda item: (item.published_at, item.canonical_url))
    chosen, remainder = historical[:limit], historical[limit:]
    if not chosen or not remainder:
        raise ValueError("snapshot lacks a bounded historical subset and one unaffected public item")
    selected = public_ingest_rows(retain(chosen, categories=cfg.categories, observed_at=snapshot.generated_at), allowed_source_ids=allowed)
    unaffected = public_ingest_rows(retain([remainder[0]], categories=cfg.categories, observed_at=snapshot.generated_at), allowed_source_ids=allowed)
    if len(selected) != len(chosen) or len(unaffected) != 1:
        raise ValueError("deduplication changed the bounded rehearsal selection")
    artifact_bytes = snapshot_path.read_bytes()
    return {
        "criterion": "NC2-A10",
        "environment": "local isolated PostgreSQL only",
        "scope_exclusions": ["newsletter", "mailbox", "browser_history", "private_history", "production"],
        "source_artifact": {"path": str(snapshot_path), "sha256": hashlib.sha256(artifact_bytes).hexdigest(), "content_digest": snapshot.content_digest, "configuration_digest": snapshot.configuration_digest, "generated_at": snapshot.generated_at.isoformat()},
        "selection": {"historical_cutoff_utc": normalize_timestamp(cutoff.isoformat()), "source_item_count": len(all_items), "historical_candidate_count": len(historical), "selected_count": len(selected), "unaffected_count": len(unaffected), "selected_story_ids": [str(row["story_id"]) for row in selected], "selected_published_at": {str(row["story_id"]): normalize_timestamp(str(row["published_at"])) for row in selected}, "excluded": {"unconfigured_source": len(unexpected), "newsletter": sum(item.is_newsletter for item in all_items), "not_older_than_24_hours": sum(item.source_id in allowed and not item.is_newsletter and item.published_at >= cutoff for item in all_items), "outside_bounded_selection": len(remainder) - 1}},
    }, selected, unaffected


def _transaction_sql(selected_json: str, unaffected_story: str, expected_ids: list[str], expected_timestamps: dict[str, str]) -> str:
    ids = ",".join(_sql_literal(value) for value in expected_ids)
    expected = _sql_literal(json.dumps(sorted(expected_ids + [unaffected_story])))
    timestamp_values = ",".join(f"({_sql_literal(story)}, {_sql_literal(timestamp)}::timestamptz)" for story, timestamp in expected_timestamps.items())
    service = "set role service_role; set request.jwt.claims = '{\"role\":\"service_role\"}'; "
    return service + f"""
begin;
create temporary table rehearsal_checks(name text primary key, passed boolean not null) on commit drop;
with imported as (select public.m2_ingest_retained_corpus({_sql_literal(selected_json)}::jsonb) as count)
insert into rehearsal_checks select 'initial bounded import', count={len(expected_ids)} from imported;
with replay as (select public.m2_ingest_retained_corpus({_sql_literal(selected_json)}::jsonb) as count)
insert into rehearsal_checks select 'identical reimport idempotent', count=0 from replay;
insert into rehearsal_checks
select 'original publication timestamps preserved', bool_and(extract(epoch from o.published_at)=extract(epoch from expected_rows.expected_at))
from public.retained_corpus_observations o join (values {timestamp_values}) as expected_rows(story_id,expected_at) using(story_id);
delete from public.retained_corpus_categories where story_id in ({ids});
delete from public.retained_corpus_observations where story_id in ({ids});
insert into rehearsal_checks select 'bounded removal deletes selected rows', count(*)=0 from public.retained_corpus_observations where story_id in ({ids});
insert into rehearsal_checks select 'unaffected row survives removal', exists(select 1 from public.retained_corpus_observations where story_id={_sql_literal(unaffected_story)});
with rebuilt as (select public.m2_ingest_retained_corpus({_sql_literal(selected_json)}::jsonb) as count)
insert into rehearsal_checks select 'clean rebuild restores selected count', count={len(expected_ids)} from rebuilt;
insert into rehearsal_checks select 'clean rebuild restores exact expected set', coalesce(jsonb_agg(story_id order by story_id), '[]'::jsonb)={expected}::jsonb from public.retained_corpus_observations;
select coalesce(jsonb_agg(jsonb_build_object('name',name,'passed',passed) order by name),'[]'::jsonb) from rehearsal_checks;
rollback;
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--port", type=int, default=5432)
    args = parser.parse_args()
    receipt: dict[str, Any] = {"criterion": "NC2-A10", "status": "blocked_before_execution", "checks": [], "database_created": False, "cleanup": {"method": "transaction_rollback_to_pre_import_set", "completed": False}}
    database: str | None = None
    try:
        host = validate_local_socket(args.host, args.port)
        if not args.authorization_reference.strip():
            raise ValueError("authorization reference is required")
        metadata, selected, unaffected = select_rows(args.snapshot, root=args.root, limit=args.limit)
        receipt.update(metadata, authorization_reference=args.authorization_reference)
        psql = shutil.which("psql")
        if not psql:
            raise RuntimeError("psql unavailable")
        database = "nc_m2_import_" + uuid.uuid4().hex[:12]
        receipt["database"] = database
        _run_sql(psql, "postgres", "create database " + database, host=host, port=args.port)
        receipt["database_created"] = True
        _run_sql(psql, database, """create schema auth; create schema extensions; create extension pgcrypto with schema extensions; create table auth.users(id uuid primary key); create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$; create function auth.jwt() returns jsonb language sql stable as $$ select coalesce(nullif(current_setting('request.jwt.claims',true),'')::jsonb,'{}'::jsonb) $$; grant usage on schema auth to anon,authenticated,service_role; grant execute on all functions in schema auth to anon,authenticated,service_role;""", host=host, port=args.port)
        migrations = []
        for path in sorted((args.root / "supabase/migrations").glob("*.sql")):
            data = path.read_text(encoding="utf-8")
            _run_sql(psql, database, data, host=host, port=args.port)
            migrations.append({"file": path.name, "sha256": hashlib.sha256(data.encode()).hexdigest()})
        receipt["migrations"] = migrations
        service = "set role service_role; set request.jwt.claims = '{\"role\":\"service_role\"}'; "
        _run_sql(psql, database, service + f"select public.m2_ingest_retained_corpus({_sql_literal(json.dumps(unaffected, ensure_ascii=False))}::jsonb);", host=host, port=args.port)
        output = _run_sql(psql, database, _transaction_sql(json.dumps(selected, ensure_ascii=False), str(unaffected[0]["story_id"]), [str(row["story_id"]) for row in selected], receipt["selection"]["selected_published_at"]), host=host, port=args.port)
        checks = json.loads(output.splitlines()[-2])
        receipt["checks"] = checks
        selected_ids = ",".join(_sql_literal(str(row["story_id"])) for row in selected)
        rollback = _run_sql(psql, database, f"select count(*)=0 from public.retained_corpus_observations where story_id in ({selected_ids});", host=host, port=args.port)
        anchor = _run_sql(psql, database, f"select exists(select 1 from public.retained_corpus_observations where story_id={_sql_literal(unaffected[0]['story_id'])});", host=host, port=args.port)
        receipt["cleanup"]["completed"] = rollback.splitlines()[-1:] == ["t"] and anchor.splitlines()[-1:] == ["t"]
        receipt["status"] = "pass" if all(check["passed"] for check in checks) and receipt["cleanup"]["completed"] else "fail"
    except Exception as exc:
        receipt["status"] = "fail"
        receipt["error_class"] = type(exc).__name__
    finally:
        receipt["database"] = database
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
