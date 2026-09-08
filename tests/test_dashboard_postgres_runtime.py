from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
READING_MIGRATION = ROOT / "supabase/migrations/202609070001_reading_history.sql"
DASHBOARD_MIGRATION = ROOT / "supabase/migrations/202609080001_dashboard_summary.sql"
POSTGRES_IMAGE = "postgres:17.11"


def _run(*args: str, input_text: str | None = None, check: bool = True):
    return subprocess.run(
        args, input=input_text, text=True, capture_output=True, check=check, timeout=60
    )


def _psql(container: str, sql: str, *, check: bool = True):
    return _run(
        "docker", "exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-At", input_text=sql, check=check,
    )


def _wait(container: str) -> None:
    deadline = time.monotonic() + 30
    ready = 0
    while time.monotonic() < deadline:
        result = _psql(container, "select 1;", check=False)
        ready = ready + 1 if result.returncode == 0 and result.stdout.strip() == "1" else 0
        if ready == 2:
            return
        time.sleep(0.25)
    raise AssertionError("PostgreSQL 17.11 readiness deadline exceeded")


def test_dashboard_summary_is_owner_scoped_complete_and_cascade_safe() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker unavailable")
    if _run("docker", "image", "inspect", POSTGRES_IMAGE, check=False).returncode:
        pytest.skip(f"Local {POSTGRES_IMAGE} image unavailable")
    container = f"news-curator-dashboard-{uuid.uuid4().hex[:10]}"
    _run("docker", "run", "--rm", "-d", "--name", container,
         "-e", "POSTGRES_PASSWORD=review-only", POSTGRES_IMAGE)
    try:
        _wait(container)
        _psql(container, """
          create role anon nologin;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create schema extensions;
          create extension pgcrypto with schema extensions;
          create schema auth;
          create table auth.users(id uuid primary key);
          create function auth.uid() returns uuid language sql stable as $$
            select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
          $$;
          grant usage on schema public, auth to anon, authenticated, service_role;
          grant execute on function auth.uid() to anon, authenticated, service_role;
        """)
        _psql(container, READING_MIGRATION.read_text(encoding="utf-8"))
        _psql(container, DASHBOARD_MIGRATION.read_text(encoding="utf-8"))
        owner = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        urls = [f"https://publisher.example/{index}" for index in range(1, 4)]
        stories = [
            "story:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
            for url in urls
        ]
        values = ",".join(
            f"('{story}','{url}','Story {index}','Summary','en','outlet','Publisher',now())"
            for index, (story, url) in enumerate(zip(stories, urls, strict=True), 1)
        )
        _psql(container, f"""
          insert into auth.users values ('{owner}'), ('{other}');
          insert into public.canonical_stories
            (story_id,canonical_url,title,summary,language,source_kind,source_name,published_at)
            values {values};
          insert into public.story_topics(story_id,topic_id,topic_name) values
            ('{stories[0]}','ai','AI'), ('{stories[1]}','ai','AI'),
            ('{stories[2]}','energy','Energy');
          insert into public.user_story_state(user_id,story_id,read_at,saved_at) values
            ('{owner}','{stories[0]}',now(),now()),
            ('{owner}','{stories[1]}',null,now()),
            ('{owner}','{stories[2]}',now(),null),
            ('{other}','{stories[2]}',null,now());
          insert into public.user_story_interests(user_id,story_id,topic_id,signal) values
            ('{owner}','{stories[0]}','ai','more_like'),
            ('{owner}','{stories[1]}','ai','less_like'),
            ('{owner}','{stories[2]}','energy','more_like'),
            ('{other}','{stories[2]}','energy','less_like');
          update public.feed_policy set dashboard_topic_limit=1 where singleton;
        """)
        result = _psql(container, f"""
          set role authenticated;
          select set_config('request.jwt.claim.sub','{owner}',false);
          select public.dashboard_summary();
        """).stdout.splitlines()[-1]
        payload = json.loads(result)
        assert payload["scope"] == "current_retained_state"
        assert payload["saved_count"] == 2
        assert payload["saved_unread_count"] == 1
        assert payload["read_count"] == 2
        assert payload["active_interest_signal_count"] == 3
        assert len(payload["topic_signals"]) == 1
        assert payload["topic_signals"][0] == {
            "topic_id": "ai", "more_like_count": 1, "less_like_count": 1
        }

        anonymous = _psql(container, "set role anon; select public.dashboard_summary();", check=False)
        assert anonymous.returncode != 0
        assert "permission denied" in anonymous.stderr

        _psql(container, f"reset role; delete from auth.users where id='{owner}';")
        remaining = _psql(container, f"""
          select count(*) from public.user_story_state where user_id='{owner}';
          select count(*) from public.user_story_interests where user_id='{owner}';
        """).stdout.splitlines()
        assert remaining == ["0", "0"]
    finally:
        _run("docker", "stop", container, check=False)
