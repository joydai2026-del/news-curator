"""What m2_retained_candidates_v2 COSTS at real corpus scale, on PostgreSQL 17.11.

Production POST /rank answered 503 "Supabase request failed" after about 4.8s
against a 3.0s client timeout. This file is the measurement that found it and
the measurement that proves it fixed: the numbers below were taken on this exact
fixture before and after 202609210001 replaced the quadratic dedupe with a
window function.

The shape of the measurement matters as much as the number:

  * ~7,000 retained rows, which is the live corpus (about 6.5K) plus headroom,
    spread across the 14-day retention window rather than piled on one instant.
    The lane age bounds are the thing that decides how many rows each lane can
    even see, so a corpus seeded "2 hours ago" would put every row in the
    updates lane and measure a query the feed never runs.
  * Each lane called with the arguments curator/recommendation/service.py
    `_pool_rows` actually sends, read off that function and
    config/ranking-policy-r2.yaml, not invented here.
  * EXPLAIN (ANALYZE, BUFFERS) of the query INSIDE the function, captured with
    auto_explain rather than EXPLAIN on the function call (which only ever
    reports a Function Scan and tells you nothing). The full plan is printed to
    the test log so CI carries the evidence.
  * A worst case the natural corpus does not contain: 3,000 rows carrying ONE
    identical title in one language, all inside the dedupe window. That is the
    input the old correlated NOT EXISTS was quadratic in, so it is the input a
    fix has to survive.

HOW TO RUN IT

    pytest tests/test_m2_candidate_scale_postgres_runtime.py -s

Every test in this file runs in CI. Nothing is deselected any more: when this
file was written, general-pool alone took 102 seconds and two lanes had to be
tagged SLOW and skipped by name. The whole file now finishes in seconds, which
is the point.

Container fixture, MIGRATIONS ordering and the _sql / _service helpers are
lifted from tests/test_m2_phase2_postgres_runtime.py unchanged.
"""
from __future__ import annotations

import json
import re
import shutil
import statistics
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
    'supabase/migrations/202609180007_m2_claimed_ranker_reservation.sql',
    'supabase/migrations/202609180101_m2_retained_candidates_dedupe.sql',
    'supabase/migrations/202609180102_m2_retained_candidates_v2_dedupe.sql',
    'supabase/migrations/202609210001_m2_retained_candidates_v2_dedupe_linear.sql',
    'supabase/migrations/202609230001_m2_retained_candidates_v2_bulk_general.sql',
    'supabase/migrations/202609230002_m2_retained_candidates_filtered.sql',
    'supabase/migrations/202609230003_m2_opened_candidate_ids.sql',
    'supabase/migrations/202609230004_m2_retained_candidates_for_owner.sql',
    'supabase/migrations/202609230005_m2_retained_candidates_general_narrow_for_owner.sql',
    'supabase/migrations/202609240003_m2_fast_owner_all_pools.sql',
    'supabase/migrations/202609240004_m2_activate_fast_owner_all_pools.sql',
)

# Seeded corpus size. The live retained corpus was about 6,500 rows when the
# 503 was investigated on 2026-09-21; 7,000 is that plus headroom.
CORPUS_ROWS = 7000

# The ten category ids topics.yaml actually ships. A synthetic 'cat1'..'catN'
# set would give the planner a different selectivity than production has.
CATEGORY_IDS = ('ai', 'crypto', 'quantum', 'energy', 'space', 'biotech',
                'world', 'us-news', 'business', 'trending')

# THE MEASUREMENT, taken 2026-09-21 on this exact fixture (7,000 rows,
# postgres:17.11 in Docker on an Apple M4, three repeats, median), BEFORE and
# AFTER supabase/migrations/202609210001:
#
#     lane             before      after    vs the 3.0s client timeout
#     --------------  ---------  ---------   --------------------------
#     updates             72 ms     7.4 ms   fit before, fits now
#     hot                111 ms    10.3 ms   fit before, fits now
#     surprise           568 ms    18.1 ms   fit before, fits now
#     interested      15,481 ms   157.0 ms   was 5x over, now fits
#     general-pool   102,241 ms   280.7 ms   was 34x over, now fits
#
# All five lanes together are now about 474 ms, against the 4.8s ONE of them
# was taking in production. The `after` column is one clean run with nothing
# else on the machine; an earlier run sharing the laptop with a second
# container measured the same lanes 30% to 90% higher, which is worth knowing
# before anyone reads a single number here as precise.
#
# The bill was the `chosen` CTE: a correlated NOT EXISTS against the `visible`
# CTE, which has no indexes, so the planner ran `CTE Scan on visible peer` once
# per row. 7,000 loops over a 7,000-row CTE is about 49 million comparisons,
# each calling m2_story_dedupe_key twice. 202609210001 replaced it with a
# lead() over the same set: one sort, one pass, the key computed once per row.
# The rule is unchanged, which tests/test_m2_candidate_dedupe_postgres_runtime.py
# and test_the_identical_title_worst_case_is_not_quadratic below both pin.
#
# The budget is 1500 ms per lane, the number PR #51's reviewers asked to have
# proven. It is not a target anybody chose and it is not the production timeout:
# the feed issues five of these calls per /rank, so a lane AT the budget still
# means a request that cannot fit the client's timeout. Read a PASS as "no lane
# is pathological at this scale", not as "the endpoint is fast enough".
#
# If this ever fails, do NOT raise the number. The query is doing work the
# lane's own filters should have avoided, and the plan printed below says which.
LANE_BUDGET_MS = 1500.0

# What the HTTP client allows for the WHOLE request. Named here because the
# budgets above only mean something against it.
CLIENT_TIMEOUT_MS = 3000.0

# Repeats per lane. The first execution of a plpgsql function in a session pays
# for parse and plan; the median of several runs is the steady-state cost the
# feed actually pays on a warm connection pool.
REPEATS = 3


def _run(*args, input_text=None, check=True, timeout=180):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=timeout, check=check)


def _sql(container, sql, check=True, timeout=180):
    return _run('docker', 'exec', '-i', container, 'psql', '-X', '-At', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', input_text=sql, check=check, timeout=timeout)


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _service(container, statement, check=True, timeout=180):
    return _sql(container, "set role service_role;"
                "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                + statement, check=check, timeout=timeout)


def _last(result):
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ''


@pytest.fixture(scope='module')
def db():
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable; PostgreSQL 17.11 runtime not verified')
    if _run('docker', 'image', 'inspect', IMAGE, check=False).returncode:
        pytest.skip('Installed PostgreSQL 17.11 image or Docker daemon unavailable; no pull attempted')
    container = 'news-curator-scale-' + uuid.uuid4().hex[:10]
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
        _sql(container, "insert into auth.users(id) values "
            "('11111111-1111-1111-1111-111111111111');")
        _seed_corpus_at_scale(container)
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


def _seed_corpus_at_scale(container):
    """Seven thousand rows via generate_series, not seven thousand round trips.

    Written as set-based SQL rather than through m2_ingest_retained_corpus on
    purpose: that RPC is a row-at-a-time plpgsql loop and seeding through it
    would spend the test's whole budget on the WRITE path, which is not what is
    being measured. Every value below still satisfies the same constraints the
    ingest RPC enforces, including the story_id = 'story:' || sha256(url) rule
    that canonical_stories checks.

    Shape, and why each piece is there:

      published_at  168 seconds apart, so 7,000 rows cover 13.6 days: inside the
                    14-day retention floor and across every lane's age window.
                    About 128 rows land in the 6h updates window, about 385 more
                    in the 6-24h hot window, about 900 in the 6-48h surprise
                    window, and the rest only the profile lanes can see.
      language      one row in four is zh, which is roughly the live mix and
                    matters because the dedupe peer test joins on language.
      source_id     twelve sources, two of them aggregators, so the surprise
                    lane's non-aggregator rule and the hot lane's aggregator
                    discount both have something to exclude.
      titles        one row in thirteen belongs to a near-duplicate PAIR eleven
                    minutes apart (same language, inside the 36h dedupe window),
                    the rest are unique. A corpus of 7,000 unique titles would
                    make the anti-join trivially empty and would flatter the
                    dedupe CTE; a corpus of 7,000 identical titles would be a
                    worst case that does not exist. Both are measured below.
      coverage      about 40% of stories carry 2-4 independent publishers plus
                    an aggregator echo, so independent_source_count is
                    non-trivial and the hot lane has a real pool.
    """
    _sql(container, f"""
      insert into public.canonical_stories(
        story_id, canonical_url, title, summary, language, source_kind, source_name, published_at)
      select
        'story:' || encode(extensions.digest('https://scale.test/story/' || i, 'sha256'), 'hex'),
        'https://scale.test/story/' || i,
        case when i % 26 in (0, 4) then 'Markets react to the overnight decision, round ' || (i / 26)
             else 'Story number ' || i || ': ' || (array['regulators open a review of',
                    'a second outlet confirms', 'engineers describe', 'the filing shows',
                    'analysts disagree about'])[1 + i % 5] || ' the ' ||
                  (array['reactor restart','satellite launch','chip export rule','clinical trial',
                         'settlement talks','grid outage'])[1 + i % 6] end,
        'Summary body for story ' || i || '. ' || repeat('Context sentence. ', 4),
        case when i % 4 = 0 then 'zh' else 'en' end,
        'outlet',
        'Source ' || (i % 12),
        now() - make_interval(secs => i * 168)
      from generate_series(1, {CORPUS_ROWS}) as g(i);

      insert into public.retained_corpus_observations(
        story_id, source_id, source_name, source_is_aggregator, language, title, summary,
        canonical_url, published_at, first_observed_at, source_observed_at)
      select s.story_id, 'source-' || (g.i % 12), 'Source ' || (g.i % 12), (g.i % 12) < 2,
             s.language, s.title, s.summary, s.canonical_url, s.published_at,
             s.published_at, s.published_at
      from generate_series(1, {CORPUS_ROWS}) as g(i)
      join public.canonical_stories s
        on s.canonical_url = 'https://scale.test/story/' || g.i;

      -- One or two categories per story, drawn from the ten topics.yaml ships.
      insert into public.retained_corpus_categories(story_id, category_id)
      select o.story_id, c.category_id from public.retained_corpus_observations o
      cross join lateral (
        select unnest(case when (('x' || substr(md5(o.story_id), 1, 8))::bit(32)::bigint % 3) = 0
          then array[(array{list(CATEGORY_IDS)})[1 + (('x' || substr(md5(o.story_id), 1, 8))::bit(32)::bigint % 10)],
                     (array{list(CATEGORY_IDS)})[1 + (('x' || substr(md5(o.story_id), 9, 8))::bit(32)::bigint % 10)]]
          else array[(array{list(CATEGORY_IDS)})[1 + (('x' || substr(md5(o.story_id), 1, 8))::bit(32)::bigint % 10)]]
          end) as category_id) c
      on conflict do nothing;

      -- Coverage for about 40% of stories: 2-4 independent publishers plus one
      -- aggregator echo that must NOT be counted.
      insert into public.retained_corpus_coverage(story_id, publisher_id, is_independent, first_seen_at)
      select o.story_id, p.publisher_id, p.is_independent, o.published_at
      from public.retained_corpus_observations o
      cross join lateral (
        select 'pub-' || n as publisher_id, true as is_independent
        from generate_series(0, 1 + (('x' || substr(md5(o.story_id), 1, 8))::bit(32)::bigint % 3)) as n
        union all select 'echo-aggregator', false) p
      where (('x' || substr(md5(o.story_id), 17, 8))::bit(32)::bigint % 10) < 4
      on conflict do nothing;

      analyze public.canonical_stories;
      analyze public.retained_corpus_observations;
      analyze public.retained_corpus_categories;
      analyze public.retained_corpus_coverage;
    """, timeout=300)


# --- the call shapes the feed actually sends --------------------------------
#
# Read off curator/recommendation/service.py `_pool_rows` together with
# config/ranking-policy-r2.yaml. Nothing here is a guess:
#
#   trend.window_hours = 24, trend.min_independent_sources = 2
#   composition.updates_max_age_hours = 6
#   exploration.max_age_hours = 48
#   candidate_window_size = 50, page_size = 25
#
#   general pool  lane null, no age bound, limit = 50 + 25 = 75
#   updates       max_age 6,  min_age none, limit min(100, max(14*3, 10)) = 42
#   hot           max_age 24, min_age 6,    limit min(100, max( 8*3, 10)) = 24
#   interested    max_age none, min_age 6,  limit min(100, max(22*3, 10)) = 66
#   surprise      max_age 48, min_age 6,    limit min(100, max( 6*3, 10)) = 18
#
# lane_window_quotas(50) with the revision-2 ratios gives
# interested 22, updates 14, hot 8, surprise 6.
PROFILE_CATEGORIES = ('ai', 'energy', 'world')
PROFILE_SOURCES = ('source-3', 'source-7')

LANE_CALLS = {
    'general-pool': dict(lane=None, max_age=None, min_age=None, limit=75, profile=False),
    'updates': dict(lane='updates', max_age=6, min_age=None, limit=42, profile=True),
    'hot': dict(lane='hot', max_age=24, min_age=6, limit=24, profile=True),
    'interested': dict(lane='interested', max_age=None, min_age=6, limit=66, profile=True),
    'surprise': dict(lane='surprise', max_age=48, min_age=6, limit=18, profile=True),
}


def _call_sql(spec, *, limit=None):
    lane = spec['lane']
    categories = (f"array{list(PROFILE_CATEGORIES)}::text[]" if spec['profile'] else 'null')
    sources = (f"array{list(PROFILE_SOURCES)}::text[]" if spec['profile'] else 'null')
    arguments = [
        'p_category_id => null', 'p_query => null',
        'p_lane => ' + (_quote(lane) if lane else 'null'),
        f'p_profile_categories => {categories}', f'p_profile_sources => {sources}',
        'p_trend_window_hours => 24', 'p_trend_min_sources => 2',
        'p_max_age_hours => ' + ('null' if spec['max_age'] is None else str(spec['max_age'])),
        'p_min_age_hours => ' + ('null' if spec['min_age'] is None else str(spec['min_age'])),
        f"p_limit => {limit or spec['limit']}",
    ]
    return ("select count(*) from public.m2_retained_candidates_v2("
            + ', '.join(arguments) + ") as rows(value);")


_TIMING = re.compile(r'^Time:\s+([0-9.]+)\s+ms', re.MULTILINE)


def _timed(container, statement, repeats=REPEATS, prelude='', postlude='', timeout=900):
    """Median server-side wall clock, from psql's own \\timing.

    Measured inside the container so docker-exec startup is not counted as
    query cost. The SET statements run before \\timing is on, so every number
    parsed out belongs to the RPC call itself.

    `prelude` and `postlude` let a caller seed extra rows inside a transaction
    it then rolls back, so a worst case can be measured without leaving the
    shared corpus different for whatever test runs next. The prelude runs
    BEFORE the role switch, as the connecting superuser, because seeding needs
    the extensions schema that service_role deliberately cannot reach. Both run
    with timing off, so their cost is never counted as the query's.
    """
    script = (prelude
              + "set role service_role;\n"
              + "set request.jwt.claims = '{\"role\":\"service_role\"}';\n"
              + "\\timing on\n" + (statement + "\n") * repeats
              + "\\timing off\n" + postlude)
    result = _sql(container, script, timeout=timeout)
    samples = [float(value) for value in _TIMING.findall(result.stdout)]
    assert len(samples) == repeats, f'expected {repeats} timings, parsed {samples}'
    return samples


def _explain(container, statement):
    """The plan of the query INSIDE the function.

    EXPLAIN on a plpgsql function call reports a Function Scan and nothing
    about what the function does, so auto_explain is loaded with nested
    statements on and its log_level pushed to NOTICE, which routes the plan to
    the client instead of the server log.
    """
    script = ("load 'auto_explain';\n"
              # 1 ms, not 0. m2_story_dedupe_key carries `set search_path`, which
              # blocks SQL-function inlining, so at log_min_duration=0 auto_explain
              # emits one plan per CALL of it, and log_nested_statements has to
              # stay on because the RPC body is itself a nested statement. 1 ms
              # keeps the plan that matters and drops every sub-millisecond key
              # call. It used to be 50, which was fine when the cheapest lane
              # took 72 ms; after 202609210001 a lane can finish under 50 ms and
              # the capture would come back empty.
              "set auto_explain.log_min_duration = 1;\n"
              "set auto_explain.log_analyze = on;\n"
              "set auto_explain.log_buffers = on;\n"
              # log_timing OFF on purpose. With per-node timing on, the 49M-comparison
              # self-join in the general pool takes twenty times longer to
              # instrument than to run, and CI would time out capturing evidence
              # about a query it already timed. Row counts, loop counts and the
              # total are still exact; only the per-node splits are gone.
              "set auto_explain.log_timing = off;\n"
              "set auto_explain.log_nested_statements = on;\n"
              "set auto_explain.log_level = 'notice';\n"
              "set role service_role;\n"
              "set request.jwt.claims = '{\"role\":\"service_role\"}';\n"
              + statement + "\n")
    result = _sql(container, script, timeout=900)
    return result.stderr


def _dominant(plan_text):
    """The node that does the most work in the plan.

    With log_timing off there are no per-node milliseconds, so "most work" is
    loops x rows: the self-join shows up as `loops=7000` over a 7,000-row CTE
    scan, which is exactly the thing worth naming.
    """
    best = (0, '')
    for line in plan_text.splitlines():
        match = re.search(r'rows=([0-9]+) loops=([0-9]+)', line)
        if match:
            work = int(match.group(1)) * int(match.group(2))
            if work >= best[0]:
                best = (work, line.strip())
    return best


# --- the measurements -------------------------------------------------------

def test_owner_narrow_general_matches_old_owner_rpc_at_scale(db):
    owner = '11111111-1111-1111-1111-111111111111'
    rows = int(_last(_sql(db, 'select count(*) from public.retained_corpus_observations;')))
    assert rows == CORPUS_ROWS
    # A long opened head must be skipped before LIMIT, not returned as a short page.
    _sql(db, "insert into public.user_story_state(user_id,story_id,read_at) "
        f"select '{owner}'::uuid, story_id, now() from ("
        "select story_id from public.retained_corpus_observations "
        "order by published_at desc, story_id desc limit 100) head "
        "on conflict(user_id,story_id) do update set read_at=excluded.read_at;")
    for limit in (75, 200):
        for filters in ('', ", p_suppressed_sources => array['source-3','source-7']::text[], "
                       "p_suppressed_topics => array['ai']::text[]"):
            args = f"p_owner_id => '{owner}', p_hide_already_opened => true, p_limit => {limit}{filters}"
            def payload(name, arguments):
                value = _last(_service(db, "select coalesce(jsonb_agg(value),'[]'::jsonb) "
                    f"from public.{name}({arguments}) rows(value);"))
                return json.loads(value)
            old = payload('m2_retained_candidates_for_owner', args)
            new = payload('m2_retained_candidates_general_narrow_for_owner', args)
            assert new == old and len(new) == limit
            cursor = (f", p_before_published_at => {_quote(old[0]['published_at'])}, "
                      f"p_before_story_id => {_quote(old[0]['story_id'])}")
            assert payload('m2_retained_candidates_general_narrow_for_owner', args + cursor) == (
                payload('m2_retained_candidates_for_owner', args + cursor))
            samples = _timed(db, 'select count(*) from '
                f'public.m2_retained_candidates_general_narrow_for_owner({args});')
            median = statistics.median(samples)
            print(f'owner narrow general {limit} rows: {median:.1f} ms median of {REPEATS}')
            assert median < LANE_BUDGET_MS, f'owner narrow general read took {median:.1f} ms'



def test_fast_owner_all_pools_keep_bytes_and_sharply_reduce_work(db):
    owner = '11111111-1111-1111-1111-111111111111'
    def payload(name, args):
        value = _last(_service(db, "select coalesce(jsonb_agg(value),'[]'::jsonb) "
            f"from public.{name}({args}) rows(value);"))
        return json.loads(value)
    shared = f"p_owner_id => '{owner}', p_hide_already_opened => true"
    general_old = 'm2_retained_candidates_for_owner'
    general_new = 'm2_retained_candidates_general_narrow_for_owner'
    interested_new = 'm2_retained_candidates_interested_narrow_for_owner'
    profiles = (
        "p_profile_categories => array['ai','energy','world']::text[], "
        "p_profile_sources => array['source-3','source-7']::text[]")
    cursor = ("p_before_published_at => now() - interval '2 days', "
              "p_before_story_id => 'story:zz'")
    suppress = ("p_suppressed_sources => array['source-7']::text[], "
                "p_suppressed_topics => array['ai']::text[]")
    for extras in ('', cursor, suppress, cursor + ', ' + suppress,
                   'p_dedupe_window_hours => 0'):
        args = shared + ', p_limit => 75' + (', ' + extras if extras else '')
        assert payload(general_new, args) == payload(general_old, args)
    for extras in ('', cursor, suppress, cursor + ', ' + suppress,
                   'p_dedupe_window_hours => 0'):
        args = shared + ', ' + profiles + ', p_min_age_hours => 6, p_limit => 66'
        if extras:
            args += ', ' + extras
        assert payload(interested_new, args) == payload(general_old,
            args + ", p_lane => 'interested'")
    for new_name, old_name, args in (
        (general_new, general_old, shared + ', p_limit => 75'),
        (interested_new, general_old,
            shared + ', ' + profiles + ', p_min_age_hours => 6, p_limit => 66')):
        old_args = args + (", p_lane => 'interested'" if new_name == interested_new else '')
        old_ms = statistics.median(_timed(db,
            f'select count(*) from public.{old_name}({old_args});'))
        new_ms = statistics.median(_timed(db,
            f'select count(*) from public.{new_name}({args});'))
        print(f'{new_name}: {old_ms:.1f} ms old, {new_ms:.1f} ms new')
        assert new_ms < old_ms * 0.6, 'the narrow route did not reduce measured work'
        assert new_ms < LANE_BUDGET_MS, f'owner narrow {new_name} took {new_ms:.1f} ms'



def test_fast_owner_paths_keep_tied_duplicate_hidden_after_owner_filters(db):
    # Same publication instant uses story_id as the final dedupe tie-break.
    # The newer twin is opened and explicitly excluded, but still suppresses
    # its older copy because dedupe precedes those owner filters.
    owner = '11111111-1111-1111-1111-111111111111'
    title = 'Tied headline for indexed owner deduplication'
    script = f"""begin;
      insert into public.canonical_stories(
        story_id, canonical_url, title, summary, language, source_kind, source_name, published_at)
      select 'story:' || encode(extensions.digest('https://tie.test/' || i, 'sha256'), 'hex'),
        'https://tie.test/' || i, '{title}', '', 'en', 'outlet', 'Tie Source',
        now() - interval '7 hours' from generate_series(1,2) g(i);
      insert into public.retained_corpus_observations(
        story_id, source_id, source_name, source_is_aggregator, language, title,
        summary, canonical_url, published_at, first_observed_at, source_observed_at)
      select story_id, 'source-3', source_name, false, language, title,
        summary, canonical_url, published_at, published_at, published_at
      from public.canonical_stories where canonical_url like 'https://tie.test/%';
      insert into public.user_story_state(user_id, story_id, read_at)
      select '{owner}'::uuid, max(story_id), now()
      from public.retained_corpus_observations where title = '{title}';
      set role service_role;
      with excluded as (
        select array[max(story_id)]::text[] ids, min(story_id) older
        from public.retained_corpus_observations where title = '{title}'
      ), results as (
        select
          (select coalesce(jsonb_agg(value),'[]'::jsonb)
           from public.m2_retained_candidates_for_owner(
             p_owner_id => '{owner}', p_hide_already_opened => true,
             p_limit => 200, p_excluded_story_ids => excluded.ids) rows(value)) as old_general,
          (select coalesce(jsonb_agg(value),'[]'::jsonb)
           from public.m2_retained_candidates_general_narrow_for_owner(
             p_owner_id => '{owner}', p_hide_already_opened => true,
             p_limit => 200, p_excluded_story_ids => excluded.ids) rows(value)) as new_general,
          (select coalesce(jsonb_agg(value),'[]'::jsonb)
           from public.m2_retained_candidates_for_owner(
             p_owner_id => '{owner}', p_hide_already_opened => true,
             p_lane => 'interested', p_profile_sources => array['source-3']::text[],
             p_min_age_hours => 6, p_limit => 100,
             p_excluded_story_ids => excluded.ids) rows(value)) as old_interested,
          (select coalesce(jsonb_agg(value),'[]'::jsonb)
           from public.m2_retained_candidates_interested_narrow_for_owner(
             p_owner_id => '{owner}', p_hide_already_opened => true,
             p_profile_sources => array['source-3']::text[],
             p_min_age_hours => 6, p_limit => 100,
             p_excluded_story_ids => excluded.ids) rows(value)) as new_interested,
          older from excluded
      ) select old_general = new_general and old_interested = new_interested
          and not exists (select 1 from jsonb_array_elements(new_general) item
                          where item->>'story_id' = older)
          and not exists (select 1 from jsonb_array_elements(new_interested) item
                          where item->>'story_id' = older)
        from results;
      rollback;"""
    assert 't' in _sql(db, script).stdout.splitlines()


def test_interested_dedupe_ignores_young_and_off_profile_twins(db):
    # The old RPC filters age and Interested membership before deduplication.
    # A newer twin outside either boundary must not hide the older lane member.
    owner = '11111111-1111-1111-1111-111111111111'
    script = f"""begin;
      with sample(url, title, source_id, age_minutes) as (values
        ('https://lane.test/older-young', 'Age boundary twin', 'source-3', 362),
        ('https://lane.test/newer-young', 'Age boundary twin', 'source-3', 180),
        ('https://lane.test/older-profile', 'Profile boundary twin', 'source-3', 362),
        ('https://lane.test/newer-profile', 'Profile boundary twin', 'source-4', 361),
        ('https://lane.test/older-category', 'Category boundary twin', 'source-4', 362),
        ('https://lane.test/newer-category', 'Category boundary twin', 'source-5', 361),
        ('https://lane.test/older-cross', 'Cross membership twin', 'source-4', 362),
        ('https://lane.test/newer-cross', 'Cross membership twin', 'source-3', 361)
      )
      insert into public.canonical_stories(
        story_id, canonical_url, title, summary, language, source_kind, source_name,
        published_at)
      select 'story:' || encode(extensions.digest(url, 'sha256'), 'hex'), url,
        title, '', 'en', 'outlet', source_id,
        now() - make_interval(mins => age_minutes) from sample;
      insert into public.retained_corpus_observations(
        story_id, source_id, source_name, source_is_aggregator, language, title,
        summary, canonical_url, published_at, first_observed_at, source_observed_at)
      select story_id, source_name, source_name, false,
        language, title, summary, canonical_url, published_at, published_at, published_at
      from public.canonical_stories where canonical_url like 'https://lane.test/%';
      insert into public.retained_corpus_categories(story_id, category_id)
      select story_id, 'ai' from public.retained_corpus_observations
      where canonical_url in ('https://lane.test/older-category',
                              'https://lane.test/older-cross');
      set role service_role;
      with old_rows as (
        select coalesce(jsonb_agg(value), '[]'::jsonb) rows
        from public.m2_retained_candidates_for_owner(
          p_owner_id => '{owner}', p_hide_already_opened => true,
          p_lane => 'interested', p_profile_sources => array['source-3']::text[],
          p_profile_categories => array['ai']::text[],
          p_min_age_hours => 6, p_limit => 100) items(value)
      ), new_rows as (
        select coalesce(jsonb_agg(value), '[]'::jsonb) rows
        from public.m2_retained_candidates_interested_narrow_for_owner(
          p_owner_id => '{owner}', p_hide_already_opened => true,
          p_profile_sources => array['source-3']::text[],
          p_profile_categories => array['ai']::text[],
          p_min_age_hours => 6, p_limit => 100) items(value)
      )
      select old_rows.rows = new_rows.rows
        and (select count(*) from jsonb_array_elements(new_rows.rows) item
             where item->>'title' in ('Age boundary twin', 'Profile boundary twin',
                                     'Category boundary twin')) = 3
        and exists (select 1 from jsonb_array_elements(new_rows.rows) item
                    where item->>'canonical_url' = 'https://lane.test/newer-cross')
        and not exists (select 1 from jsonb_array_elements(new_rows.rows) item
                        where item->>'canonical_url' = 'https://lane.test/older-cross')
      from old_rows, new_rows;
      rollback;"""
    assert 't' in _sql(db, script).stdout.splitlines()


def test_fixed_width_dedupe_index_accepts_maximum_title(db):
    script = """begin;
      insert into public.canonical_stories(
        story_id, canonical_url, title, summary, language, source_kind, source_name, published_at)
      select 'story:' || encode(extensions.digest('https://long.test/title','sha256'),'hex'),
        'https://long.test/title', repeat('a',8000), '', 'en', 'outlet', 'Long Title', now();
      insert into public.retained_corpus_observations(
        story_id, source_id, source_name, source_is_aggregator, language, title,
        summary, canonical_url, published_at, first_observed_at, source_observed_at)
      select story_id, 'long-title', source_name, false, language, title,
        summary, canonical_url, published_at, published_at, published_at
      from public.canonical_stories where canonical_url='https://long.test/title';
      set role service_role;
      select count(*) > 0 from public.m2_retained_candidates_general_narrow_for_owner(
        '11111111-1111-1111-1111-111111111111',true);
      rollback;"""
    assert 't' in _sql(db, script).stdout.splitlines()


def test_general_pool_accepts_two_hundred_but_refuses_unbounded_reads(db):
    elapsed = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        result = _service(db,
            'select count(*) from public.m2_retained_candidates_v2(p_lane => null, p_limit => 200);')
        elapsed.append((time.perf_counter() - started) * 1000)
        assert _last(result) == '200'
    median = statistics.median(elapsed)
    print(f'bulk general 200 rows: {median:.1f} ms median of {REPEATS}')
    assert median < LANE_BUDGET_MS, f'bulk general read took {median:.1f} ms'
    refused = _service(db,
        'select count(*) from public.m2_retained_candidates_v2(p_lane => null, p_limit => 201);',
        check=False)
    assert refused.returncode != 0 and 'invalid limit' in refused.stderr
    lane_refused = _service(db,
        "select count(*) from public.m2_retained_candidates_v2(p_lane => 'updates', p_limit => 101);",
        check=False)
    assert lane_refused.returncode != 0 and 'invalid limit' in lane_refused.stderr


def test_the_corpus_really_is_at_scale_and_spread_across_the_window(db):
    """A scale test that quietly seeded 6 rows would pass every assertion below
    and prove nothing, so the corpus is asserted before it is measured."""
    rows = int(_last(_sql(db, 'select count(*) from public.retained_corpus_observations;')))
    assert rows == CORPUS_ROWS, rows
    spread = _sql(db, """
      select count(*) filter (where published_at >= now() - interval '6 hours'),
             count(*) filter (where published_at >= now() - interval '24 hours'),
             count(*) filter (where published_at >= now() - interval '48 hours'),
             count(distinct language), count(distinct source_id),
             count(*) filter (where source_is_aggregator)
      from public.retained_corpus_observations;""")
    fresh, day, two_days, languages, sources, aggregators = _last(spread).split('|')
    assert 50 < int(fresh) < 400, f'the updates window holds {fresh} rows'
    assert int(day) > int(fresh) and int(two_days) > int(day), (fresh, day, two_days)
    assert int(languages) == 2 and int(sources) == 12 and int(aggregators) > 0
    covered = int(_last(_sql(db, 'select count(distinct story_id) from public.retained_corpus_coverage;')))
    assert covered > CORPUS_ROWS // 4, f'only {covered} stories carry coverage'
    # The dedupe anti-join needs real collisions to be worth measuring.
    collisions = int(_last(_sql(db, """
      select count(*) from (select public.m2_story_dedupe_key(title) as key, language
                            from public.retained_corpus_observations
                            group by 1, 2 having count(*) > 1) duplicated;""")))
    assert collisions > 50, f'only {collisions} duplicate title groups; the dedupe CTE is untested'


@pytest.mark.parametrize('lane', list(LANE_CALLS))
def test_each_lane_stays_inside_the_latency_budget(db, lane, capsys):
    """Per-lane cost at 7,000 rows, with the full plan printed for the record.

    Before 202609210001 two of these five lanes could not be run on a pull
    request at all: they were tagged SLOW in the parameter id and deselected
    with -k, and carried regression CEILINGS of 30s and 180s instead of a
    budget. They are back on the 1500 ms budget with the other three, which is
    the whole result of that migration stated as a test.
    """
    spec = LANE_CALLS[lane]
    statement = _call_sql(spec)
    samples = _timed(db, statement, repeats=REPEATS)
    median = statistics.median(samples)

    plan = _explain(db, statement)
    elapsed, line = _dominant(plan)
    print(f'\n===== lane {lane} =====')
    print(f'call: {statement}')
    print('timings (ms): ' + ', '.join(f'{value:.1f}' for value in samples))
    print(f'median: {median:.1f} ms   min: {min(samples):.1f} ms   max: {max(samples):.1f} ms')
    print(f'budget: {LANE_BUDGET_MS:.0f} ms')
    print(f'busiest node (rows x loops = {elapsed}): {line}')
    print('--- EXPLAIN (ANALYZE, BUFFERS) of the query inside the function ---')
    print(plan)

    assert 'Seq Scan' in plan or 'Index' in plan or 'Bitmap' in plan, \
        'auto_explain returned no plan; the measurement below is unproven'
    assert median < LANE_BUDGET_MS, (
        f'lane {lane} took {median:.1f} ms at {CORPUS_ROWS} rows, over its '
        f'{LANE_BUDGET_MS:.0f} ms budget. Fix the query, not this number.')


def test_no_lane_carries_the_correlated_dedupe_self_join_any_more(db, capsys):
    """The plan-shape assertion, so a REVERT cannot pass on timing alone.

    A timing budget on a fast laptop can be met by a query that is still
    quadratic, just on a smaller corpus. This reads the plan instead: the
    defect's signature was `CTE Scan on visible peer` running once per row
    under a SubPlan, and it is gone. What replaces it is a WindowAgg.
    """
    print('\n===== plan shape, every lane =====')
    for lane, spec in LANE_CALLS.items():
        plan = _explain(db, _call_sql(spec))
        loops = [int(match) for match in
                 re.findall(r'CTE Scan on \w+ peer[^\n]*loops=([0-9]+)', plan)]
        windowed = 'WindowAgg' in plan
        print(f'  {lane:<14} WindowAgg={windowed}  peer-CTE-scans={loops or "none"}')
        assert not loops, (
            f'lane {lane} still scans the visible CTE per row ({loops}); the '
            'correlated dedupe self-join is back')
        assert windowed, (
            f'lane {lane} has no WindowAgg in its plan, so the lead() dedupe is '
            'not running: check the migration order in MIGRATIONS')


def test_the_dedupe_now_costs_about_what_turning_it_off_costs(db, capsys):
    """Dedupe ON versus the one switch that turns it OFF.

    This test used to assert the opposite: `with_dedupe > without * 10` was the
    proof that the self-join was the whole bill. Inverted now, it is the proof
    that the bill is gone. p_dedupe_window_hours => 0 short-circuits the
    `chosen` filter, so the difference between the two numbers is the sort the
    window function needs and nothing else.
    """
    spec = LANE_CALLS['general-pool']
    arguments = _call_sql(spec).replace(
        f"p_limit => {spec['limit']}", f"p_limit => {spec['limit']}, p_dedupe_window_hours => 0")
    off_samples = _timed(db, arguments, repeats=REPEATS)
    on_samples = _timed(db, _call_sql(spec), repeats=REPEATS)
    # Best of each, not median: this container's timings swing by 3x run to run,
    # and a ratio guard that flakes gets deleted instead of read.
    without, with_dedupe = min(off_samples), min(on_samples)
    print('\n===== where the general pool spends its time =====')
    print(f'  dedupe ON  (36h window, production default) {with_dedupe:10.1f} ms')
    print(f'  dedupe OFF (p_dedupe_window_hours => 0)     {without:10.1f} ms')
    print(f'  the dedupe now adds {with_dedupe - without:+.1f} ms '
          f'({with_dedupe / max(without, 0.001):.2f}x), best of {REPEATS} on each side')
    assert with_dedupe < without * 4 + 100, (
        f'collapsing duplicates costs {with_dedupe:.1f} ms against {without:.1f} ms '
        'with it off. It should be a sort, not a join. Read the plan.')
    assert with_dedupe < LANE_BUDGET_MS, f'{with_dedupe:.1f} ms with the dedupe on'


# The worst case the natural corpus does not contain: one title, thousands of
# times, one language, all inside the dedupe window. 3,000 is deliberately not
# a round fraction of CORPUS_ROWS, and they are 90 seconds apart so EVERY row
# has its successor inside the 36h window and the whole block must collapse to
# exactly one.
# The ceiling for the worst case, which is NOT the per-lane budget: this call
# runs against 10,000 rows rather than 7,000, and the ratio assertion below is
# the real check. Measured 2026-09-21 on an M4, across repeated runs: 285 to
# 970 ms with collapsing on and 250 to 3,140 ms with it off, which says two
# things. The dedupe is no longer the cost, and this fixture's timings are
# NOISY at the hundred-millisecond scale (Docker on a laptop, a 10,000-row scan
# per call). That is why the ratio assertion below compares the BEST of each
# sample rather than the median, and why the ceiling is 3,000 ms rather than
# the per-lane budget.
WORST_CASE_CEILING_MS = 3000.0

IDENTICAL_TITLE_ROWS = 3000
IDENTICAL_TITLE = 'One headline, printed three thousand times'


def _identical_title_prelude():
    return f"""begin;
      insert into public.canonical_stories(
        story_id, canonical_url, title, summary, language, source_kind, source_name, published_at)
      select 'story:' || encode(extensions.digest('https://identical.test/' || i, 'sha256'), 'hex'),
             'https://identical.test/' || i, {_quote(IDENTICAL_TITLE)},
             'Summary body.', 'en', 'outlet', 'Source 5', now() - make_interval(secs => i * 90)
      from generate_series(1, {IDENTICAL_TITLE_ROWS}) as g(i);
      insert into public.retained_corpus_observations(
        story_id, source_id, source_name, source_is_aggregator, language, title, summary,
        canonical_url, published_at, first_observed_at, source_observed_at)
      select s.story_id, 'source-5', 'Source 5', false, s.language, s.title, s.summary,
             s.canonical_url, s.published_at, s.published_at, s.published_at
      from public.canonical_stories s
      where s.canonical_url like 'https://identical.test/%';
      analyze public.retained_corpus_observations;
"""


def test_the_identical_title_worst_case_is_not_quadratic(db, capsys):
    """Three thousand copies of one headline, then the general pool.

    The rows are seeded inside a transaction this test rolls back, so the
    shared corpus is the same afterwards for whatever runs next.

    Two things are asserted, and the second is the one that matters: the call
    stays inside the budget, AND the block still collapses to exactly one row.
    A "fix" that got fast by not deduplicating would pass the first assertion
    and fail the second.
    """
    spec = LANE_CALLS['general-pool']
    # p_limit is the RPC's hard cap of 100 here, not the feed's 75: the point is
    # to prove the whole 3,000-row block collapses, and a 75-row page could hide
    # 25 survivors below the fold.
    statement = _call_sql(spec, limit=100)
    samples = _timed(db, statement, repeats=REPEATS,
                     prelude=_identical_title_prelude(), postlude='rollback;\n')
    median = statistics.median(samples)
    # The same call on the same 10,000 rows with collapsing switched off. This
    # is what separates "the dedupe is quadratic again" from "a 43% bigger
    # corpus costs more to scan", and it is the assertion that survives being
    # run on slower hardware than the laptop these numbers came from.
    without_samples = _timed(
        db, statement.replace('p_limit => 100', 'p_limit => 100, p_dedupe_window_hours => 0'),
        repeats=REPEATS, prelude=_identical_title_prelude(), postlude='rollback;\n')
    without = statistics.median(without_samples)
    best, best_without = min(samples), min(without_samples)

    survivors = _sql(db, _identical_title_prelude()
                     + "set role service_role;"
                     + "set request.jwt.claims = '{\"role\":\"service_role\"}';"
                     + "select count(*) from public.m2_retained_candidates_v2("
                     + "p_category_id => null, p_lane => null, p_limit => 100) as rows(value)"
                     + f" where value->>'title' = {_quote(IDENTICAL_TITLE)};"
                     + "rollback;")
    # Not _last(): the script ends with the ROLLBACK that puts the corpus
    # back, so the count is the last line that is only digits.
    kept = int([line for line in survivors.stdout.splitlines()
                if line.strip().isdigit()][-1])

    print('\n===== worst case: one title, '
          f'{IDENTICAL_TITLE_ROWS} times, all inside the dedupe window =====')
    print('timings (ms): ' + ', '.join(f'{value:.1f}' for value in samples))
    print(f'median: {median:.1f} ms   with the dedupe off: {without:.1f} ms')
    print(f'best of {REPEATS}: {best:.1f} ms   with the dedupe off: {best_without:.1f} ms')
    print(f'the dedupe adds {best - best_without:+.1f} ms '
          f'({best / max(best_without, 0.001):.2f}x) on 3,000 colliding titles, best vs best')
    print(f'copies surviving the dedupe: {kept} (must be exactly 1)')
    assert kept == 1, (
        f'{kept} copies of one headline survived. The dedupe rule changed; '
        'that is a correctness regression, not a performance one.')
    assert best < best_without * 4 + 100, (
        f'{IDENTICAL_TITLE_ROWS} identical titles cost {best:.1f} ms at best '
        f'against {best_without:.1f} ms with collapsing off. A sort does not do '
        'that: the dedupe is quadratic again.')
    assert median < WORST_CASE_CEILING_MS, (
        f'{IDENTICAL_TITLE_ROWS} identical titles took {median:.1f} ms, over the '
        f'{WORST_CASE_CEILING_MS:.0f} ms ceiling.')
