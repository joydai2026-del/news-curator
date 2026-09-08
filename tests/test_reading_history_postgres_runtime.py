from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/202609070001_reading_history.sql"
POSTGRES_IMAGE = "postgres:15"


def _run(
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    timeout_seconds: float = 60,
):
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout_seconds,
    )


def _psql(container: str, sql: str, *, database: str = "review") -> str:
    result = _run(
        "docker", "exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", database, "-At", input_text=sql,
    )
    return result.stdout


def _candidate(
    *, nonce: str, built_at: datetime, stories: list[tuple[str, str, datetime]]
) -> dict:
    story_rows = []
    entries = []
    aliases = []
    for position, (story_id, url, published_at) in enumerate(stories, start=1):
        story_rows.append({
            "story_id": story_id,
            "canonical_url": url,
            "title": f"Story {position}",
            "summary": f"Summary {position}",
            "language": "en",
            "published_at": published_at.isoformat(),
            "source_kind": "outlet",
            "source_name": "Publisher",
            "distinct_coverage_source_count": 1,
        })
        aliases.append({
            "normalized_url": url,
            "story_id": story_id,
            "match_method": "exact",
        })
        entries.append({
            "story_id": story_id,
            "topic_id": "ai",
            "position": position,
            "score_components": {"freshness": 1, "final_score": 1},
            "ordering_mode": "weighted_total",
            "ordering_key": {"weighted_total": 1},
            "topic_ranks": {"ai": position},
            "source_kind": "outlet",
            "source_name": "Publisher",
            "ranking_explanation": "Weighted using freshness.",
        })
    return {
        "schema_version": 1,
        "build_nonce": nonce,
        "commit_sha": "a" * 40,
        "site_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
        "built_at": built_at.isoformat(),
        "stories": story_rows,
        "aliases": aliases,
        "coverage_mentions": [],
        "topics": [{"topic_id": "ai", "name": "AI"}],
        "entries": entries,
    }


def _jsonb(value: object) -> str:
    return "'" + json.dumps(value, separators=(",", ":")).replace("'", "''") + "'::jsonb"


def _postgres_readiness(container: str) -> tuple[bool, str]:
    pid_one = _run(
        "docker", "exec", container, "cat", "/proc/1/comm",
        check=False, timeout_seconds=5,
    )
    process_name = pid_one.stdout.strip()
    if pid_one.returncode != 0 or process_name != "postgres":
        detail = (pid_one.stderr or pid_one.stdout).strip()
        return False, f"pid 1={process_name or '<unavailable>'}; {detail}".strip()

    query = _run(
        "docker", "exec", container, "psql", "-X", "-U", "postgres",
        "-At", "-c", "select 1", check=False, timeout_seconds=5,
    )
    ready = query.returncode == 0 and query.stdout.strip() == "1"
    detail = (query.stderr or query.stdout).strip()
    return ready, f"pid 1=postgres; query={detail or '<no output>'}"


def _wait_for_postgres(
    container: str,
    *,
    deadline_seconds: float = 30,
    probe: Callable[[str], tuple[bool, str]] = _postgres_readiness,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + deadline_seconds
    consecutive_ready = 0
    last_detail = "no readiness probe completed"
    while monotonic() < deadline:
        ready, last_detail = probe(container)
        consecutive_ready = consecutive_ready + 1 if ready else 0
        if consecutive_ready >= 2:
            return
        sleep(0.25)

    logs = _run(
        "docker", "logs", "--tail", "80", container,
        check=False, timeout_seconds=5,
    )
    diagnostic = (logs.stderr or logs.stdout).strip()
    pytest.fail(
        "PostgreSQL final postmaster did not become stably ready within "
        f"{deadline_seconds:.1f}s. Last probe: {last_detail}. "
        f"Container logs: {diagnostic or '<empty>'}",
        pytrace=False,
    )


def test_wait_for_postgres_requires_stable_final_postmaster() -> None:
    observations = iter([
        (False, "entrypoint pid 1 is docker-entrypoint.sh"),
        (True, "final postmaster ready"),
        (False, "temporary server stopped"),
        (True, "final postmaster ready"),
        (True, "final postmaster ready"),
    ])
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    _wait_for_postgres(
        "test-container",
        deadline_seconds=2.0,
        probe=lambda _container: next(observations),
        monotonic=lambda: clock[0],
        sleep=advance,
    )
    assert list(observations) == []


def test_republication_uses_corrected_publisher_time_in_story_entry_and_feed() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable.")
    if _run("docker", "image", "inspect", POSTGRES_IMAGE, check=False).returncode:
        pytest.skip(f"Local {POSTGRES_IMAGE} image is unavailable.")

    container = f"news-curator-published-at-{uuid.uuid4().hex[:10]}"
    _run(
        "docker", "run", "--rm", "-d", "--name", container,
        "-e", "POSTGRES_PASSWORD=review-only", POSTGRES_IMAGE,
    )
    try:
        _wait_for_postgres(container)
        _run(
            "docker", "exec", container, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1",
            "-c", "create role anon nologin; create role authenticated nologin; "
            "create role service_role nologin bypassrls;",
            "-c", "create database review;",
        )
        _psql(container, """
            create schema extensions;
            create extension pgcrypto with schema extensions;
            create schema auth;
            create table auth.users(id uuid primary key);
            create function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
            grant usage on schema public, auth to anon, authenticated, service_role;
            grant execute on function auth.uid() to anon, authenticated, service_role;
        """)
        _psql(container, MIGRATION.read_text(encoding="utf-8"))

        now = datetime.now(timezone.utc).replace(microsecond=0)
        primary_url = "https://publisher.example/corrected"
        peer_url = "https://publisher.example/peer"
        primary_id = "story:" + hashlib.sha256(primary_url.encode()).hexdigest()
        peer_id = "story:" + hashlib.sha256(peer_url.encode()).hexdigest()
        original_time = now - timedelta(hours=2)
        corrected_time = now - timedelta(minutes=10)
        peer_time = now - timedelta(minutes=20)
        first = _candidate(
            nonce="original-time", built_at=now - timedelta(hours=1),
            stories=[(primary_id, primary_url, original_time)],
        )
        corrected = _candidate(
            nonce="corrected-time", built_at=now,
            stories=[(primary_id, primary_url, corrected_time), (peer_id, peer_url, peer_time)],
        )
        _psql(container, f"set role service_role; select public.finalize_archive({_jsonb(first)}, 'https://news.example/');")
        _psql(container, f"set role service_role; select public.finalize_archive({_jsonb(corrected)}, 'https://news.example/');")

        result = _psql(container, f"""
            select to_char(published_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
              from public.canonical_stories where story_id = '{primary_id}';
            select to_char(pe.published_at at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
              from public.publication_entries pe
              join public.publication_runs pr using (publication_seq)
             where pr.build_nonce = 'corrected-time' and pe.story_id = '{primary_id}';
            set role anon;
            select value->>'story_id' from public.feed_page(
              null, 'history_freshness', null, null, null, null, 20
            ) with ordinality as page(value, position) order by position;
        """).splitlines()
        expected = corrected_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert result[:2] == [expected, expected]
        assert result[-2:] == [primary_id, peer_id]
    finally:
        _run("docker", "stop", container, check=False)
