"""What m2_retained_candidates_v2 COSTS at real corpus scale, on PostgreSQL 17.11.

Production POST /rank answered 503 "Supabase request failed" after about 4.8s
against a 3.0s client timeout, and the review of PR #51 flagged this RPC as
"cost grows with corpus size, unmeasured". Unmeasured is the part this file
fixes. Every other Phase 2 test seeds a handful of rows, so all of them would
stay green while the one query the feed cannot live without walked off a cliff.

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

HOW TO RUN IT

    # as CI runs it: the three healthy lanes, about 10 seconds
    pytest tests/test_m2_candidate_scale_postgres_runtime.py -k "not SLOW" -s

    # everything, including the two lanes that are currently pathological
    pytest tests/test_m2_candidate_scale_postgres_runtime.py -s

The SLOW half is deselected in CI by name rather than skipped, because the
postgres job in .github/workflows/ci.yml treats a skipped test as a failure.
general-pool alone is about 102 seconds per call; putting that on every PR buys
nothing the tripwire test does not already buy in milliseconds.

WHAT THIS FILE DOES NOT DO: it does not fix the query. It measures it, names
the cause, and pins it so it cannot get worse unnoticed. The fix (a window
function over a sorted set, or a stored normalized dedupe-key column with an
index, in place of the correlated NOT EXISTS against a CTE) changes the
projection contract and is a decision, not a cleanup.

Container fixture, MIGRATIONS ordering and the _sql / _service helpers are
lifted from tests/test_m2_phase2_postgres_runtime.py unchanged.
"""
from __future__ import annotations

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
)

# Seeded corpus size. The live retained corpus was about 6,500 rows when the
# 503 was investigated on 2026-09-21; 7,000 is that plus headroom.
CORPUS_ROWS = 7000

# The ten category ids topics.yaml actually ships. A synthetic 'cat1'..'catN'
# set would give the planner a different selectivity than production has.
CATEGORY_IDS = ('ai', 'crypto', 'quantum', 'energy', 'space', 'biotech',
                'world', 'us-news', 'business', 'trending')

# THE MEASUREMENT, taken 2026-09-21 on this exact fixture (7,000 rows,
# postgres:17.11 in Docker on an Apple M4, three repeats, median):
#
#     lane            median      what the 3.0s client timeout does with it
#     --------------  ----------  ----------------------------------------
#     updates             72 ms   fits
#     hot                111 ms   fits
#     surprise           568 ms   fits
#     interested      15,481 ms   blows it by 5x
#     general-pool   102,241 ms   blows it by 34x
#
# The asked-for budget was 1500 ms per lane. Three lanes meet it and keep it.
# Two do not, and NOT because the budget is wrong: `general-pool` applies no
# age bound at all, so its `visible` CTE holds the whole 7,000-row corpus, and
# the `chosen` CTE then self-joins that CTE against itself once per row. The
# plan node is `CTE Scan on visible peer` under `SubPlan 7`, 7,000 loops of a
# 7,000-row scan, about 49 million comparisons, each one calling
# m2_story_dedupe_key twice. No index fixes this: a CTE has no indexes, and the
# NOT EXISTS is correlated so the planner cannot turn it into a hash anti-join.
# The fix is a different dedupe rule (a window function over a sorted set, or a
# stored normalized key column), which is a migration nobody has written yet.
#
# So the two broken lanes carry a REGRESSION CEILING rather than a budget: a
# number set from the measurement above with headroom, whose only job is to
# catch this getting worse. It is a record of a known defect, not a latency
# anyone accepted. test_the_broken_lanes_still_cannot_fit_the_client_timeout is
# the tripwire that fires when someone fixes them, so these ceilings cannot
# quietly outlive the bug.
LANE_BUDGET_MS = 1500.0
LANE_BUDGET_OVERRIDES_MS = {
    'interested': 30_000.0,    # measured 15,481 ms; ceiling is about 2x
    'general-pool': 180_000.0,  # measured 102,241 ms; ceiling is about 1.8x
}

# What the HTTP client allows for the WHOLE request. Named here because the
# budgets above only mean something against it.
CLIENT_TIMEOUT_MS = 3000.0


def _budget_ms(lane):
    return LANE_BUDGET_OVERRIDES_MS.get(lane, LANE_BUDGET_MS)


# Repeats per lane. The first execution of a plpgsql function in a session pays
# for parse and plan; the median of several runs is the steady-state cost the
# feed actually pays on a warm connection pool. The two broken lanes get ONE
# run, because three of general-pool is five minutes of CI on its own.
REPEATS = 3


def _repeats(lane):
    return 1 if lane in LANE_BUDGET_OVERRIDES_MS else REPEATS


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


def _timed(container, statement, repeats=REPEATS):
    """Median server-side wall clock, from psql's own \\timing.

    Measured inside the container so docker-exec startup is not counted as
    query cost. The SET statements run before \\timing is on, so every number
    parsed out belongs to the RPC call itself.
    """
    script = ("set role service_role;\n"
              "set request.jwt.claims = '{\"role\":\"service_role\"}';\n"
              "\\timing on\n" + (statement + "\n") * repeats)
    result = _sql(container, script, timeout=900)
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
              # 50 ms, not 0. m2_story_dedupe_key carries `set search_path`, which
              # blocks SQL-function inlining, so at log_min_duration=0 auto_explain
              # emits one plan per CALL: 38 million log lines for the general pool.
              # 50 ms keeps the one plan that matters and drops the noise.
              "set auto_explain.log_min_duration = 50;\n"
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


SLOW_SUFFIX = 'SLOW'


@pytest.mark.parametrize('lane', list(LANE_CALLS), ids=[
    # The two broken lanes are tagged SLOW in their parameter id, and CI runs
    # this file with -k "not SLOW". DESELECTED, never skipped: .github's
    # postgres job treats a skipped test as a failure, and rightly.
    #
    #     on demand:  pytest tests/test_m2_candidate_scale_postgres_runtime.py
    #     as CI runs: pytest ... -k "not SLOW"
    #
    # general-pool alone is 102 seconds a run. Putting that on every PR buys
    # nothing the tripwire below does not already buy for free.
    (lane + '-' + SLOW_SUFFIX if lane in LANE_BUDGET_OVERRIDES_MS else lane)
    for lane in LANE_CALLS])
def test_each_lane_stays_inside_the_latency_budget(db, lane, capsys):
    """Per-lane cost at 7,000 rows, with the full plan printed for the record.

    The budget is 1500 ms per lane. Two things it is NOT: it is not a
    performance target anybody chose, and it is not the production timeout. It
    is the number PR #51's reviewers asked to have proven, and the feed issues
    five of these calls per /rank, so a lane at the budget already means a
    request that cannot fit the client's timeout. Read a PASS here as "no lane
    is pathological at this scale", not as "the endpoint is fast enough".

    If this ever fails, do NOT raise the number. The query is doing work the
    lane's own filters should have avoided, and the plan below says which.
    """
    spec = LANE_CALLS[lane]
    budget = _budget_ms(lane)
    statement = _call_sql(spec)
    samples = _timed(db, statement, repeats=_repeats(lane))
    median = statistics.median(samples)

    plan = _explain(db, statement)
    elapsed, line = _dominant(plan)
    print(f'\n===== lane {lane} =====')
    print(f'call: {statement}')
    print('timings (ms): ' + ', '.join(f'{value:.1f}' for value in samples))
    print(f'median: {median:.1f} ms   min: {min(samples):.1f} ms   max: {max(samples):.1f} ms')
    print(f'budget: {budget:.0f} ms' + ('  (REGRESSION CEILING for a known defect, not an accepted latency)'
                                       if lane in LANE_BUDGET_OVERRIDES_MS else ''))
    print(f'busiest node (rows x loops = {elapsed}): {line}')
    print('--- EXPLAIN (ANALYZE, BUFFERS) of the query inside the function ---')
    print(plan)

    assert 'Seq Scan' in plan or 'Index' in plan or 'Bitmap' in plan, \
        'auto_explain returned no plan; the measurement below is unproven'
    assert median < budget, (
        f'lane {lane} took {median:.1f} ms at {CORPUS_ROWS} rows, over its '
        f'{budget:.0f} ms ceiling. Fix the query, not this number.')


def test_SLOW_the_broken_lanes_still_cannot_fit_the_client_timeout(db, capsys):
    """The tripwire on the two ceilings above.

    A ceiling set from a measurement of a defect has one failure mode: the
    defect gets fixed and the ceiling stays, silently permitting a latency
    nobody would accept again. This test asserts the defect is STILL THERE. It
    goes red the day someone makes the general pool fast, and the message says
    to delete the ceilings and put both lanes back on the 1500 ms budget.
    """
    print('\n===== the two lanes that do not fit the 3.0s client timeout =====')
    for lane in LANE_BUDGET_OVERRIDES_MS:
        median = statistics.median(_timed(db, _call_sql(LANE_CALLS[lane]), repeats=1))
        print(f'  {lane:<14} {median:10.1f} ms   vs a {CLIENT_TIMEOUT_MS:.0f} ms client timeout')
        assert median > CLIENT_TIMEOUT_MS, (
            f'lane {lane} now answers in {median:.1f} ms, inside the client timeout. '
            f'Good. Delete its entry from LANE_BUDGET_OVERRIDES_MS, delete this test, '
            f'and let it run on the {LANE_BUDGET_MS:.0f} ms budget with the others.')


def test_SLOW_the_cost_is_the_dedupe_self_join_and_not_the_row_fetch(db, capsys):
    """Names the cause, so the next person does not reach for an index.

    Same lane, same rows, dedupe window set to 0, which is the one documented
    switch that turns the `chosen` CTE's self-join off (202609180102 keeps 0
    operable on purpose). If the cost were the corpus scan, the coverage
    subquery or the category lateral, this would barely move. It collapses,
    which is the proof that the self-join is the whole bill and that an index
    on the base tables cannot help: a CTE has no indexes to scan.
    """
    spec = LANE_CALLS['general-pool']
    arguments = _call_sql(spec).replace(
        f"p_limit => {spec['limit']}", f"p_limit => {spec['limit']}, p_dedupe_window_hours => 0")
    without = statistics.median(_timed(db, arguments, repeats=REPEATS))
    with_dedupe = statistics.median(_timed(db, _call_sql(spec), repeats=1))
    print('\n===== where the general pool spends its time =====')
    print(f'  dedupe ON  (36h window, production default) {with_dedupe:10.1f} ms')
    print(f'  dedupe OFF (p_dedupe_window_hours => 0)     {without:10.1f} ms')
    print(f'  the self-join is {with_dedupe / max(without, 0.001):.0f}x the rest of the query')
    assert without < LANE_BUDGET_MS, (
        f'with the self-join off the same lane still takes {without:.1f} ms, so the '
        'cost is NOT only the dedupe and this diagnosis is wrong')
    assert with_dedupe > without * 10, (
        f'the self-join is only {with_dedupe / max(without, 0.001):.1f}x the rest; '
        're-read the plan before blaming it')
