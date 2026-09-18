"""The two B8 corpus bugs, proven on PostgreSQL 17.11 with every migration applied.

A string-matched migration test proves nothing, so these APPLY the migrations
and call the real RPCs. Same container shape as
tests/test_m2_translation_postgres_runtime.py.

Covers:
  * duplicate stories in one result (identical title, same language, different
    canonical URL, so two durable corpus rows), and
  * the category floor: no retained row may reach the corpus with no category.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.config import load_config
from curator.models import Item
from curator.retained_corpus import public_ingest_rows, retain

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'postgres:17.11'
MIGRATIONS = (
    'supabase/migrations/202609070001_reading_history.sql',
    'supabase/migrations/202609140002_m2_retained_corpus.sql',
    'supabase/migrations/202609160001_m2_translation_columns.sql',
    'supabase/migrations/202609170001_m2_retained_overlay.sql',
    'supabase/migrations/202608290002_translation_store.sql',
    'supabase/migrations/202609160002_m2_translation_spend_and_decisions.sql',
    'supabase/migrations/202609160003_m2_exclusive_lane_from_decisions.sql',
    'supabase/migrations/202609180101_m2_retained_candidates_dedupe.sql',
)
POLICY = 'pairing-json-v1'
NOW = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=120, check=check)


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
    container = 'news-curator-dedupe-' + uuid.uuid4().hex[:10]
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


def _story_id(url):
    return 'story:' + hashlib.sha256(url.encode('utf-8')).hexdigest()


def _row(url, *, language='zh', title='中国出入境新规9.15上路 台湾陆委会示警5类人有风险',
         published_at='2026-09-16T10:00:00Z', source_id='rfi-zh', categories=('world',)):
    return {
        'story_id': _story_id(url), 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url, 'title': title, 'summary': '摘要内容。', 'language': language,
        'source_id': source_id, 'source_name': 'RFI Chinese', 'source_is_aggregator': False,
        'published_at': published_at, 'source_observed_at': published_at,
        'category_ids': list(categories),
    }


def _ingest(container, rows, *, check=True):
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);", check=check)


def _candidates(container, expression):
    result = _sql(container, "set role service_role;"
                  "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                  f"select coalesce(jsonb_agg(value), '[]'::jsonb) from {expression} as rows(value);")
    return json.loads(result.stdout.splitlines()[-1])


def test_two_addresses_for_one_headline_return_one_row(db):
    # The reported shape: rfi-zh served one story at two URLs, so the corpus
    # holds two rows with identical titles and the feed showed the story twice.
    first, second = 'https://www.rfi.fr/cn/a-1?x=1', 'https://www.rfi.fr/cn/a-1'
    _ingest(db, [_row(first, published_at='2026-09-16T09:00:00Z'),
                 _row(second, published_at='2026-09-16T10:00:00Z')])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    titles = [row['title'] for row in rows]
    assert len(titles) == len(set(titles)), titles
    kept = [row['story_id'] for row in rows if row['title'].startswith('中国出入境新规')]
    # The newer observation is the representative, chosen by the function's own
    # order, never by which page the caller asked for.
    assert kept == [_story_id(second)]


def test_the_collapse_is_case_and_whitespace_folded_but_never_fuzzy(db):
    _ingest(db, [
        _row('https://example.test/fold-a', title='Apple releases iOS 18.6.1',
             language='en', published_at='2026-09-16T08:00:00Z'),
        _row('https://example.test/fold-b', title='APPLE   releases  iOS 18.6.1',
             language='en', published_at='2026-09-16T08:30:00Z'),
        # One digit apart is a DIFFERENT release. Exact-only collapsing keeps it.
        _row('https://example.test/fold-c', title='Apple releases iOS 18.6.2',
             language='en', published_at='2026-09-16T08:45:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/fold-b') in ids
    assert _story_id('https://example.test/fold-a') not in ids
    assert _story_id('https://example.test/fold-c') in ids


def test_the_same_headline_in_two_languages_is_never_collapsed(db):
    shared = 'Same headline, two languages'
    _ingest(db, [
        _row('https://example.test/lang-en', title=shared, language='en',
             published_at='2026-09-16T07:00:00Z'),
        _row('https://example.test/lang-zh', title=shared, language='zh',
             published_at='2026-09-16T07:30:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/lang-en') in ids
    assert _story_id('https://example.test/lang-zh') in ids


def test_a_repeat_outside_the_window_is_kept(db):
    # A recurring headline (a daily briefing) is two real stories, not one.
    title = 'Morning briefing: what happened overnight'
    _ingest(db, [
        _row('https://example.test/repeat-day-1', title=title, language='en',
             published_at='2026-09-10T06:00:00Z'),
        _row('https://example.test/repeat-day-3', title=title, language='en',
             published_at='2026-09-13T06:00:00Z'),
    ])
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100)")
    ids = {row['story_id'] for row in rows}
    assert _story_id('https://example.test/repeat-day-1') in ids
    assert _story_id('https://example.test/repeat-day-3') in ids


def test_the_representative_does_not_change_with_the_page_boundary(db):
    # A per-page rule would collapse a duplicate on page 1 and then show the
    # loser again on page 2, because page 2's window no longer contains the
    # winner. The rule here is decided against the whole table, so it cannot.
    # Distinctness is asserted per (language, title): the same headline in two
    # languages is two stories and is SUPPOSED to appear twice.
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,3)")
    assert len(rows) == 3
    last = rows[-1]
    more = _candidates(db, "public.m2_retained_candidates(null,null,"
                           f"{_quote(last['published_at'])}::timestamptz,{_quote(last['story_id'])},100)")
    seen = [(row['language'], ' '.join(row['title'].lower().split())) for row in rows + more]
    assert len(seen) == len(set(seen)), seen
    ids = [row['story_id'] for row in rows + more]
    assert len(ids) == len(set(ids)), ids


def test_a_zero_window_restores_the_uncollapsed_projection(db):
    rows = _candidates(db, "public.m2_retained_candidates(null,null,null,null,100,0)")
    titles = [row['title'] for row in rows]
    assert len(titles) != len(set(titles))


def test_the_window_argument_is_validated(db):
    for bad in ('-1', '721'):
        denied = _sql(db, "set role service_role;"
                          f"select public.m2_retained_candidates(null,null,null,null,100,{bad});", check=False)
        assert denied.returncode != 0 and 'invalid dedupe window' in denied.stderr


def test_the_dedupe_key_helper_is_service_role_only(db):
    for role in ('anon', 'authenticated'):
        denied = _sql(db, f"set role {role}; select public.m2_story_dedupe_key('x');", check=False)
        assert denied.returncode != 0 and 'permission denied' in denied.stderr.lower()


def test_the_real_capture_ingests_with_no_uncategorised_row(db):
    # B8 bug 1, end to end: the shipped config, the real retain() floor, the
    # real ingest RPC, and the invariant asserted in SQL rather than in Python.
    capture = json.loads((ROOT / 'tests/fixtures/m2-retained-public.json').read_text())
    config = load_config(ROOT)
    allowed = {row['source_id'] for row in capture['rows']}
    items = []
    for raw in capture['rows'][:120]:
        items.append(Item(title=raw['title'], description=raw['summary'], url=raw['canonical_url'],
                          canonical_url=raw['canonical_url'], source_id=raw['source_id'],
                          source_name=raw['source_name'], language=raw['language'],
                          published_at=datetime.fromisoformat(raw['published_at'].replace('Z', '+00:00'))))
    retained = retain(items, categories=config.categories, observed_at=NOW,
                      fallback_category_for=config.fallback_category_for)
    payload = public_ingest_rows(retained, allowed_source_ids=allowed)
    assert payload
    assert _ingest(db, payload).returncode == 0
    ingested = ','.join(_quote(row['story_id']) for row in payload)
    orphans = _sql(db, "select count(*) from public.retained_corpus_observations o "
                       f"where o.story_id in ({ingested}) "
                       "and not exists (select 1 from public.retained_corpus_categories c "
                       "where c.story_id = o.story_id);")
    assert orphans.stdout.strip() == '0', orphans.stdout
    # The measurement this fixes: the SAME rows without the floor leave orphans.
    unfloored = public_ingest_rows(
        retain(items, categories=config.categories, observed_at=NOW), allowed_source_ids=allowed)
    assert [row for row in unfloored if not row['category_ids']]
