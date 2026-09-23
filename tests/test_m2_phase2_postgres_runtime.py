"""The M2.1 Phase 2 migrations APPLIED on PostgreSQL 17.11, not string-matched.

A string-matched migration test proves the file contains some text. It does not
prove the table installs, the partial unique index holds under concurrency, or
that a lane filter returns what it claims. Same container shape as
tests/test_m2_translation_postgres_runtime.py.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'postgres:17.11'
# Every fixture timestamp in this file is anchored to the moment the module
# loads, not to a calendar date. The hot lane and the corpus prune both filter
# against PostgreSQL's own now() (trend.window_hours in
# config/ranking-policy-r2.yaml, and the retention floor in
# m2_prune_retained_corpus), so a fixture seeded at a fixed 2026-09-18 date
# ages out of that window a couple of days later and the hot-lane assertions
# start failing (seen on CI run 35543559627). BASE sits comfortably inside
# every window this file exercises (24h trend, 36h dedupe, 14d retention).
NOW = datetime.now(timezone.utc)
BASE = NOW - timedelta(hours=2)


def _iso(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


MIGRATIONS = (
    'supabase/migrations/202609070001_reading_history.sql',
    'supabase/migrations/202609140001_m2_behavior_history.sql',
    'supabase/migrations/202609140002_m2_retained_corpus.sql',
    'supabase/migrations/202609140003_m2_ranker_budget.sql',
    'supabase/migrations/202609160001_m2_translation_columns.sql',
    'supabase/migrations/202609170001_m2_retained_overlay.sql',
    'supabase/migrations/202609180001_m2_retained_coverage_and_lanes.sql',
    'supabase/migrations/202609180002_m2_reading_runs.sql',
    'supabase/migrations/202609180003_m2_frozen_ranking_run_scope.sql',
    'supabase/migrations/202609180004_m2_retained_corpus_prune.sql',
    'supabase/migrations/202609180005_m2_reading_run_page_budget.sql',
    'supabase/migrations/202609180006_m2_reading_run_ranking_claim.sql',
    'supabase/migrations/202609180007_m2_claimed_ranker_reservation.sql',
    # PR #47's dedupe lands after the Phase 2 files by filename, and the lane
    # RPC inherits its rule in 0102. Applied in the same order CI applies them.
    'supabase/migrations/202609180101_m2_retained_candidates_dedupe.sql',
    'supabase/migrations/202609180102_m2_retained_candidates_v2_dedupe.sql',
    # 202609210001 replaces the quadratic dedupe with a lead() window over
    # the same set. Applied here so every assertion below is made against
    # the definition production actually runs, not the one it replaced.
    'supabase/migrations/202609210001_m2_retained_candidates_v2_dedupe_linear.sql',
    'supabase/migrations/202609220001_m2_atomic_reading_run_progress.sql',
    'supabase/migrations/202609230001_m2_retained_candidates_v2_bulk_general.sql',
    'supabase/migrations/202609230002_m2_retained_candidates_filtered.sql',
    'supabase/migrations/202609230003_m2_opened_candidate_ids.sql',
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


def test_opened_candidate_ids_are_owner_scoped_bounded_and_authenticated(db):
    first, second = _story_id(AGGREGATOR_ONLY), _story_id(MIXED)
    _sql(db, f"insert into public.user_story_state(user_id, story_id, read_at) values "
        f"('{OWNER}', '{first}', now()), ('{OTHER}', '{second}', now()) "
        "on conflict(user_id,story_id) do update set read_at=excluded.read_at;")
    query = f"select public.m2_opened_candidate_ids(array['{first}','{second}','{first}']);"
    assert _last(_as_owner(db, OWNER, query)) == first
    assert _last(_as_owner(db, OTHER, query)) == second
    assert _service(db, query, check=False).returncode != 0
    assert _sql(db, "set role anon;" + query, check=False).returncode != 0
    assert _as_owner(db, '', query, check=False).returncode != 0
    for expression in ("null", "array[null]::text[]", "array['bad']", "array_fill('" + first + "'::text,array[10001])"):
        assert _as_owner(db, OWNER, f"select public.m2_opened_candidate_ids({expression});",
                         check=False).returncode != 0
    assert _last(_as_owner(db, OWNER,
        f"select count(*) from public.m2_opened_candidate_ids(array_fill('{first}'::text,array[10000]));")) == '1'
    # The old rich-state endpoint cannot safely carry the pooled input.
    assert _as_owner(db, OWNER,
        f"select public.m2_owner_story_states(array_fill('{first}'::text,array[233]));",
        check=False).returncode != 0


def benchmark_distinct_opened_candidate_ids(db):
    """Controlled load fixture, not a claim about the live owner's history."""
    count = 10000
    ids = [_story_id(f'https://example.test/opened-benchmark/{50000 + index}')
           for index in range(count)]
    assert len(set(ids)) == count
    #20,000 rows per owner: half requested, half outside the request. A second
    # owner's equally sized history exercises the owner-keyed access boundary.
    _sql(db, f"""
      insert into public.canonical_stories(story_id, canonical_url, title, summary,
          language, source_kind, source_name, published_at)
      select 'story:' || encode(extensions.digest('https://example.test/opened-benchmark/' || n,'sha256'),'hex'),
          'https://example.test/opened-benchmark/' || n, 'Benchmark fixture', '',
          'en', 'outlet', 'Fixture source', now()
      from generate_series(50000,69999) n on conflict(story_id) do nothing;
      insert into public.user_story_state(user_id,story_id,read_at)
      select owner_id::uuid, 'story:' || encode(extensions.digest(
          'https://example.test/opened-benchmark/' || n,'sha256'),'hex'), now()
      from generate_series(50000,69999) n
      cross join (values ('{OWNER}'), ('{OTHER}')) owners(owner_id)
      on conflict(user_id,story_id) do update set read_at=excluded.read_at;
      analyze public.user_story_state;
    """)
    request = json.dumps({"p_story_ids": ids}, separators=(',', ':'))
    statement = ("select coalesce(json_agg(id),'[]'::json) from "
        "public.m2_opened_candidate_ids(array(select jsonb_array_elements_text("
        + _quote(request) + "::jsonb->'p_story_ids'))) as id;")
    elapsed, result_size = [], None
    for _ in range(5):
        started = time.perf_counter()
        result = _last(_as_owner(db, OWNER, statement))
        elapsed.append(round((time.perf_counter() - started) * 1000, 3))
        returned = json.loads(result)
        assert len(returned) == count, 'maximum-size RPC returned the wrong count'
        assert set(returned) == set(ids), 'maximum-size RPC returned the wrong intersection'
        result_size = len(result.encode())
    return {"fixture": "controlled, not live owner history", "distinct_request_ids": count,
        "owner_opened_rows": 20000, "other_owner_opened_rows": 20000,
        "returned_ids": count, "request_json_bytes": len(request.encode()),
        "response_json_bytes": len(json.dumps(returned, separators=(',', ':')).encode()),
        "postgres_json_bytes": result_size, "elapsed_ms": elapsed,
        "timing_scope": "local psql process, Unix socket, SQL JSON parse, RPC, result transfer; not HTTPS/PostgREST"}


def test_opened_candidate_ids_accept_ten_thousand_distinct_ids(db):
    receipt = benchmark_distinct_opened_candidate_ids(db)
    assert receipt['distinct_request_ids'] == receipt['returned_ids'] == 10000


def _row(url, **extra):
    payload = {
        'story_id': _story_id(url), 'origin_class': 'public_outlet', 'source_kind': 'outlet',
        'canonical_url': url, 'title': f'Headline for {url}', 'summary': 'Summary.',
        'language': 'en', 'source_id': 'reuters', 'source_name': 'Reuters',
        'source_is_aggregator': False, 'published_at': _iso(BASE),
        'source_observed_at': _iso(BASE), 'category_ids': ['world'],
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
         'first_seen_at': _iso(BASE)},
        {'story_id': _story_id(AGGREGATOR_ONLY), 'publisher_id': 'google-36kr', 'is_independent': False,
         'first_seen_at': _iso(BASE + timedelta(minutes=5))},
        {'story_id': _story_id(AGGREGATOR_ONLY), 'publisher_id': 'hnfront', 'is_independent': False,
         'first_seen_at': _iso(BASE + timedelta(minutes=6))},
        # Mixed: one publisher plus two aggregator echoes of it.
        {'story_id': _story_id(MIXED), 'publisher_id': 'cnbeta', 'is_independent': True,
         'first_seen_at': _iso(BASE)},
        {'story_id': _story_id(MIXED), 'publisher_id': 'buzzing', 'is_independent': False,
         'first_seen_at': _iso(BASE + timedelta(minutes=2))},
        {'story_id': _story_id(MIXED), 'publisher_id': 'google-36kr', 'is_independent': False,
         'first_seen_at': _iso(BASE + timedelta(minutes=3))},
        # True multi-outlet: three publishers, and a duplicate Reuters sighting
        # that must COLLAPSE into the existing row rather than count twice.
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'reuters', 'is_independent': True,
         'first_seen_at': _iso(BASE)},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'cnn', 'is_independent': True,
         'first_seen_at': _iso(BASE + timedelta(minutes=1))},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'cnbeta', 'is_independent': True,
         'first_seen_at': _iso(BASE + timedelta(minutes=2))},
        {'story_id': _story_id(MULTI_OUTLET), 'publisher_id': 'reuters', 'is_independent': True,
         'first_seen_at': _iso(BASE + timedelta(minutes=30))},
    ]
    _service(container, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")


def _lane(container, lane=None, *, categories=None, sources=None, limit=50, min_sources=2,
          before=None, check=True):
    arguments = [
        'p_lane => ' + (_quote(lane) if lane else 'null'),
        'p_profile_categories => ' + (f"array[{','.join(_quote(c) for c in categories)}]::text[]"
                                      if categories else 'null'),
        'p_profile_sources => ' + (f"array[{','.join(_quote(s) for s in sources)}]::text[]"
                                   if sources else 'null'),
        f'p_trend_min_sources => {min_sources}', f'p_limit => {limit}',
    ]
    if before is not None:
        count, published, story = before
        if count is not None:
            arguments.append(f'p_before_source_count => {count}')
        arguments += [f'p_before_published_at => {_quote(published)}::timestamptz',
                      f'p_before_story_id => {_quote(story)}']
    result = _service(container, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
                      f"public.m2_retained_candidates_v2({', '.join(arguments)}) as rows(value);",
                      check=check)
    if not check and result.returncode != 0:
        return None
    return json.loads(_last(result))


def _by_story(rows):
    return {row['story_id']: row for row in rows}


def _filtered(container, *, category_id=None, lane=None, categories=(), profile_sources=(), limit=50,
              before=None, excluded=(), sources=(), topics=()):
    def array(values):
        return 'array[' + ','.join(_quote(value) for value in values) + ']::text[]'
    arguments = [f'p_category_id => {_quote(category_id) if category_id else "null"}',
                 f'p_lane => {_quote(lane) if lane else "null"}',
                 f'p_profile_categories => {array(categories)}',
                 f'p_profile_sources => {array(profile_sources)}',
                 f'p_limit => {limit}',
                 f'p_excluded_story_ids => {array(excluded)}',
                 f'p_suppressed_sources => {array(sources)}',
                 f'p_suppressed_topics => {array(topics)}']
    if before is not None:
        count, published, story = before
        if count is not None:
            arguments.append(f'p_before_source_count => {count}')
        arguments += [f'p_before_published_at => {_quote(published)}::timestamptz',
                      f'p_before_story_id => {_quote(story)}']
    result = _service(container, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
        f'public.m2_retained_candidates_filtered({", ".join(arguments)}) as rows(value);')
    return json.loads(_last(result))


# --- coverage and hot ------------------------------------------------------

def test_ordered_phase_two_migration_replay_restores_the_current_rpc(db):
    """Replaying old definitions must end at the current non-ambiguous RPC."""
    for migration in ('supabase/migrations/202609180001_m2_retained_coverage_and_lanes.sql',
                      'supabase/migrations/202609180002_m2_reading_runs.sql',
                      'supabase/migrations/202609180003_m2_frozen_ranking_run_scope.sql',
                      'supabase/migrations/202609180004_m2_retained_corpus_prune.sql',
                      'supabase/migrations/202609180005_m2_reading_run_page_budget.sql',
                      'supabase/migrations/202609180006_m2_reading_run_ranking_claim.sql',
                      'supabase/migrations/202609180007_m2_claimed_ranker_reservation.sql',
                      'supabase/migrations/202609180102_m2_retained_candidates_v2_dedupe.sql',
                      'supabase/migrations/202609210001_m2_retained_candidates_v2_dedupe_linear.sql',
                      'supabase/migrations/202609220001_m2_atomic_reading_run_progress.sql',
                      'supabase/migrations/202609230001_m2_retained_candidates_v2_bulk_general.sql',
                      'supabase/migrations/202609230002_m2_retained_candidates_filtered.sql'):
        again = _sql(db, (ROOT / migration).read_text(), check=False)
        assert again.returncode == 0, f'{migration} failed ordered replay: {again.stderr[:400]}'
    functions = _sql(db, "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                     "where n.nspname = 'public' and p.proname = 'm2_retained_candidates_v2';")
    assert functions.stdout.strip() == '1'
    assert len(_lane(db, limit=200)) >= 1


def test_the_hot_lane_continuation_cursor_is_accepted_by_the_real_rpc(db):
    """The service's continuation sends the hot lane its own keyset. This is the
    same call shape, against the real function, across a page boundary."""
    first = _lane(db, 'hot', limit=1)
    if not first:
        pytest.skip('no hot rows in the fixture corpus')
    head = first[0]
    second = _lane(db, 'hot', limit=5,
                   before=(head['independent_source_count'], head['published_at'], head['story_id']))
    assert head['story_id'] not in {row['story_id'] for row in second}
    # And the general cursor shape the other lanes use is still refused for hot.
    assert _service(db, "select public.m2_retained_candidates_v2(p_lane => 'hot', "
                    f"p_before_published_at => {_quote(head['published_at'])}::timestamptz, "
                    f"p_before_story_id => {_quote(head['story_id'])});", check=False).returncode != 0


def test_the_migrations_apply_and_the_new_objects_exist(db):
    result = _sql(db, "select table_name from information_schema.tables "
                      "where table_name in ('retained_corpus_coverage','m2_reading_runs') order by table_name;")
    assert result.stdout.split() == ['m2_reading_runs', 'retained_corpus_coverage']


def test_atomic_run_rpc_is_versioned_for_a_safe_two_phase_rollout(db):
    """The migration lands before the new Modal revision.  The old function
    must therefore keep accepting the old profile shape until traffic switches,
    while v2 refuses that shape and enforces explicit privacy epochs."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OTHER)}::uuid;")
    legacy = _service(db, "select public.m2_open_or_join_reading_run("
        f"{_quote(OTHER)}::uuid,60,'{{}}'::jsonb,60);", check=False)
    assert legacy.returncode == 0, legacy.stderr
    strict = _service(db, "select public.m2_open_or_join_reading_run_v2("
        f"{_quote(OTHER)}::uuid,60,'{{}}'::jsonb,60);", check=False)
    assert strict.returncode != 0 and 'stale reading run epoch' in strict.stderr


def test_one_coverage_row_per_distinct_publisher(db):
    result = _sql(db, "select count(*) from public.retained_corpus_coverage "
                      f"where story_id = {_quote(_story_id(MULTI_OUTLET))};")
    # Four observations were sent; the duplicate Reuters sighting collapsed.
    assert result.stdout.strip() == '3'


def test_a_repeat_sighting_keeps_the_earliest_first_seen(db):
    result = _sql(db, "select first_seen_at from public.retained_corpus_coverage "
                      f"where story_id = {_quote(_story_id(MULTI_OUTLET))} and publisher_id = 'reuters';")
    # The earliest of the two reuters sightings (BASE), not the later duplicate
    # (BASE + 30 minutes).
    assert result.stdout.strip().startswith(BASE.strftime('%Y-%m-%d %H:%M:%S')), result.stdout


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
                'is_independent': True, 'first_seen_at': _iso(NOW)}]
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
    observed = datetime.now(timezone.utc)

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
    observed = datetime.now(timezone.utc)
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

def test_exactly_one_lane_rpc_overload_exists(db):
    """`create or replace` with a different parameter list creates a SECOND
    overload, and PostgreSQL then refuses every named-argument call as
    ambiguous. Caught in CI, not by reading the file."""
    result = _sql(db, "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                      "where n.nspname = 'public' and p.proname = 'm2_retained_candidates_v2';")
    assert _last(result) == '1', 'more than one v2 overload exists; named calls will be ambiguous'


def test_the_lane_rpc_inherits_the_dedupe_rule(db):
    """The Phase 2 feed reads exclusively through v2, so a dedupe rule that lives
    only in m2_retained_candidates would be a fix on a path nothing calls."""
    first = 'https://example.test/lane-dedupe-older'
    second = 'https://example.test/lane-dedupe-newer'
    title = 'One headline, two addresses'
    rows = [_row(first, title=title, source_id='wire-a', source_name='Wire A',
                 published_at=_iso(BASE), source_observed_at=_iso(BASE)),
            _row(second, title=title, source_id='wire-b', source_name='Wire B',
                 published_at=_iso(BASE + timedelta(minutes=30)),
                 source_observed_at=_iso(BASE + timedelta(minutes=30)))]
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    served = [row for row in _lane(db) if row['title'] == title]
    assert len(served) == 1, f'the lane RPC served {len(served)} copies of one story'
    # The newest wins, the same representative the other projections choose.
    assert served[0]['story_id'] == _story_id(second)
    # And the rule is operable: a window of zero restores one row per observation.
    result = _service(db, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
                      "public.m2_retained_candidates_v2(p_limit => 100, p_dedupe_window_hours => 0) "
                      "as rows(value);")
    both = [row for row in json.loads(_last(result)) if row['title'] == title]
    assert len(both) == 2, 'the dedupe window is not operable'


def _twins(container, slug, title, *, older_source, newer_source,
           older_published=_iso(BASE), newer_published=_iso(BASE + timedelta(minutes=30)),
           categories=None):
    """Two rows, one headline, two addresses. The older one is the interesting
    one: the dedupe rule keeps the NEWER, so a lane that only the older
    qualifies for is where a pre-dedupe lane filter earns its place."""
    older = f'https://example.test/{slug}-older'
    newer = f'https://example.test/{slug}-newer'
    rows = [_row(older, title=title, source_id=older_source, source_name=older_source,
                 published_at=older_published, source_observed_at=older_published,
                 category_ids=categories or ['world']),
            _row(newer, title=title, source_id=newer_source, source_name=newer_source,
                 published_at=newer_published, source_observed_at=newer_published,
                 category_ids=categories or ['world'])]
    _service(container, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    return _story_id(older), _story_id(newer)


def test_a_lane_keeps_the_twin_that_qualifies_for_it_hot(db):
    """Only the OLDER twin is hot. With the lane applied after the dedupe, the
    newer one suppressed it and was then filtered out itself, and the hot lane
    returned zero copies of a story that had a perfectly good one."""
    title = 'Twins where only the older is hot'
    older, newer = _twins(db, 'lane-twin-hot', title,
                          older_source='wire-hot', newer_source='wire-quiet')
    coverage = [{'story_id': older, 'publisher_id': publisher, 'is_independent': True,
                 'first_seen_at': _iso(BASE)} for publisher in ('a', 'b', 'c')]
    _service(db, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")
    served = [row for row in _lane(db, 'hot') if row['title'] == title]
    assert [row['story_id'] for row in served] == [older], served
    # The unfiltered projection still prefers the newer one: the lane changed
    # which rows were VISIBLE, not the dedupe rule itself.
    everything = [row for row in _lane(db) if row['title'] == title]
    assert [row['story_id'] for row in everything] == [newer], everything


def test_a_lane_keeps_the_twin_that_qualifies_for_it_interested(db):
    title = 'Twins where only the older is for you'
    older, newer = _twins(db, 'lane-twin-interested', title,
                          older_source='liked-wire', newer_source='unknown-wire')
    served = [row for row in _lane(db, 'interested', sources=['liked-wire'])
              if row['title'] == title]
    assert [row['story_id'] for row in served] == [older], served


def test_a_lane_keeps_the_twin_that_qualifies_for_it_surprise(db):
    """Here the NEWER twin is the one on profile, so it is excluded from
    surprise and the older must survive the collapse."""
    title = 'Twins where only the older is a surprise'
    older, newer = _twins(db, 'lane-twin-surprise', title,
                          older_source='odd-wire', newer_source='liked-wire')
    served = [row for row in _lane(db, 'surprise', sources=['liked-wire'])
              if row['title'] == title]
    assert [row['story_id'] for row in served] == [older], served


def test_each_lane_still_pages_without_skipping_or_repeating(db):
    """The page-boundary property, per lane, after moving the lane predicate into
    the pre-dedupe set."""
    for lane in ('updates', 'interested', 'surprise'):
        arguments = {'sources': ['liked-wire']} if lane in ('interested', 'surprise') else {}
        whole = _lane(db, lane, limit=50, **arguments)
        if len(whole) < 3:
            continue
        head = whole[0]
        rest = _lane(db, lane, limit=50, **arguments)
        assert [row['story_id'] for row in rest] == [row['story_id'] for row in whole], lane
        paged = _service(db, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
            f"public.m2_retained_candidates_v2(p_lane => {_quote(lane)}, "
            + ("p_profile_sources => array['liked-wire']::text[], " if arguments else "")
            + f"p_before_published_at => {_quote(head['published_at'])}::timestamptz, "
            f"p_before_story_id => {_quote(head['story_id'])}, p_limit => 50) as rows(value);")
        after = json.loads(_last(paged))
        assert head['story_id'] not in {row['story_id'] for row in after}, lane
        assert [row['story_id'] for row in after] == [row['story_id'] for row in whole[1:]], lane


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


def test_the_hot_lane_pages_on_its_own_sort_key_without_skipping_or_repeating(db):
    """The hot lane orders by independent source count first. A published_at
    cursor cannot describe that boundary, so the cursor is the whole sort key."""
    rows = []
    for index in range(6):
        url = f'https://example.test/hot-keyset-{index}'
        rows.append(_row(url, source_id=f'wire{index}', source_name=f'Wire {index}',
                         published_at=_iso(BASE), source_observed_at=_iso(BASE)))
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    coverage = []
    for index in range(6):
        story = _story_id(f'https://example.test/hot-keyset-{index}')
        # Deliberately DIFFERENT counts with the SAME published_at, which is
        # exactly the shape a published_at-only cursor gets wrong.
        for publisher in range(2 + index % 3):
            coverage.append({'story_id': story, 'publisher_id': f'pub{publisher}',
                             'is_independent': True, 'first_seen_at': _iso(BASE)})
    _service(db, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")

    first = _lane(db, 'hot', limit=3)
    assert len(first) == 3
    counts = [row['independent_source_count'] for row in first]
    assert counts == sorted(counts, reverse=True), 'hot leads with the count'
    tail = first[-1]
    second = _lane(db, 'hot', limit=10,
                   before=(tail['independent_source_count'], tail['published_at'], tail['story_id']))
    assert not ({row['story_id'] for row in first} & {row['story_id'] for row in second}), 'repeated a row'
    whole = [row['story_id'] for row in _lane(db, 'hot', limit=50)]
    paged = [row['story_id'] for row in first] + [row['story_id'] for row in second]
    assert paged == whole, 'the two pages must be the unpaged order, with nothing skipped'


def test_half_a_hot_cursor_is_refused(db):
    """Half a keyset silently drops rows at the boundary, so it is refused."""
    assert _service(db, "select public.m2_retained_candidates_v2(p_lane => 'hot', "
                    "p_before_published_at => '2026-09-18T09:00:00Z'::timestamptz, "
                    "p_before_story_id => 'story:" + 'a' * 64 + "');", check=False).returncode != 0
    assert _service(db, "select public.m2_retained_candidates_v2(p_lane => 'updates', "
                    "p_before_source_count => 2);", check=False).returncode != 0


def test_the_prune_removes_old_rows_and_leaves_recent_coverage_intact(db):
    """Both the dedupe CTE and the coverage count scan this table, so it needs a
    window. The window must not take yesterday with it: hot counts a 24-hour
    window and would read as zero if it did."""
    old_url = 'https://example.test/prune-old'
    recent_url = 'https://example.test/prune-recent'
    long_ago = NOW - timedelta(days=30)
    rows = [_row(old_url, source_id='oldwire', source_name='Old Wire',
                 published_at=_iso(long_ago), source_observed_at=_iso(long_ago)),
            _row(recent_url, source_id='newwire', source_name='New Wire',
                 published_at=_iso(NOW), source_observed_at=_iso(NOW))]
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    coverage = [{'story_id': _story_id(url), 'publisher_id': publisher, 'is_independent': True,
                 'first_seen_at': _iso(NOW)}
                for url in (old_url, recent_url) for publisher in ('a', 'b')]
    _service(db, f"select public.m2_ingest_retained_coverage({_quote(json.dumps(coverage))}::jsonb);")

    removed = _service(db, "select public.m2_prune_retained_corpus(14);")
    assert int(_last(removed)) >= 1

    survivors = _sql(db, "select count(*) from public.retained_corpus_observations "
                         f"where story_id = {_quote(_story_id(recent_url))};")
    assert _last(survivors) == '1', 'a recent story was pruned'
    gone = _sql(db, "select count(*) from public.retained_corpus_observations "
                    f"where story_id = {_quote(_story_id(old_url))};")
    assert _last(gone) == '0', 'the old story survived the prune'
    # The child rows went with it, and the survivor kept its own coverage.
    orphans = _sql(db, "select count(*) from public.retained_corpus_coverage "
                       f"where story_id = {_quote(_story_id(old_url))};")
    assert _last(orphans) == '0'
    kept = _sql(db, "select count(*) from public.retained_corpus_coverage "
                    f"where story_id = {_quote(_story_id(recent_url))};")
    assert _last(kept) == '2', 'the surviving story lost its coverage count'


def test_an_out_of_range_retention_window_is_refused(db):
    """Two days is the floor because the trend window is 24 hours."""
    for value in (1, 91):
        assert _service(db, f"select public.m2_prune_retained_corpus({value});",
                        check=False).returncode != 0


# --- reading runs ----------------------------------------------------------

def _open_run(container, user_id=OWNER, idle=60, profile='{}', max_minutes=60):
    profile_value = json.loads(profile)
    epoch = json.loads(_last(_sql(container,
        "select jsonb_build_object("
        f"'history_generation', coalesce((select history_generation from public.user_behavior_revisions where user_id={_quote(user_id)}::uuid),1), "
        f"'consent_revision', coalesce((select consent_revision from public.user_behavior_settings where user_id={_quote(user_id)}::uuid),0));")))
    profile_value.update({"_history_generation": epoch["history_generation"],
                          "_consent_revision": epoch["consent_revision"]})
    result = _service(container, "select public.m2_open_or_join_reading_run_v2("
                      f"{_quote(user_id)}::uuid, {idle}, "
                      f"{_quote(json.dumps(profile_value))}::jsonb, {max_minutes});")
    return json.loads(_last(result))


def test_concurrent_first_ranks_join_one_run_with_one_profile(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    statement = ("set role service_role;"
                 "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                 "select public.m2_open_or_join_reading_run_v2("
                 f"{_quote(OWNER)}::uuid, 60, "
                 "'{\"schema_version\":1,\"event_count\":7,"
                 "\"_history_generation\":1,\"_consent_revision\":0}'::jsonb);")
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


def test_the_idle_boundary_is_strict_at_exactly_sixty_minutes(db):
    """Decided once: the run ends when the gap is GREATER THAN idle_minutes, so
    at exactly 60 minutes a new run opens. Asserted at the exact instant."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OTHER)}::uuid;")
    first = _open_run(db, OTHER)
    _service(db, "update public.m2_reading_runs set last_activity_at = now() - interval '60 minutes' "
                 f"where run_id = {_quote(first['run_id'])}::uuid;")
    at_the_boundary = _open_run(db, OTHER)
    assert at_the_boundary['created'] is True, 'exactly 60 idle minutes must end the run'
    assert at_the_boundary['run_id'] != first['run_id']
    _service(db, "update public.m2_reading_runs set last_activity_at = now() - interval '59 minutes' "
                 f"where run_id = {_quote(at_the_boundary['run_id'])}::uuid;")
    inside = _open_run(db, OTHER)
    assert inside['created'] is False and inside['run_id'] == at_the_boundary['run_id']


def test_a_run_also_ends_on_age_so_an_hourly_reader_is_ever_learned_from(db):
    """last_activity_at slides forward on every page, so idle alone never fires
    for someone who keeps reading. Without the age cap one run would last all
    day and the profile would never be recomputed."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OTHER)}::uuid;")
    first = _open_run(db, OTHER)
    # Busy the whole time: activity is current, but the run itself is an hour old.
    _service(db, "update public.m2_reading_runs set opened_at = now() - interval '60 minutes', "
                 f"last_activity_at = now() where run_id = {_quote(first['run_id'])}::uuid;")
    rolled = _open_run(db, OTHER)
    assert rolled['created'] is True, 'exactly 60 minutes of age must end the run'
    _service(db, "update public.m2_reading_runs set opened_at = now() - interval '59 minutes', "
                 f"last_activity_at = now() where run_id = {_quote(rolled['run_id'])}::uuid;")
    assert _open_run(db, OTHER)['run_id'] == rolled['run_id'], 'one minute short must not roll'


def test_an_out_of_range_run_age_cap_is_refused(db):
    assert _service(db, f"select public.m2_open_or_join_reading_run_v2({_quote(OWNER)}::uuid, 60, '{{}}'::jsonb, 5);",
                    check=False).returncode != 0
    assert _service(db, f"select public.m2_open_or_join_reading_run_v2({_quote(OWNER)}::uuid, 60, '{{}}'::jsonb, 999);",
                    check=False).returncode != 0


ALL_VIEW = 'a' * 64
TECH_VIEW = 'b' * 64


def _open_view(container, run_id, key, user_id=OWNER):
    result = _service(container, "select public.m2_open_run_view("
                      f"{_quote(user_id)}::uuid, {_quote(run_id)}::uuid, {_quote(key)});")
    return json.loads(_last(result))


def _record(container, run_id, key, pages, user_id=OWNER):
    return int(_last(_service(container, "select public.m2_record_run_page("
                              f"{_quote(user_id)}::uuid, {_quote(run_id)}::uuid, "
                              f"{_quote(key)}, {pages});")))


def _reserve_response(container, run_id, key, frozen_order_id, response, offset, next_offset,
                      user_id=OWNER):
    result = _service(container, "select public.m2_reserve_run_response("
        f"{_quote(user_id)}::uuid, {_quote(run_id)}::uuid, {_quote(key)}, "
        f"{_quote(frozen_order_id)}::uuid, {response}, {offset}, {next_offset});")
    return json.loads(_last(result))


def _claim(container, run_id, key, ttl=60, user_id=OWNER):
    return json.loads(_last(_service(container, "select public.m2_claim_run_ranking("
        f"{_quote(user_id)}::uuid, {_quote(run_id)}::uuid, {_quote(key)}, "
        f"gen_random_uuid(), {ttl});")))


def test_the_page_budget_is_a_high_water_mark_per_view(db):
    """Counted per VIEW, and a high-water mark rather than a counter: re-reading
    page one must not spend the budget, the count returned is the one BEFORE this
    page so the request that trips the cap cannot also inflate it, and the pages
    she has read of All say nothing about a category."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    everything = _open_view(db, run['run_id'], ALL_VIEW)
    assert everything['pages_served'] == 0 and everything['frozen_order_id'] is None
    assert _record(db, run['run_id'], ALL_VIEW, 1) == 0, 'the count before the first page is zero'
    assert _record(db, run['run_id'], ALL_VIEW, 2) == 1
    assert _record(db, run['run_id'], ALL_VIEW, 1) == 2, 're-reading page one must not lower the mark'
    _open_view(db, run['run_id'], TECH_VIEW)
    assert _record(db, run['run_id'], TECH_VIEW, 1) == 0, "one view's budget leaked into another"
    assert _open_view(db, run['run_id'], ALL_VIEW)['pages_served'] == 2


def test_an_unknown_view_spends_nothing(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    assert _record(db, run['run_id'], 'c' * 64, 3) == 0


def test_a_stale_privacy_epoch_cannot_replace_the_current_run(db):
    """The old close/open gap let a request carrying epoch 1 recreate an epoch
    1 run after epoch 2 had closed it.  The RPC now validates against the live
    behavior rows while it holds the same lock as reset/consent."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.user_behavior_settings where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.user_behavior_revisions where user_id = {_quote(OWNER)}::uuid;")
    old = _open_run(db, OWNER)
    _sql(db, "insert into public.user_behavior_revisions(user_id,latest_revision,history_generation) "
        f"values ({_quote(OWNER)}::uuid,0,2) on conflict(user_id) do update set history_generation=2;")
    current = _open_run(db, OWNER)
    assert current['run_id'] != old['run_id'] and current['created'] is True

    stale = _service(db, "select public.m2_open_or_join_reading_run_v2("
        f"{_quote(OWNER)}::uuid,60,"
        "'{\"_history_generation\":1,\"_consent_revision\":0}'::jsonb,60);",
        check=False)
    assert stale.returncode != 0 and 'stale reading run epoch' in stale.stderr
    still_open = _sql(db, "select run_id from public.m2_reading_runs "
        f"where user_id={_quote(OWNER)}::uuid and closed_at is null;")
    assert _last(still_open) == current['run_id'], 'the stale caller displaced the current run'
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.user_behavior_settings where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.user_behavior_revisions where user_id = {_quote(OWNER)}::uuid;")


def test_response_number_and_offset_commit_together(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    cards = [{"story_id": f"story:{index:064x}", "title": f"Story {index}",
              "source_name": "Wire", "lane": "updates", "lane_label": "fresh",
              "surprise_label": None, "exclusive_label": None} for index in range(60)]
    request_id = _freeze_page(db, OWNER, _iso(BASE), cards, run_id=run['run_id'])
    frozen_id = _last(_sql(db, "select frozen_order_id from public.m2_frozen_rankings "
        f"where request_id={_quote(request_id)}::uuid;"))
    assert _last(_service(db, "select public.m2_bind_run_frozen_order("
        f"{_quote(OWNER)}::uuid,{_quote(run['run_id'])}::uuid,{_quote(ALL_VIEW)},"
        f"{_quote(frozen_id)}::uuid,null);")) == 't'

    first = _reserve_response(db, run['run_id'], ALL_VIEW, frozen_id, 1, 0, 25)
    assert first == {'previous': 0, 'reserved': True}
    stored = _sql(db, "select v.pages_served, f.bindings->>'responses_served',"
        "f.bindings->>'last_served_offset',f.bindings->>'last_served_next_offset' "
        "from public.m2_reading_run_views v join public.m2_frozen_rankings f "
        "on f.frozen_order_id=v.frozen_order_id "
        f"where v.run_id={_quote(run['run_id'])}::uuid and v.eligibility_key={_quote(ALL_VIEW)};")
    assert _last(stored) == '1|1|0|25'
    assert _reserve_response(db, run['run_id'], ALL_VIEW, frozen_id, 1, 1, 26)['reserved'] is False
    second = _reserve_response(db, run['run_id'], ALL_VIEW, frozen_id, 2, 25, 50)
    assert second == {'previous': 1, 'reserved': True}


def test_response_reservation_takes_the_privacy_lock_before_row_locks(db):
    definition = _sql(db, "select pg_get_functiondef('public.m2_reserve_run_response("
        "uuid,uuid,text,uuid,integer,integer,integer)'::regprocedure);").stdout
    privacy_lock = definition.index("pg_advisory_xact_lock")
    view_lock = definition.index("for update of v")
    frozen_lock = definition.index("for update;", view_lock + 1)
    assert privacy_lock < view_lock < frozen_lock


def test_frozen_extension_takes_the_privacy_lock_before_its_row_lock(db):
    definition = _sql(db, "select pg_get_functiondef('public.m2_extend_frozen_ranking("
        "uuid,uuid,jsonb,jsonb)'::regprocedure);").stdout
    privacy_lock = definition.index("pg_advisory_xact_lock")
    frozen_lock = definition.index("update public.m2_frozen_rankings")
    assert privacy_lock < frozen_lock


def test_a_view_needs_a_run_that_belongs_to_the_caller(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    denied = _service(db, "select public.m2_open_run_view("
                      f"{_quote(OTHER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)});",
                      check=False)
    assert denied.returncode != 0, "a view was opened on someone else's run"


def test_the_ranking_claim_is_atomic_under_two_sessions(db):
    """Exactly one of eight concurrent callers may pay. The losers learn that
    from an empty update, not from a duplicate charge."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    statement = ("set role service_role;"
                 "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                 "select public.m2_claim_run_ranking("
                 f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)}, "
                 "gen_random_uuid(), 60);")
    processes = [subprocess.Popen(
        ['docker', 'exec', '-i', db, 'psql', '-X', '-At', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(8)]
    outputs = [process.communicate(statement)[0] for process in processes]
    granted = [json.loads(output.strip().splitlines()[-1])['granted'] for output in outputs]
    assert granted.count(True) == 1, f'{granted.count(True)} callers were allowed to pay'


def test_an_expired_claim_is_taken_over_and_a_live_one_is_not(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    assert _claim(db, run['run_id'], ALL_VIEW)['granted'] is True
    assert _claim(db, run['run_id'], ALL_VIEW)['granted'] is False, 'a live claim was handed over'
    _service(db, "update public.m2_reading_run_views set ranking_claimed_at = now() - interval '10 minutes' "
                 f"where run_id = {_quote(run['run_id'])}::uuid;")
    assert _claim(db, run['run_id'], ALL_VIEW)['granted'] is True, 'an expired claim locked her out'


def _reserve_claimed(container, run_id, key, token, amount='0.004', user_id=OWNER,
                     request_id=None):
    request_id = request_id or str(uuid.uuid4())
    result = _service(container, "select public.m2_reserve_ranker_budget_claimed("
        f"{_quote(user_id)}::uuid, {_quote(request_id)}::uuid, {amount}, 2.00, "
        f"{_quote(run_id)}::uuid, {_quote(key)}, {_quote(token)}::uuid);")
    return json.loads(_last(result))


def test_a_stale_claim_holder_cannot_reserve(db):
    """The check and the budget move are now one locked transaction. A bare
    exists() decided on a snapshot and moved money in later statements, so under
    READ COMMITTED a takeover landing in between let a stale holder reserve."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    first = _claim(db, run['run_id'], ALL_VIEW)
    assert first['granted'] is True
    # The takeover, exactly as a second request would do it.
    _service(db, "update public.m2_reading_run_views set ranking_claimed_at = now() - interval '10 minutes' "
                 f"where run_id = {_quote(run['run_id'])}::uuid;")
    second = _claim(db, run['run_id'], ALL_VIEW)
    assert second['granted'] is True and second['token'] != first['token']

    stale = _reserve_claimed(db, run['run_id'], ALL_VIEW, first['token'])
    assert stale['reserved'] is False and stale['refusal'] == 'claim_lost', stale
    # A lost claim reports no remaining budget: nothing was looked up, because
    # nothing was going to be spent either way.
    assert stale['remaining_usd'] is None, stale
    live = _reserve_claimed(db, run['run_id'], ALL_VIEW, second['token'])
    assert live['reserved'] is True
    # Exactly one reservation exists for this run's view.
    rows = _sql(db, "select count(*) from public.m2_ranker_reservations "
                    f"where user_id = {_quote(OWNER)}::uuid;")
    assert _last(rows) == '1', 'a stale holder reserved anyway'


def test_a_budget_refusal_names_what_was_left(db):
    """A refusal that only says "no" makes an operator query the ledger by hand
    to find out how close it was."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_reservations where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_daily_budget where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    held = _claim(db, run['run_id'], ALL_VIEW)
    # Spend almost the whole day, then ask for more than what is left.
    assert _reserve_claimed(db, run['run_id'], ALL_VIEW, held['token'],
                            amount='1.99')['reserved'] is True
    refused = _reserve_claimed(db, run['run_id'], ALL_VIEW, held['token'], amount='0.50')
    assert refused['reserved'] is False and refused['refusal'] == 'budget'
    assert abs(float(refused['remaining_usd']) - 0.01) < 1e-6, refused


def test_a_claim_and_a_claimed_reserve_cannot_interleave(db):
    """Both take the same row lock, so under contention the reserve either wins
    the lock and succeeds, or reads the settled takeover and refuses. What must
    never happen is two reservations for one view."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_reservations where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_daily_budget where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    held = _claim(db, run['run_id'], ALL_VIEW)
    _service(db, "update public.m2_reading_run_views set ranking_claimed_at = now() - interval '10 minutes' "
                 f"where run_id = {_quote(run['run_id'])}::uuid;")

    def session(statement):
        process = subprocess.Popen(
            ['docker', 'exec', '-i', db, 'psql', '-X', '-At', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return process, statement

    prefix = ("set role service_role;"
              "set request.jwt.claims = '{\"role\":\"service_role\"}';")
    # A: the slow holder, reserving inside a transaction that pauses first.
    slow = (prefix + "begin; select pg_sleep(0.4); select public.m2_reserve_ranker_budget_claimed("
            f"{_quote(OWNER)}::uuid, gen_random_uuid(), 0.004, 2.00, {_quote(run['run_id'])}::uuid, "
            f"{_quote(ALL_VIEW)}, {_quote(held['token'])}::uuid); commit;")
    # B: the takeover, arriving while A is asleep.
    takeover = (prefix + "select pg_sleep(0.1); select public.m2_claim_run_ranking("
                f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)}, "
                "gen_random_uuid(), 60);")
    processes = [subprocess.Popen(
        ['docker', 'exec', '-i', db, 'psql', '-X', '-At', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)]
    outputs = [processes[0].communicate(slow)[0], processes[1].communicate(takeover)[0]]
    assert all(process.returncode == 0 for process in processes), outputs
    rows = _sql(db, "select count(*) from public.m2_ranker_reservations "
                    f"where user_id = {_quote(OWNER)}::uuid;")
    assert int(_last(rows)) <= 1, 'two reservations were created for one view'


def test_eight_sessions_racing_the_claim_then_the_reserve(db):
    """The eight-session claim test, carried through to the money: only the
    granted caller may reserve."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_reservations where user_id = {_quote(OWNER)}::uuid;")
    _sql(db, f"delete from public.m2_ranker_daily_budget where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    statement = ("set role service_role;"
                 "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                 "select public.m2_claim_run_ranking("
                 f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)}, "
                 "gen_random_uuid(), 60);")
    processes = [subprocess.Popen(
        ['docker', 'exec', '-i', db, 'psql', '-X', '-At', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(8)]
    answers = [json.loads(process.communicate(statement)[0].strip().splitlines()[-1])
               for process in processes]
    winners = [answer for answer in answers if answer['granted']]
    assert len(winners) == 1
    for answer in answers:
        outcome = _reserve_claimed(db, run['run_id'], ALL_VIEW,
                                   answer['token'] or str(uuid.uuid4()))
        assert outcome['reserved'] is (answer['granted'] is True), outcome
    rows = _sql(db, "select count(*) from public.m2_ranker_reservations "
                    f"where user_id = {_quote(OWNER)}::uuid;")
    assert _last(rows) == '1', 'more than one caller reserved'


def test_a_losing_bind_cannot_overwrite_the_winners_order_in_sql(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _open_view(db, run['run_id'], ALL_VIEW)
    winner = _claim(db, run['run_id'], ALL_VIEW)
    frozen = str(uuid.uuid4())
    assert _last(_service(db, "select public.m2_bind_run_frozen_order("
        f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)}, "
        f"{_quote(frozen)}::uuid, {_quote(winner['token'])}::uuid);")) == 't'
    assert _last(_service(db, "select public.m2_bind_run_frozen_order("
        f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, {_quote(ALL_VIEW)}, "
        f"{_quote(str(uuid.uuid4()))}::uuid, {_quote(str(uuid.uuid4()))}::uuid);")) == 'f'
    stored = _sql(db, "select frozen_order_id from public.m2_reading_run_views "
                      f"where run_id = {_quote(run['run_id'])}::uuid;")
    assert _last(stored) == frozen


def test_an_out_of_range_idle_window_is_refused(db):
    assert _service(db, f"select public.m2_open_or_join_reading_run_v2({_quote(OWNER)}::uuid, 4000, '{{}}'::jsonb);",
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

def _freeze_page(container, user_id, created_at, cards, *, revision=0, run_id=None, check=True,
                 seed=True):
    """Freeze one page. By default the owner's behavior revision is seeded to
    match, so each test stands alone instead of inheriting whatever revision a
    previous test happened to leave behind. The tests that deliberately bind a
    MISMATCHED revision pass seed=False and seed it themselves."""
    if seed:
        _behavior_revision(container, user_id, revision)
    request_id = str(uuid.uuid4())
    bindings = {"history_generation": 1, "server_commit_revision": revision, "consent_revision": 0,
                "result_mode": "heuristic", "fallback_reason": "test",
                "eligibility": {"category": None, "query": None}, "short_lane_reasons": []}
    result = _service(container, "insert into public.m2_frozen_rankings"
             "(request_id,user_id,bindings,cards,page_size,expires_at,created_at,run_id) values ("
             f"{_quote(request_id)}::uuid, {_quote(user_id)}::uuid, {_quote(json.dumps(bindings))}::jsonb, "
             f"{_quote(json.dumps(cards))}::jsonb, 25, now() + interval '15 minutes', "
             f"{_quote(created_at)}::timestamptz, "
             + (f"{_quote(run_id)}::uuid" if run_id else "null") + ");", check=check)
    return request_id if result.returncode == 0 else None


def _behavior_revision(container, user_id, revision):
    """Seed the owner's behavior revision AS THE SUPERUSER.

    service_role deliberately has no grant on user_behavior_revisions (it is an
    owner-scoped table reached only through the behavior RPCs), so seeding it is
    test setup and not a claim about what the service can reach.
    """
    _sql(container, "insert into public.user_behavior_revisions(user_id, latest_revision, history_generation) "
         f"values ({_quote(user_id)}::uuid, {revision}, 1) on conflict (user_id) do update "
         f"set latest_revision = {revision};")


def test_a_paid_order_inside_an_open_run_survives_a_behavior_write(db):
    """The race Codex named: the history re-check passes, a behavior event lands,
    then the insert runs. Outside a run the trigger still demands exactness; the
    relaxation is scoped to an OPEN run, where the order is frozen on purpose."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _behavior_revision(db, OWNER, 7)
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "Headline", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    # Computed against revision 5, inserted after a write moved it to 7.
    assert _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=5, seed=False,
                        run_id=run['run_id']) is not None, 'a paid order was discarded'
    # Without a run, the same lag is still refused.
    assert _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=5, seed=False,
                        check=False) is None
    # A revision from the FUTURE is refused even inside a run: that is not lag.
    assert _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=99, seed=False,
                        run_id=run['run_id'], check=False) is None


def test_a_closed_run_gets_the_strict_check_back(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _service(db, f"update public.m2_reading_runs set closed_at = now() where run_id = {_quote(run['run_id'])}::uuid;")
    _behavior_revision(db, OWNER, 7)
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "Headline", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    assert _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=5, seed=False,
                        run_id=run['run_id'], check=False) is None


def test_a_continuation_appends_to_the_same_order(db):
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _behavior_revision(db, OWNER, 7)
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "First", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    request_id = _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=7, run_id=run['run_id'])
    assert request_id is not None
    more = [{"story_id": _story_id(MIXED), "title": "Older", "source_name": "cnBeta",
             "lane": "updates", "lane_label": "fresh", "surprise_label": None, "exclusive_label": None}]
    result = _service(db, "select public.m2_extend_frozen_ranking("
                      f"{_quote(OWNER)}::uuid, (select frozen_order_id from public.m2_frozen_rankings "
                      f"where request_id = {_quote(request_id)}::uuid), "
                      f"{_quote(json.dumps(more))}::jsonb, '{{\"corpus_has_more\": false}}'::jsonb);")
    assert _last(result) == '2', 'the continuation must append, not replace'
    stored = _sql(db, "select cards->0->>'title', cards->1->>'title' from public.m2_frozen_rankings "
                      f"where request_id = {_quote(request_id)}::uuid;")
    assert _last(stored) == 'First|Older', 'the first page must not move'


def test_closing_a_reading_run_does_not_fail_on_its_own_frozen_orders(db):
    """The FK is `on delete set null`, so deleting a run UPDATES every ranking
    that names it. That update changes no cards and no bindings, so it is not a
    ranking write and must not be re-validated."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _behavior_revision(db, OWNER, 11)
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "Headline", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    assert _freeze_page(db, OWNER, '2026-09-18T09:30:00Z', cards, revision=11,
                        run_id=run['run_id']) is not None
    # The behavior revision then moves, as it does all day.
    _behavior_revision(db, OWNER, 12)
    deleted = _service(db, "delete from public.m2_reading_runs where run_id = "
                       f"{_quote(run['run_id'])}::uuid;", check=False)
    assert deleted.returncode == 0, 'deleting a run must not fail on its own rankings'


def test_an_owner_cannot_extend_another_owners_order(db):
    denied = _as_owner(db, OTHER, "select public.m2_extend_frozen_ranking("
                       f"{_quote(OTHER)}::uuid, gen_random_uuid(), '[]'::jsonb, '{{}}'::jsonb);",
                       check=False)
    assert denied.returncode != 0


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


def test_the_review_shows_what_less_like_this_removed(db):
    """The round trip: the service records the filter, and the hour review shows
    it. Without both halves a reviewed page cannot be told apart from a page
    that never carried those cards."""
    _service(db, f"delete from public.m2_reading_runs where user_id = {_quote(OWNER)}::uuid;")
    run = _open_run(db, OWNER)
    _behavior_revision(db, OWNER, 3)
    story = _story_id(MIXED)
    _service(db, "select public.m2_record_reading_run_filter("
                 f"{_quote(OWNER)}::uuid, {_quote(run['run_id'])}::uuid, array[{_quote(story)}]::text[]);")
    cards = [{"story_id": _story_id(MULTI_OUTLET), "title": "Headline", "source_name": "Reuters",
              "lane": "hot", "lane_label": "hot", "surprise_label": None, "exclusive_label": None}]
    request_id = str(uuid.uuid4())
    bindings = {"history_generation": 1, "server_commit_revision": 3, "consent_revision": 0,
                "result_mode": "heuristic", "fallback_reason": "test", "run_id": run['run_id'],
                "eligibility": {"category": None, "query": None}, "short_lane_reasons": []}
    _service(db, "insert into public.m2_frozen_rankings"
             "(request_id,user_id,bindings,cards,page_size,expires_at,created_at,run_id) values ("
             f"{_quote(request_id)}::uuid, {_quote(OWNER)}::uuid, {_quote(json.dumps(bindings))}::jsonb, "
             f"{_quote(json.dumps(cards))}::jsonb, 25, now() + interval '15 minutes', "
             f"'2026-09-18T10:30:00Z'::timestamptz, {_quote(run['run_id'])}::uuid);")
    result = _as_owner(db, OWNER, "select coalesce(jsonb_agg(value),'[]'::jsonb) from "
                       "public.m2_owner_reading_pages('2026-09-18T10:00:00Z'::timestamptz) as rows(value);")
    pages = json.loads(_last(result))
    assert pages and pages[0]['filtered_story_ids'] == [story]


def test_one_owner_cannot_review_another_owners_pages(db):
    result = _as_owner(db, OTHER, "select count(*) from "
                       "public.m2_owner_reading_pages('2026-09-18T08:00:00Z'::timestamptz) as rows(value);")
    assert _last(result) == '0'


def test_filtered_rpc_refills_limit_after_owner_filters(db):
    baseline = _lane(db, limit=200)
    blocked_source = baseline[0]['source_id']
    expected_source = next(row for row in baseline if row['source_id'] != blocked_source)
    assert _filtered(db, limit=1, sources=(blocked_source,))[0]['story_id'] == expected_source['story_id']
    assert _filtered(db, limit=1, excluded=(baseline[0]['story_id'],))[0]['story_id'] == baseline[1]['story_id']
    blocked_topic = baseline[0]['category_ids'][0]
    expected_topic = next(row for row in baseline if blocked_topic not in row['category_ids'])
    assert _filtered(db, limit=1, topics=(blocked_topic,))[0]['story_id'] == expected_topic['story_id']


def test_filtered_rpc_refills_seventy_five_or_reports_finite_corpus(db):
    for label, total in [('long-head', 230), ('finite-head', 130)]:
        rows = [_row(f'https://example.test/{label}-{index}',
                     title=f'{label} unique title {index}',
                     source_id='blocked-wire' if index < 97 else f'eligible-{index}',
                     category_ids=[label, 'blocked-topic' if index < 97 else 'other-topic'],
                     published_at=_iso(NOW - timedelta(minutes=index + 10)))
                for index in range(total)]
        _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
        found = _filtered(db, category_id=label, limit=75, topics=('blocked-topic',))
        assert len(found) == (75 if label == 'long-head' else 33)
        assert all('blocked-topic' not in row['category_ids'] for row in found)


def test_filtered_rpc_does_not_reveal_older_twin_of_suppressed_winner(db):
    title = 'Owner filter after dedupe proof ' + uuid.uuid4().hex
    older = _row('https://example.test/filtered-older-' + uuid.uuid4().hex,
                 title=title, source_id='allowed-wire',
                 published_at=_iso(NOW - timedelta(minutes=20)))
    newer = _row('https://example.test/filtered-newer-' + uuid.uuid4().hex,
                 title=title, source_id='blocked-wire',
                 published_at=_iso(NOW - timedelta(minutes=10)))
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps([older, newer]))}::jsonb);")
    baseline = [row for row in _lane(db, limit=200) if row['title'] == title]
    assert [row['story_id'] for row in baseline] == [newer['story_id']]
    filtered = [row for row in _filtered(db, limit=200, sources=('blocked-wire',))
                if row['title'] == title]
    assert filtered == []


@pytest.mark.parametrize('lane, categories, sources', [
    (None, (), ()), ('hot', (), ()),
    ('interested', ('science',), ('quanta',)),
    ('surprise', ('science',), ('quanta',)),
])
def test_filtered_rpc_without_owner_filters_matches_v2_in_every_lane_and_cursor(
        db, lane, categories, sources):
    kwargs = {'categories': categories, 'sources': sources, 'limit': 50}
    original = _lane(db, lane, **kwargs)
    filtered = _filtered(db, lane=lane, categories=categories, profile_sources=sources)
    assert [row['story_id'] for row in filtered] == [row['story_id'] for row in original]
    if len(original) > 1:
        head = original[0]
        before = (head['independent_source_count'] if lane == 'hot' else None,
                  head['published_at'], head['story_id'])
        old_tail = _lane(db, lane, before=before, **kwargs)
        new_tail = _filtered(db, lane=lane, categories=categories,
                             profile_sources=sources, before=before)
        assert [row['story_id'] for row in new_tail] == [row['story_id'] for row in old_tail]


def test_filtered_rpc_refuses_public_roles_and_oversized_filters(db):
    statement = 'select count(*) from public.m2_retained_candidates_filtered();'
    anon = _sql(db, 'set role anon;' + statement, check=False)
    owner = _as_owner(db, OWNER, statement, check=False)
    assert anon.returncode != 0 and 'permission denied' in anon.stderr
    assert owner.returncode != 0 and 'permission denied' in owner.stderr
    oversized = _service(db, 'select count(*) from public.m2_retained_candidates_filtered('
                         "p_excluded_story_ids => array_fill('x'::text, array[1201]));", check=False)
    assert oversized.returncode != 0 and 'invalid filter size' in oversized.stderr
