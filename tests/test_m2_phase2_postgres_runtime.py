"""The M2.1 Phase 2 migrations APPLIED on PostgreSQL 17.11, not string-matched.

A string-matched migration test proves the file contains some text. It does not
prove the table installs, the partial unique index holds under concurrency, or
that a lane filter returns what it claims. Same container shape as
tests/test_m2_translation_postgres_runtime.py.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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
    'supabase/migrations/202609180003_m2_frozen_ranking_run_scope.sql',
    'supabase/migrations/202609180004_m2_retained_corpus_prune.sql',
    'supabase/migrations/202609180005_m2_reading_run_page_budget.sql',
    'supabase/migrations/202609180006_m2_reading_run_ranking_claim.sql',
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
        arguments += [f'p_before_source_count => {count}',
                      f'p_before_published_at => {_quote(published)}::timestamptz',
                      f'p_before_story_id => {_quote(story)}']
    result = _service(container, "select coalesce(jsonb_agg(value), '[]'::jsonb) from "
                      f"public.m2_retained_candidates_v2({', '.join(arguments)}) as rows(value);",
                      check=check)
    if not check and result.returncode != 0:
        return None
    return json.loads(_last(result))


def _by_story(rows):
    return {row['story_id']: row for row in rows}


# --- coverage and hot ------------------------------------------------------

def test_every_phase_two_migration_is_a_no_op_on_a_re_run(db):
    """Applying a migration twice must not error. A recovery re-run should not
    depend on anyone remembering whether it already ran."""
    for migration in ('supabase/migrations/202609180001_m2_retained_coverage_and_lanes.sql',
                      'supabase/migrations/202609180002_m2_reading_runs.sql',
                      'supabase/migrations/202609180003_m2_frozen_ranking_run_scope.sql',
                      'supabase/migrations/202609180004_m2_retained_corpus_prune.sql',
                      'supabase/migrations/202609180005_m2_reading_run_page_budget.sql',
                      'supabase/migrations/202609180006_m2_reading_run_ranking_claim.sql'):
        again = _sql(db, (ROOT / migration).read_text(), check=False)
        assert again.returncode == 0, f'{migration} is not idempotent: {again.stderr[:400]}'


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


def test_the_hot_lane_pages_on_its_own_sort_key_without_skipping_or_repeating(db):
    """The hot lane orders by independent source count first. A published_at
    cursor cannot describe that boundary, so the cursor is the whole sort key."""
    rows = []
    for index in range(6):
        url = f'https://example.test/hot-keyset-{index}'
        rows.append(_row(url, source_id=f'wire{index}', source_name=f'Wire {index}',
                         published_at='2026-09-18T09:00:00Z', source_observed_at='2026-09-18T09:00:00Z'))
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    coverage = []
    for index in range(6):
        story = _story_id(f'https://example.test/hot-keyset-{index}')
        # Deliberately DIFFERENT counts with the SAME published_at, which is
        # exactly the shape a published_at-only cursor gets wrong.
        for publisher in range(2 + index % 3):
            coverage.append({'story_id': story, 'publisher_id': f'pub{publisher}',
                             'is_independent': True, 'first_seen_at': '2026-09-18T09:00:00Z'})
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
    rows = [_row(old_url, source_id='oldwire', source_name='Old Wire',
                 published_at='2026-08-01T10:00:00Z', source_observed_at='2026-08-01T10:00:00Z'),
            _row(recent_url, source_id='newwire', source_name='New Wire',
                 published_at=datetime.now(timezone.utc).isoformat(),
                 source_observed_at=datetime.now(timezone.utc).isoformat())]
    _service(db, f"select public.m2_ingest_retained_corpus({_quote(json.dumps(rows))}::jsonb);")
    coverage = [{'story_id': _story_id(url), 'publisher_id': publisher, 'is_independent': True,
                 'first_seen_at': datetime.now(timezone.utc).isoformat()}
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
    result = _service(container, "select public.m2_open_or_join_reading_run("
                      f"{_quote(user_id)}::uuid, {idle}, {_quote(profile)}::jsonb, {max_minutes});")
    return json.loads(_last(result))


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
    assert _service(db, f"select public.m2_open_or_join_reading_run({_quote(OWNER)}::uuid, 60, '{{}}'::jsonb, 5);",
                    check=False).returncode != 0
    assert _service(db, f"select public.m2_open_or_join_reading_run({_quote(OWNER)}::uuid, 60, '{{}}'::jsonb, 999);",
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
