"""The M2.1 translation migration APPLIED on PostgreSQL 17.11, not string-matched.

A CHECK constraint carrying a subquery is accepted by no PostgreSQL, so the only
proof that this migration is installable is installing it. Same container shape
as tests/test_discovery_postgres_runtime.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'postgres:17.11'
MIGRATIONS = (
    'supabase/migrations/202609070001_reading_history.sql',
    'supabase/migrations/202609140002_m2_retained_corpus.sql',
    'supabase/migrations/202609160001_m2_translation_columns.sql',
)


def _run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=60, check=check)


def _sql(container, sql, check=True):
    return _run('docker', 'exec', '-i', container, 'psql', '-X', '-At', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', input_text=sql, check=check)


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


@pytest.fixture(scope='module')
def db():
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable; PostgreSQL 17.11 runtime not verified')
    if _run('docker', 'image', 'inspect', IMAGE, check=False).returncode:
        pytest.skip('Installed PostgreSQL 17.11 image or Docker daemon unavailable; no pull attempted')
    container = 'news-curator-translation-' + uuid.uuid4().hex[:10]
    _run('docker', 'run', '--rm', '-d', '--name', container, '-e', 'POSTGRES_PASSWORD=review-only', IMAGE)
    try:
        deadline = time.monotonic() + 30
        ready = 0
        while time.monotonic() < deadline:
            result = _sql(container, 'select 1;', check=False)
            ready = ready + 1 if result.returncode == 0 and result.stdout.strip() == '1' else 0
            if ready >= 2:
                break
            time.sleep(.25)
        else:
            pytest.fail('PostgreSQL 17.11 readiness timeout')
        _sql(container, """
          create role anon nologin;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create schema extensions;
          create extension pgcrypto with schema extensions;
          create schema auth;
          create table auth.users(id uuid primary key);
          create function auth.uid() returns uuid language sql stable as $$
            select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
          create function auth.jwt() returns jsonb language sql stable as $$
            select coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
          grant usage on schema public,auth to anon,authenticated,service_role;
          grant execute on function auth.uid(), auth.jwt() to anon,authenticated,service_role;
        """)
        for migration in MIGRATIONS:
            _sql(container, (ROOT / migration).read_text())
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


def _row(story_id, *, language='zh', title='独家：某部门发布七项新规', url=None, extra=None):
    payload = {
        'story_id': story_id, 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url or f'https://example.test/{story_id[-6:]}',
        'title': title, 'summary': '摘要内容。', 'language': language,
        'source_id': 'fixture', 'source_name': 'Fixture Wire', 'source_is_aggregator': False,
        'published_at': '2026-09-16T10:00:00Z', 'source_observed_at': '2026-09-16T10:00:00Z',
        'category_ids': ['world'],
    }
    payload.update(extra or {})
    return payload


def _ingest(container, rows, *, check=True):
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);", check=check)


def _candidates(container, expression):
    result = _sql(container, "set role service_role;"
                  "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                  f"select coalesce(jsonb_agg(value), '[]'::jsonb) from {expression} as rows(value);")
    return json.loads(result.stdout.splitlines()[-1])


def test_the_migration_applies_and_the_columns_exist(db):
    result = _sql(db, "select column_name from information_schema.columns "
                      "where table_name='retained_corpus_observations' "
                      "and column_name in ('title_translations','summary_translations','event_group_id') "
                      "order by column_name;")
    assert result.stdout.split() == ['event_group_id', 'summary_translations', 'title_translations']


def test_the_overlay_check_accepts_supported_languages_and_refuses_everything_else(db):
    story = 'story:' + 'a' * 58
    assert _ingest(db, [_row(story, extra={'title_translations': {'en': 'Exclusive: seven new rules'}})]).returncode == 0
    rejected = _ingest(db, [_row('story:' + 'b' * 58, extra={'title_translations': {'fr': 'Interdit'}})], check=False)
    assert rejected.returncode != 0 and 'invalid translation overlay' in rejected.stderr
    # The CHECK itself, reached directly rather than through the RPC guard.
    direct = _sql(db, "update public.retained_corpus_observations "
                      "set title_translations = '{\"fr\":\"Interdit\"}'::jsonb;", check=False)
    assert direct.returncode != 0 and 'retained_corpus_title_translations_shape' in direct.stderr


def test_a_later_observation_merges_translations_and_never_downgrades_a_group(db):
    story = 'story:' + 'c' * 58
    group = 'group:' + '0' * 32
    _ingest(db, [_row(story, extra={'title_translations': {'en': 'First English title'},
                                    'event_group_id': group,
                                    'source_observed_at': '2026-09-16T10:00:00Z'})])
    _ingest(db, [_row(story, extra={'summary_translations': {'en': 'A later summary'},
                                    'source_observed_at': '2026-09-16T11:00:00Z'})])
    result = _sql(db, "select title_translations->>'en', summary_translations->>'en', event_group_id "
                      f"from public.retained_corpus_observations where story_id={_quote(story)};")
    title, summary, stored_group = result.stdout.strip().split('|')
    assert title == 'First English title'      # not erased by the later batch
    assert summary == 'A later summary'        # merged in
    assert stored_group == group               # not downgraded to null


def test_the_read_rpc_returns_the_overlay_and_the_exclusive_rpc_filters_by_group(db):
    zh_paired = 'story:' + 'd' * 58
    en_paired = 'story:' + 'e' * 58
    zh_alone = 'story:' + 'f' * 58
    group = 'group:' + '1' * 32
    _ingest(db, [
        _row(zh_paired, extra={'event_group_id': group}),
        _row(en_paired, language='en', title='An English wire story', extra={'event_group_id': group}),
        _row(zh_alone, extra={'title_translations': {'en': 'Only the Chinese press ran this'}}),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    assert rows and all({'title_translations', 'summary_translations', 'event_group_id'} <= set(row) for row in rows)
    exclusive = _candidates(db, "public.m2_retained_candidates_language_exclusive('en',null,null,null,100)")
    ids = {row['story_id'] for row in exclusive}
    assert zh_alone in ids                       # no English outlet carried it
    assert zh_paired not in ids                  # its group has an English member
    assert en_paired not in ids                  # already in the display language
    assert all(row['language'] != 'en' for row in exclusive)


def test_the_exclusive_rpc_is_service_role_only(db):
    for role in ('anon', 'authenticated'):
        denied = _sql(db, f"set role {role}; "
                          "select public.m2_retained_candidates_language_exclusive('en',null,null,null,10);",
                      check=False)
        assert denied.returncode != 0 and 'permission denied' in denied.stderr.lower()


def test_the_exclusive_rpc_refuses_an_unsupported_display_language(db):
    denied = _sql(db, "set role service_role;"
                      "select public.m2_retained_candidates_language_exclusive('fr',null,null,null,10);", check=False)
    assert denied.returncode != 0 and 'invalid display language' in denied.stderr
