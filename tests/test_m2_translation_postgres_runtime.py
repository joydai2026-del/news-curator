"""The M2.1 translation migration APPLIED on PostgreSQL 17.11, not string-matched.

A CHECK constraint carrying a subquery is accepted by no PostgreSQL, so the only
proof that this migration is installable is installing it. Same container shape
as tests/test_discovery_postgres_runtime.py.
"""
from __future__ import annotations

import hashlib
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
SPEND_MIGRATIONS = (
    'supabase/migrations/202608290002_translation_store.sql',
    'supabase/migrations/202609160002_m2_translation_spend_and_decisions.sql',
    'supabase/migrations/202609160003_m2_exclusive_lane_from_decisions.sql',
)
POLICY = 'pairing-json-v1'


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
        for migration in SPEND_MIGRATIONS:
            _sql(container, (ROOT / migration).read_text())
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


def _story_id(url):
    """canonical_stories requires story_id == 'story:' + sha256(canonical_url)."""
    return 'story:' + hashlib.sha256(url.encode('utf-8')).hexdigest()


def _row(url, *, language='zh', title='独家：某部门发布七项新规', extra=None):
    payload = {
        'story_id': _story_id(url), 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url,
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
    assert _ingest(db, [_row('https://example.test/overlay-ok',
                             extra={'title_translations': {'en': 'Exclusive: seven new rules'}})]).returncode == 0
    rejected = _ingest(db, [_row('https://example.test/overlay-bad',
                                 extra={'title_translations': {'fr': 'Interdit'}})], check=False)
    assert rejected.returncode != 0 and 'invalid translation overlay' in rejected.stderr
    # The CHECK itself, reached directly rather than through the RPC guard.
    direct = _sql(db, "update public.retained_corpus_observations "
                      "set title_translations = '{\"fr\":\"Interdit\"}'::jsonb;", check=False)
    assert direct.returncode != 0 and 'retained_corpus_title_translations_shape' in direct.stderr


def test_a_later_observation_merges_translations_and_never_downgrades_a_group(db):
    url = 'https://example.test/merge-case'
    story = _story_id(url)
    group = 'group:' + '0' * 32
    _ingest(db, [_row(url, extra={'title_translations': {'en': 'First English title'},
                                    'event_group_id': group,
                                    'source_observed_at': '2026-09-16T10:00:00Z'})])
    _ingest(db, [_row(url, extra={'summary_translations': {'en': 'A later summary'},
                                    'source_observed_at': '2026-09-16T11:00:00Z'})])
    result = _sql(db, "select title_translations->>'en', summary_translations->>'en', event_group_id "
                      f"from public.retained_corpus_observations where story_id={_quote(story)};")
    title, summary, stored_group = result.stdout.strip().split('|')
    assert title == 'First English title'      # not erased by the later batch
    assert summary == 'A later summary'        # merged in
    assert stored_group == group               # not downgraded to null


def _decide(container, story_id, outcome, *, match=None, language='en'):
    match_sql = _quote(match) if match else 'null'
    return _spend(container, f"public.m2_record_exclusivity_decision({_quote(story_id)}, {_quote(language)}, "
                             f"{_quote(POLICY)}, 'gpt-5-mini', {_quote(outcome)}, {match_sql})")


def test_the_exclusive_lane_returns_only_the_stories_the_model_ruled_exclusive(db):
    """Matched and undecided stories must NOT appear. Absence of a group id is
    not evidence of exclusivity: it is what an unasked story looks like."""
    matched_url = 'https://example.test/lane-matched'
    undecided_url = 'https://example.test/lane-undecided'
    exclusive_url = 'https://example.test/lane-exclusive'
    peer_url = 'https://example.test/lane-english-peer'
    matched, undecided, exclusive, peer = (_story_id(matched_url), _story_id(undecided_url),
                                           _story_id(exclusive_url), _story_id(peer_url))
    _ingest(db, [
        _row(matched_url),
        _row(undecided_url),
        _row(exclusive_url),
        _row(peer_url, language='en', title='An English wire story about the same event'),
    ])
    _decide(db, matched, 'matched', match=peer)
    _decide(db, undecided, 'undecided')
    _decide(db, exclusive, 'exclusive')
    rows = _candidates(db, "public.m2_retained_candidates_language_exclusive('en',null,null,null,100)")
    assert [row['story_id'] for row in rows] == [exclusive], [row['story_id'] for row in rows]


def test_a_decision_from_another_policy_is_not_inherited(db):
    url = 'https://example.test/lane-old-policy'
    story = _story_id(url)
    _ingest(db, [_row(url)])
    _spend(db, f"public.m2_record_exclusivity_decision({_quote(story)}, 'en', 'pairing-json-v0', "
               f"'gpt-5-mini', 'exclusive', null)")
    rows = _candidates(db, "public.m2_retained_candidates_language_exclusive("
                           f"'en',null,null,null,100,{_quote(POLICY)})")
    assert story not in {row['story_id'] for row in rows}


def test_a_matched_pair_sharing_a_group_id_is_never_exclusive(db):
    """Both rows carry the group id, which is what the ingest now writes."""
    zh_url = 'https://example.test/lane-pair-zh'
    en_url = 'https://example.test/lane-pair-en'
    zh_story, en_story = _story_id(zh_url), _story_id(en_url)
    group = 'group:' + '2' * 32
    _ingest(db, [
        _row(zh_url, extra={'event_group_id': group}),
        _row(en_url, language='en', title='The English peer', extra={'event_group_id': group}),
    ])
    # Even a stale EXCLUSIVE decision cannot resurrect a story whose group has
    # a display-language member.
    _decide(db, zh_story, 'exclusive')
    rows = _candidates(db, "public.m2_retained_candidates_language_exclusive('en',null,null,null,100)")
    assert zh_story not in {row['story_id'] for row in rows}


def test_the_read_rpc_still_returns_the_translation_overlay(db):
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    assert rows and all({'title_translations', 'summary_translations', 'event_group_id'} <= set(row) for row in rows)


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


def _spend(container, expression):
    result = _sql(container, "set role service_role;"
                  "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                  f"select {expression};")
    return json.loads(result.stdout.splitlines()[-1])


def test_the_daily_dollar_cap_is_persisted_and_shared_across_runs(db):
    """An in-memory ledger resets twelve times an hour; this one does not."""
    start = _spend(db, "public.m2_read_translation_spend()")
    limit = float(start['usd_settled']) + float(start['usd_reserved']) + 0.01
    first = _spend(db, f"public.m2_reserve_translation_spend(0.006, {limit})")
    assert first['status'] == 'reserved'
    _spend(db, "public.m2_settle_translation_spend(0.006, 0.006)")
    # A SECOND run, same UTC day, same cap: the remaining room is what is left.
    second = _spend(db, f"public.m2_reserve_translation_spend(0.006, {limit})")
    assert second['status'] == 'cost_limit_reached'
    room = _spend(db, f"public.m2_reserve_translation_spend(0.003, {limit})")
    assert room['status'] == 'reserved'


def test_a_settled_decision_is_written_once_but_an_undecided_one_may_be_retried(db):
    story = _story_id('https://example.test/decision-one')
    other = _story_id('https://example.test/decision-match')
    first = _decide(db, story, 'exclusive')
    assert first['outcome'] == 'exclusive' and first['attempts'] == 1
    # A later run must not flip a published story into a group.
    second = _decide(db, story, 'matched', match=other)
    assert second['outcome'] == 'exclusive' and second['decided_at'] == first['decided_at']
    # An UNDECIDED row is different: it is the "ask again, but not for ever" state.
    pending = _story_id('https://example.test/decision-pending')
    one = _decide(db, pending, 'undecided')
    two = _decide(db, pending, 'undecided')
    assert one['attempts'] == 1 and two['attempts'] == 2
    settled = _decide(db, pending, 'exclusive')
    assert settled['outcome'] == 'exclusive'
    rows = _candidates(db, f"public.m2_read_exclusivity_decisions(array[{_quote(story)}]::text[], 'en', {_quote(POLICY)})")
    assert len(rows) == 1 and rows[0]['policy_id'] == POLICY


def test_a_pre_send_release_returns_the_reservation_to_the_day(db):
    start = _spend(db, "public.m2_read_translation_spend()")
    limit = float(start['usd_settled']) + float(start['usd_reserved']) + 0.02
    assert _spend(db, f"public.m2_reserve_translation_spend(0.01, {limit})")['status'] == 'reserved'
    _spend(db, "public.m2_release_translation_spend(0.01)")
    after = _spend(db, "public.m2_read_translation_spend()")
    assert float(after['usd_reserved']) == float(start['usd_reserved'])


def test_pairing_calls_are_capped_per_utc_day_not_per_process(db):
    before = _spend(db, "public.m2_read_translation_spend()")
    calls = int(before['pairing_calls'])
    assert _spend(db, f"public.m2_reserve_pairing_call(0.0001, 100, {calls + 1})")['status'] == 'reserved'
    refused = _spend(db, f"public.m2_reserve_pairing_call(0.0001, 100, {calls + 1})")
    assert refused['status'] == 'call_limit_reached'


def test_settlement_lands_on_the_day_it_reserved_against(db):
    """A run that crosses midnight must not lose its charge."""
    yesterday = '2026-09-15'
    settled = _spend(db, f"public.m2_settle_translation_spend(0, 0.004, {_quote(yesterday)})")
    assert settled['scope_key'] == yesterday


def test_the_spend_and_decision_rpcs_are_service_role_only(db):
    for expression in ("public.m2_read_translation_spend()",
                       "public.m2_reserve_translation_spend(0.001, 1)",
                       "public.m2_reserve_pairing_call(0.001, 1, 10)",
                       "public.m2_release_translation_spend(0.001)",
                       "public.m2_read_exclusivity_decisions(array[]::text[], 'en', 'pairing-json-v1')"):
        denied = _sql(db, f"set role authenticated; select {expression};", check=False)
        assert denied.returncode != 0 and 'permission denied' in denied.stderr.lower()
