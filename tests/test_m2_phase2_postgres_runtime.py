"""The M2.1 Phase 2 migrations APPLIED on PostgreSQL 17.11, not string-matched.

A string-matched migration test proves the file contains some text. It does not
prove the table installs, the partial unique index holds under concurrency, or
that a lane filter returns what it claims. Same container shape as
tests/test_m2_translation_postgres_runtime.py.
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
    'supabase/migrations/202609140001_m2_behavior_history.sql',
    'supabase/migrations/202609140002_m2_retained_corpus.sql',
    'supabase/migrations/202609140003_m2_ranker_budget.sql',
    'supabase/migrations/202609160001_m2_translation_columns.sql',
    'supabase/migrations/202609170001_m2_retained_overlay.sql',
    'supabase/migrations/202609180001_m2_retained_coverage_and_lanes.sql',
    'supabase/migrations/202609180002_m2_reading_runs.sql',
)
OWNER = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'


def _run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=120, check=check)


def _sql(container, sql, check=True):
    return _run('docker', 'exec', '-i', container, 'psql', '-X', '-At', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', input_text=sql, check=check)


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _service(container, statement, check=True):
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                + statement, check=check)


def _last(result):
    """psql -At still prints a status tag for each SET, so read the final line."""
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ''


def _as_owner(container, user_id, statement, check=True):
    return _sql(container, "set role authenticated;"
                f"set request.jwt.claim.sub = {_quote(user_id)};"
                f"set request.jwt.claims = '{{\"role\":\"authenticated\",\"sub\":\"{user_id}\"}}';"
                + statement, check=check)


@pytest.fixture(scope='module')
def db():
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable; PostgreSQL 17.11 runtime not verified')
    if _run('docker', 'image', 'inspect', IMAGE, check=False).returncode:
        pytest.skip('Installed PostgreSQL 17.11 image or Docker daemon unavailable; no pull attempted')
    container = 'news-curator-phase2-' + uuid.uuid4().hex[:10]
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
        _sql(container, f"insert into auth.users(id) values ({_quote(OWNER)}), ({_quote(OTHER)});")
        _seed_corpus(container)
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


def _story_id(url):
    return 'story:' + hashlib.sha256(url.encode('utf-8')).hexdigest()


def _row(url, **extra):
    payload = {
        'story_id': _story_id(url), 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url, 'title': f'Headline for {url}', 'summary': 'Summary.',
        'language': 'en', 'source_id': 'reuters', 'source_name': 'Reuters',
        'source_is_aggregator': False, 'published_at': '2026-09-18T10:00:00Z',
        'source_observed_at': '2026-09-18T10:00:00Z', 'category_ids': ['world'],
    }
    payload.update(extra)
    return payload


# Three named coverage fixtures. Each asserts both the row count and the verdict.
AGGREGATOR_ONLY = 'https://example.test/coverage-aggregator-only'
MIXED = 'https://example.test/coverage-mixed'
MULTI_OUTLET = 'https://example.test/coverage-true-multi-outlet'
PROFILE_MATCH = 'https://example.test/profile-match'
OFF_PROFILE = 'https://example.test/off-profile'
AGGREGATOR_STORY = 'https://example.test/aggregator-story'


def _seed_corpus(container):
    rows = [
        _row(AGGREGATOR_ONLY, source_id='buzzing', source_name='buzzing.cc', source_is_aggregator=True),
        _row(MIXED, source_id='cnbeta', source_name='cnBeta', language='zh'),
        _row(MULTI_OUTLET),
        _row(PROFILE_MATCH, source_id='quanta', source_name='Quanta', category_ids=['science']),
        _row(OFF_PROFILE, source_id='espn', source_name='ESPN', category_ids=['sport']),
        _row(AGGREGATOR_STORY, source_id='google-36kr', source_name='36Kr via Google',
             source_is_aggregator=True, category_ids=['tech']),
    ]
    _service(container, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    coverage = [
        # Aggregator only: three observations, zero independent publishers.
        {'story_id': _story_id(AGGREGATOR_ONLY), 'publisher_id': 'buzzing', 'is_independent': False,
         'first_seen_at': '2026-09-18T10:00:00Z'},
        {'story_id': _story_id(AGGREGATOR_ONLY), 'publisher_id': 'google-36kr', 'is_independent': False,
         'first_seen_at': '2026-09-18T10:05:00Z'},
        {'story_id': _story_id(AGGREGATOR_ONLY), 'publisher_id': 'hnfront', 'is_independent': False,
         'first_seen_at': '2026-09-18T10:06:00Z'},
        # Mixed: one publisher plus two aggregator echoes of it.
        {'story_id': _story_id(MIXED), 'publisher_id': 'cnbeta', 'is_independent': True,
         'first_seen_at': '2026-09-18T10:00:00Z'},
        {'story_id': _story_id(MIXED), 'publisher_id': 'buzzing', 'is_independent': False,
         'first_seen_at': '2026-09-18T10:02:00Z'},
        {'story_id': _story_id(MIXED), 'publisher_id': 'google-36kr', 'is_independent': False,
         'first_seen_at': '2026-09-18T10:03:00Z'},
        # True multi-outlet: three publishers, and a duplicate Reuters sighting
        # that must COLLAPSE into the existing row rather than count twice.
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'reuters', 'is_independent': True,
         'first_seen_at': '2026-09-18T10:00:00Z'},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'cnn', 'is_independent': True,
         'first_seen_at': '2026-09-18T10:01:00Z'},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'cnbeta', 'is_independent': True,
         'first_seen_at': '2026-09-18T10:02:00Z'},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'reuters', 'is_independent': True,
         'first_seen_at': '2026-09-18T10:30:00Z'},
    ]
    _service(container, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")


def _lane(container, lane=None, *, categories=None, sources=None, limit=50, min_sources=2):
    arguments = [
        'p_lane => ' + (_quote(lane) if lane else 'null'),
        'p_profile_categories => ' + (f"array[{','.join(_quote(c) for c in categories)}]::text[]"
                                      if categories else 'null'),
        'p_profile_sources => ' + (f"array[{','.join(_quote(s) for s in sources)}]::text[]"
                                   if sources else 'null'),
        f'p_trend_min_sources => {min_sources}', f'p_limit => {limit}',
    ]
    result = _service(container, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
                      f"public.m2_retained_candidates_v2({', '.join(arguments)}) as rows(value);")
    return json.loads(result.stdout.splitlines()[-1])


def _by_story(rows):
    return {row['story_id']: row for row in rows}


# --- coverage and hot ------------------------------------------------------

def test_the_migrations_apply_and_the_new_objects_exist(db):
    result = _sql(db, "select table_name from information_schema.tables "
                      "where table_name in ('retained_corpus_coverage','m2_reading_runs') order by table_name;")
    assert result.stdout.split() == ['m2_reading_runs', 'retained_corpus_coverage']


def test_one_coverage_row_per_distinct_publisher(db):
    result = _sql(db, "select count(*) from public.retained_corpus_coverage "
                      f"where story_id = {_quote(_story_id(MULTI_OUTLET))};")
    # Four observations were sent; the duplicate Reuters sighting collapsed.
    assert result.stdout.strip() == '3'


def test_a_repeat_sighting_keeps_the_earliest_first_seen(db):
    result = _sql(db, "select first_seen_at from public.retained_corpus_coverage "
                      f"where story_id = {_quote(_story_id(MULTI_OUTLET))} and publisher_id = 'reuters';")
    assert result.stdout.strip().startswith('2026-09-18 10:00')


def test_aggregator_echoes_alone_are_not_hot(db):
    row = _by_story(_lane(db))[_story_id(AGGREGATOR_ONLY)]
    assert row['independent_source_count'] == 0
    assert _story_id(AGGREGATOR_ONLY) not in _by_story(_lane(db, 'hot'))


def test_one_publisher_plus_echoes_is_not_hot_at_threshold_two(db):
    row = _by_story(_lane(db))[_story_id(MIXED)]
    assert row['independent_source_count'] == 1
    assert _story_id(MIXED) not in _by_story(_lane(db, 'hot'))


def test_three_real_outlets_are_hot(db):
    row = _by_story(_lane(db))[_story_id(MULTI_OUTLET)]
    assert row['independent_source_count'] == 3
    assert _story_id(MULTI_OUTLET) in _by_story(_lane(db, 'hot'))


def test_a_story_nobody_else_carried_still_counts_its_own_publisher(db):
    assert _by_story(_lane(db))[_story_id(PROFILE_MATCH)]['independent_source_count'] == 1


def test_coverage_for_an_unknown_story_is_skipped_not_inserted(db):
    payload = [{'story_id': _story_id('https://example.test/never-ingested'), 'publisher_id': 'ghost',
                'is_independent': True, 'first_seen_at': '2026-09-18T10:00:00Z'}]
    result = _service(db, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(payload))}::jsonb);")
    assert result.stdout.strip().splitlines()[-1] == '0'


def test_the_writer_path_produces_coverage_that_lands_in_the_hot_pool(db):
    """End to end from the deduper to the hot lane, through the REAL writer.

    Three routes carry one link. `retain` merges them into one canonical story
    with three coverage mentions, `coverage_ingest_rows` turns those into the
    exact payload the RPC accepts, and the lane RPC then counts three
    independent publishers and returns the story in the hot pool. Nothing in
    this test hand-writes a coverage row.
    """
    from datetime import datetime, timezone

    from curator.config import Category
    from curator.models import Item
    from curator.retained_corpus import coverage_ingest_rows, retain

    url = 'https://example.test/writer-path'
    observed = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)

    def route(source_id, *, aggregator=False, echo_eligible=True):
        return Item(title='Three outlets carried this', url=url, canonical_url=url,
                    source_id=source_id, source_name=source_id.title(), published_at=observed,
                    language='en', description='Body.', is_aggregator=aggregator,
                    echo_eligible=echo_eligible)

    retained = retain([route('reuters'), route('cnn'), route('cnbeta'),
                       route('buzzing', aggregator=True)],
                      categories=(Category(name='World', id='world', keywords=['outlets']),),
                      observed_at=observed)
    assert len(retained) == 1
    rows = [{'story_id': retained[0].story_id, 'origin_class': 'public_outlet',
             'source_kind': 'outlet', 'canonical_url': url, 'title': retained[0].item.title,
             'summary': 'Body.', 'language': 'en', 'source_id': retained[0].item.source_id,
             'source_name': retained[0].item.source_name, 'source_is_aggregator': False,
             'published_at': observed.isoformat(), 'source_observed_at': observed.isoformat(),
             'category_ids': ['world']}]
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")

    coverage = coverage_ingest_rows(retained, independent_source_ids={'reuters', 'cnn', 'cnbeta'})
    assert len(coverage) == 4, 'every route is recorded, including the aggregator'
    written = _service(db, "select public.m2_ingest_retained_coverage("
                       f"{_quote(json.dumps(coverage))}::jsonb);")
    assert _last(written) == '4'

    row = _by_story(_lane(db))[retained[0].story_id]
    assert row['independent_source_count'] == 3, 'the aggregator must not be counted'
    assert retained[0].story_id in _by_story(_lane(db, 'hot')), 'three outlets must be hot'


def test_the_writer_is_idempotent_across_runs(db):
    """The hourly ingest re-sends the same rows every run. A second write must
    not double a count, or every story would be hot by the end of the day."""
    from datetime import datetime, timezone

    from curator.config import Category
    from curator.models import Item
    from curator.retained_corpus import coverage_ingest_rows, retain

    url = 'https://example.test/writer-path'
    observed = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    retained = retain([Item(title='Three outlets carried this', url=url, canonical_url=url,
                            source_id=source, source_name=source, published_at=observed,
                            language='en', description='Body.') for source in ('reuters', 'cnn')],
                      categories=(Category(name='World', id='world', keywords=['outlets']),),
                      observed_at=observed)
    coverage = coverage_ingest_rows(retained, independent_source_ids={'reuters', 'cnn'})
    _service(db, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")
    again = _service(db, "select public.m2_ingest_retained_coverage("
                     f"{_quote(json.dumps(coverage))}::jsonb);")
    assert _last(again) == '0', 'an unchanged re-send writes nothing'
    total = _sql(db, "select count(*) from public.retained_corpus_coverage "
                     f"where story_id = {_quote(retained[0].story_id)};")
    assert int(_last(total)) == 4, 'the earlier four rows, not eight'


# --- the lane RPC ----------------------------------------------------------

def test_the_lane_rpc_returns_the_phase_one_overlay_fields(db):
    row = _by_story(_lane(db))[_story_id(MIXED)]
    assert set(row) >= {'event_group_id', 'title_translations', 'summary_translations',
                        'source_is_aggregator', 'independent_source_count'}


def test_the_interested_lane_matches_the_profile_by_category_or_source(db):
    rows = _by_story(_lane(db, 'interested', categories=['science'], sources=['espn']))
    assert _story_id(PROFILE_MATCH) in rows and _story_id(OFF_PROFILE) in rows
    assert _story_id(MULTI_OUTLET) not in rows


def test_the_surprise_lane_is_off_profile_and_never_an_aggregator(db):
    rows = _by_story(_lane(db, 'surprise', categories=['science'], sources=['espn']))
    assert _story_id(PROFILE_MATCH) not in rows and _story_id(OFF_PROFILE) not in rows
    assert _story_id(AGGREGATOR_STORY) not in rows, 'an aggregator must never hold an exploration slot'
    assert _story_id(MULTI_OUTLET) in rows


def test_an_unknown_lane_is_refused(db):
    assert _service(db, "select public.m2_retained_candidates_v2(p_lane => 'trending');",
                    check=False).returncode != 0


def test_the_original_candidate_rpc_is_still_present_for_rollback(db):
    result = _service(db, "select count(*) from public.m2_retained_candidates(p_limit => 5) as rows(value);")
    assert int(result.stdout.strip().splitlines()[-1]) > 0


# --- reading runs ----------------------------------------------------------

def _open_run(container, user_id=OWNER, idle=60, profile='{}'):
    result = _service(container, "select public.m2_open_or_join_reading_run("
                      f"{_quote(user_id)}::uuid, {idle}, {_quote(profile)}::jsonb);")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_concurrent_first_ranks_join_one_run_with_one_profile(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    statement = ("set role service_role;"
                 "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                 "select public.m2_open_or_join_reading_run("
                 f"{_quote(OWNER)}::uuid, 60, '{{\"schema_version\":1,\"event_count\":7}}'::jsonb);")
    processes = [subprocess.Popen(
        ['docker', 'exec', '-i', db, 'psql', '-X', '-At', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
    outputs = [process.communicate(statement)[0] for process in processes]
    assert all(process.returncode == 0 for process in processes)
    run_ids = {json.loads(output.strip().splitlines()[-1])['run_id'] for output in outputs}
    assert len(run_ids) == 1, 'eight concurrent first ranks must join one run'
    open_rows = _sql(db, "select count(*) from public.m2_reading_runs "
                         f"where user_id = {_quote(OWNER)}::uuid and closed_at is null;")
    assert open_rows.stdout.strip() == '1'


def test_a_request_inside_the_idle_window_joins_and_one_after_it_opens_a_new_run(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OTHER)}::uuid;")
    first = _open_run(db, OTHER)
    assert first['created'] is True
    assert _open_run(db, OTHER)['run_id'] == first['run_id']
    _service(db, "update public.m2_reading_runs set last_activity_at = now() - interval '2 hours' "
                 f"where run_id = {_quote(first['run_id'])}::uuid;")
    second = _open_run(db, OTHER)
    assert second['created'] is True and second['run_id'] != first['run_id']
    closed = _sql(db, "select closed_at is not null from public.m2_reading_runs "
                      f"where run_id = {_quote(first['run_id'])}::uuid;")
    assert closed.stdout.strip() == 't'


def test_the_frozen_profile_is_returned_to_every_page_in_the_run(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    opened = _open_run(db, OWNER, profile='{"schema_version":1,"event_count":7}')
    joined = _open_run(db, OWNER, profile='{"schema_version":1,"event_count":999}')
    assert joined['run_id'] == opened['run_id']
    assert joined['profile_snapshot']['event_count'] == 7, 'a joined run must not re-freeze the profile'


def test_an_out_of_range_idle_window_is_refused(db):
    assert _service(db, f"select public.m2_open_or_join_reading_run({_quote(OWNER)}::uuid, 4000, '{{}}'::jsonb);",
                    check=False).returncode != 0


def test_recorded_negative_filters_accumulate_without_duplicates(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    story = _story_id(MIXED)
    _service(db, "select public.m2_record_reading_run_filter("
                 f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, array[{_quote(story)}]::text[]);")
    result = _service(db, "select public.m2_record_reading_run_filter("
                          f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, array[{_quote(story)}]::text[]);")
    assert result.stdout.strip().splitlines()[-1] == '1'


def test_an_owner_reads_only_her_own_runs(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _open_run(db, OWNER)
    mine = _as_owner(db, OWNER, "select count(*) from public.m2_reading_runs;")
    theirs = _as_owner(db, OTHER, "select count(*) from public.m2_reading_runs "
                                  f"where user_id = {_quote(OWNER)}::uuid;")
    assert int(_last(mine)) >= 1
    assert _last(theirs) == '0'


def test_an_owner_cannot_mint_her_own_run(db):
    denied = _as_owner(db, OWNER, "insert into public.m2_reading_runs(user_id) "
                                  f"values ({_quote(OWNER)}::uuid);", check=False)
    assert denied.returncode != 0


def test_anonymous_callers_reach_neither_new_table(db):
    for table in ('public.m2_reading_runs', 'public.retained_corpus_coverage'):
        denied = _sql(db, f"set role anon; select count(*) from {table};", check=False)
        assert denied.returncode != 0, f'anon reached {table}'


# --- reviewable pages ------------------------------------------------------

def _freeze_page(container, user_id, created_at, cards):
    request_id = str(uuid.uuid4())
    bindings = {"history_generation": 1, "server_commit_revision": 0, "consent_revision": 0,
                "result_mode": "heuristic", "fallback_reason": "test",
                "eligibility": {"category": None, "query": None}, "short_lane_reasons": []}
    _service(container, "insert into public.m2_frozen_rankings"
             "(request_id,user_id,bindings,cards,page_size,expires_at,created_at) values ("
             f"{_quote(request_id)}::uuid, {_quote(user_id)}::uuid, {_quote(json.dumps(bindings))}::jsonb, "
             f"{_quote(json.dumps(cards))}::jsonb, 25, now() + interval '15 minutes', "
             f"{_quote(created_at)}::timestamptz);")
    return request_id


def test_an_owner_can_review_the_labels_of_a_past_hour(db):
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "Headline", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    _freeze_page(db, OWNER, '2026-09-18T08:30:00Z', cards)
    _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards)
    result = _as_owner(db, OWNER, "select coalesce(jsonb_agg(value),'[]'::jsonb) from "
                       "public.m2_owner_reading_pages('2026-09-18T08:00:00Z'::timestamptz) as rows(value);")
    pages = json.loads(_last(result))
    assert len(pages) == 1, 'the hour is a bound, not a suggestion'
    assert pages[0]['cards'][0]['lane'] == 'hot'
    assert pages[0]['cards'][0]['lane_label'] == 'hot'


def test_one_owner_cannot_review_another_owners_pages(db):
    result = _as_owner(db, OTHER, "select count(*) from "
                       "public.m2_owner_reading_pages('2026-09-18T08:00:00Z'::timestamptz) as rows(value);")
    assert _last(result) == '0'
