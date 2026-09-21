begin;

-- Production POST /rank timed out on every call. The lane RPC's dedupe was
-- quadratic, and the corpus grew past the point where that is survivable.
--
-- Measured on 2026-09-21 against a 7,000-row corpus (live is about 6.5K), each
-- lane called with the arguments curator/recommendation/service.py `_pool_rows`
-- actually sends (tests/test_m2_candidate_scale_postgres_runtime.py):
--
--     updates          72 ms
--     hot             111 ms
--     surprise        568 ms
--     interested   15,481 ms     5x over the 3.0s client timeout
--     general pool 102,241 ms    34x over
--
-- The same general-pool call with p_dedupe_window_hours => 0, which is the one
-- documented switch that turns collapsing off, runs in 117 ms. That is the
-- proof: the entire bill is the `chosen` CTE.
--
-- 202609180101 and 202609180102 wrote the dedupe as a correlated NOT EXISTS
-- against the `visible` CTE. A CTE has no indexes and the subquery is
-- correlated, so the planner cannot turn it into a hash anti-join: it runs
-- `CTE Scan on visible peer` once per row. At 7,000 rows that is about 49
-- million comparisons, each evaluating public.m2_story_dedupe_key TWICE (once
-- per side), and the function carries `set search_path`, which blocks SQL
-- inlining, so every one of those is a real function call.
--
-- THE RULE DOES NOT CHANGE. It is reproduced exactly, not approximated.
--
-- The old predicate drops row v when some peer exists with the same language
-- and the same folded title key, ranked strictly above it by
-- (published_at, story_id), and published within p_dedupe_window_hours of it.
-- Because the peer is ranked ABOVE v, `abs(peer.published_at - v.published_at)`
-- is just `peer.published_at - v.published_at`, which is >= 0. So v is dropped
-- exactly when the SMALLEST published_at among the peers ranked above it is
-- within the window. That peer is, by definition, v's immediate successor in
-- ascending (published_at, story_id) order inside its own (language, key)
-- group, which is what `lead()` returns. One sort, one pass, no self-join, and
-- the key is computed ONCE per row instead of twice per comparison.
--
-- Same rule, same representative, same everything, including the parts that are
-- easy to lose:
--
--   * the representative is still chosen inside a set that already has the
--     caller's filters AND the lane predicate applied, so the winner is always
--     a row this call can see (the bug 202609180101 documents at length);
--   * the CURSOR is still applied afterwards, so the choice does not depend on
--     which page was asked for;
--   * p_dedupe_window_hours => 0 still disables collapsing entirely;
--   * a row whose folded key is empty is still never collapsed;
--   * chains still behave the way the old rule made them behave. Three rows A
--     < B < C where B is within the window of C and A is within the window of B
--     but NOT of C: the old predicate dropped A (B outranks it and is close
--     enough) and dropped B (C outranks it), keeping only C. `lead()` gives A
--     the successor B, which is inside the window, so A is dropped; B's
--     successor is C, inside the window, so B is dropped. Identical. A
--     naive `row_number() = 1` per (language, key) would ALSO keep only C here,
--     but it would differ the moment a group spans more than the window: it
--     would collapse a twin the old rule deliberately kept, because a story
--     republished three days later under an identical headline is not the same
--     appearance. That is why this uses lead() and not row_number().
--
-- SEMANTIC DIFFERENCES FROM 202609180101 / 202609180102: none. The predicate is
-- logically equivalent, and tests/test_m2_candidate_dedupe_postgres_runtime.py
-- plus the scale file's identical-title worst case pin it from both sides.
--
-- NO functional index and NO stored generated column. Both were considered and
-- neither is needed: the key is now evaluated once per visible row (7,000
-- calls, about 12 ms of the general pool's total), and the sort the window
-- needs is on a CTE the planner cannot index-scan anyway. A generated column
-- would cost a full table rewrite on a live table plus a write on every hourly
-- ingest, to buy back milliseconds on the read path. Revisit only with an
-- EXPLAIN ANALYZE that shows the key evaluation, not the sort, dominating.

-- All three functions keep their EXACT current signatures. `create or replace`
-- with the same parameter list replaces in place; with a different one it
-- creates a second OVERLOAD and PostgREST then rejects every named-argument
-- call as ambiguous. Nothing below changes an argument list, so there is
-- deliberately no `drop function` here.

create or replace function public.m2_retained_candidates(
  p_category_id text default null, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50, p_dedupe_window_hours integer default 36
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  return query
  with visible as (
    -- The caller's own filters, and ONLY those. The cursor is deliberately not
    -- here: the representative must not depend on the page being asked for.
    select o.* from public.retained_corpus_observations o
    where (p_category_id is null or exists (select 1 from public.retained_corpus_categories where story_id=o.story_id and category_id=p_category_id))
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
  ), keyed as (
    -- The folded key ONCE per row, never once per comparison.
    select v.*, public.m2_story_dedupe_key(v.title) as dedupe_key from visible v
  ), ranked as (
    -- The nearest twin ranked ABOVE this row, which is the only peer that can
    -- suppress it. Rows with an empty key share a partition here and are
    -- exempted in `chosen`, exactly as the old predicate exempted them.
    select k.*, lead(k.published_at) over (
      partition by k.language, k.dedupe_key order by k.published_at, k.story_id) as next_twin_at
    from keyed k
  ), chosen as (
    select r.* from ranked r
    where p_dedupe_window_hours = 0 or r.dedupe_key = '' or r.next_twin_at is null
      or r.next_twin_at > r.published_at + make_interval(secs => p_dedupe_window_hours * 3600)
  )
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at, 'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb),
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'event_group_id', o.event_group_id)
  from chosen o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids from public.retained_corpus_categories where story_id = o.story_id) c on true
  where (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

create or replace function public.m2_retained_candidates_language_exclusive(
  p_display_language text, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50, p_policy_id text default null,
  p_dedupe_window_hours integer default 36
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if p_display_language is null or p_display_language not in ('en', 'zh') then raise exception 'invalid display language'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if p_policy_id is null or p_policy_id !~ '^[A-Za-z0-9._-]{1,64}$' then
    raise exception 'invalid policy id';
  end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  return query
  with visible as (
    -- Everything that makes a row part of THIS lane: the model's exclusivity
    -- decision under the current policy, the language, and the group check.
    -- A row outside this set must never be able to suppress a row inside it.
    select o.* from public.retained_corpus_observations o
    join translation_private.exclusivity_decisions d
      on d.story_id = o.story_id
     and d.display_language = p_display_language
     and d.outcome = 'exclusive'
     and d.policy_id = p_policy_id
    where o.language <> p_display_language
      and not exists (
        select 1 from public.retained_corpus_observations peer
        where o.event_group_id is not null and peer.event_group_id = o.event_group_id
          and peer.language = p_display_language)
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
  ), keyed as (
    select v.*, public.m2_story_dedupe_key(v.title) as dedupe_key from visible v
  ), ranked as (
    select k.*, lead(k.published_at) over (
      partition by k.language, k.dedupe_key order by k.published_at, k.story_id) as next_twin_at
    from keyed k
  ), chosen as (
    select r.* from ranked r
    where p_dedupe_window_hours = 0 or r.dedupe_key = '' or r.next_twin_at is null
      or r.next_twin_at > r.published_at + make_interval(secs => p_dedupe_window_hours * 3600)
  )
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at,
    'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb),
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'event_group_id', o.event_group_id)
  from chosen o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids
                     from public.retained_corpus_categories where story_id = o.story_id) c on true
  where (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

create or replace function public.m2_retained_candidates_v2(
  p_category_id text default null, p_query text default null,
  p_lane text default null,
  p_profile_categories text[] default null, p_profile_sources text[] default null,
  p_trend_window_hours integer default 24, p_trend_min_sources integer default 2,
  p_max_age_hours integer default null, p_min_age_hours integer default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_before_source_count integer default null,
  p_limit integer default 50, p_dedupe_window_hours integer default 36
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare cutoff timestamptz; floor_at timestamptz; trend_cutoff timestamptz;
begin
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if p_lane is not null and p_lane not in ('updates','hot','interested','surprise') then
    raise exception 'invalid lane';
  end if;
  if p_trend_window_hours is null or p_trend_window_hours < 1 or p_trend_window_hours > 72 then
    raise exception 'invalid trend window';
  end if;
  if p_trend_min_sources is null or p_trend_min_sources < 1 or p_trend_min_sources > 10 then
    raise exception 'invalid trend threshold';
  end if;
  if p_max_age_hours is not null and (p_max_age_hours < 1 or p_max_age_hours > 168) then
    raise exception 'invalid age bound';
  end if;
  if p_min_age_hours is not null and (p_min_age_hours < 1 or p_min_age_hours > 168) then
    raise exception 'invalid age floor';
  end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  if p_before_source_count is not null and (p_lane is distinct from 'hot'
       or p_before_published_at is null or p_before_story_id is null) then
    raise exception 'invalid cursor';
  end if;
  if p_lane = 'hot' and p_before_published_at is not null and p_before_source_count is null then
    raise exception 'invalid cursor';
  end if;
  cutoff := case when p_max_age_hours is null then null
                 else now() - make_interval(hours => p_max_age_hours) end;
  floor_at := case when p_min_age_hours is null then null
                   else now() - make_interval(hours => p_min_age_hours) end;
  trend_cutoff := now() - make_interval(hours => p_trend_window_hours);
  return query
  with eligible as (
    -- The caller's own filters, plus the two values lane membership is decided
    -- from. No lane predicate yet, no cursor, no dedupe.
    select o.*,
      coalesce(c.category_ids, '[]'::jsonb) as category_ids,
      greatest(
        coalesce((select count(*) from public.retained_corpus_coverage cv
                  where cv.story_id = o.story_id and cv.is_independent
                    and cv.first_seen_at >= trend_cutoff), 0),
        case when o.source_is_aggregator then 0 else 1 end
      )::integer as independent_source_count,
      exists (select 1 from public.retained_corpus_categories rc
              where rc.story_id = o.story_id
                and rc.category_id = any(coalesce(p_profile_categories, '{}'::text[])))
        or o.source_id = any(coalesce(p_profile_sources, '{}'::text[])) as matches_profile
    from public.retained_corpus_observations o
    left join lateral (select jsonb_agg(category_id order by category_id) category_ids
                       from public.retained_corpus_categories where story_id = o.story_id) c on true
    where (p_category_id is null or exists (select 1 from public.retained_corpus_categories
             where story_id = o.story_id and category_id = p_category_id))
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
      and (cutoff is null or o.published_at >= cutoff)
      and (floor_at is null or o.published_at < floor_at)
  ), visible as (
    -- LANE MEMBERSHIP BELONGS HERE, BEFORE THE DEDUPE. With the lane applied
    -- afterwards, a newer twin could suppress an older one that qualified for
    -- hot, for-you or surprise and then be filtered out itself, and the lane
    -- would return ZERO copies of a story that had a perfectly good one.
    select e.* from eligible e
    where (p_lane is distinct from 'hot' or (e.independent_source_count >= p_trend_min_sources
                                             and e.published_at >= trend_cutoff))
      and (p_lane is distinct from 'interested' or e.matches_profile)
      and (p_lane is distinct from 'surprise' or (not e.matches_profile and not e.source_is_aggregator))
  ), keyed as (
    select v.*, public.m2_story_dedupe_key(v.title) as dedupe_key from visible v
  ), ranked as (
    select k.*, lead(k.published_at) over (
      partition by k.language, k.dedupe_key order by k.published_at, k.story_id) as next_twin_at
    from keyed k
  ), chosen as (
    select r.* from ranked r
    where p_dedupe_window_hours = 0 or r.dedupe_key = '' or r.next_twin_at is null
      or r.next_twin_at > r.published_at + make_interval(secs => p_dedupe_window_hours * 3600)
  )
  select jsonb_build_object('schema_version', 2, 'story_id', s.story_id, 'title', s.title,
    'summary', s.summary, 'language', s.language, 'canonical_url', s.canonical_url,
    'source_id', s.source_id, 'source_name', s.source_name,
    'source_is_aggregator', s.source_is_aggregator,
    'published_at', s.published_at, 'source_observed_at', s.source_observed_at,
    'first_ingested_at', s.first_ingested_at, 'last_ingested_at', s.last_ingested_at,
    'first_ready_at', s.first_ready_at, 'last_ready_at', s.last_ready_at,
    'category_ids', s.category_ids, 'event_group_id', s.event_group_id,
    'title_translations', s.title_translations, 'summary_translations', s.summary_translations,
    'independent_source_count', s.independent_source_count)
  from chosen s
  -- The CURSOR, and only the cursor, applied last.
  where (p_lane = 'hot' or p_before_published_at is null or s.published_at < p_before_published_at
         or (s.published_at = p_before_published_at and s.story_id < p_before_story_id))
    and (p_before_source_count is null or
         (s.independent_source_count, s.published_at, s.story_id)
           < (p_before_source_count, p_before_published_at, p_before_story_id))
  order by
    case when p_lane = 'hot' then s.independent_source_count else 0 end desc,
    s.published_at desc, s.story_id desc
  limit p_limit;
end;
$$;

-- Grants are re-stated because `create or replace` on an existing function
-- keeps its ACL, but a fresh database built by replaying migrations in order
-- has only whatever the last statement said. Idempotent either way.
revoke all on function public.m2_retained_candidates(text, text, timestamptz, text, integer, integer)
  from public, anon, authenticated;
revoke all on function public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text, integer)
  from public, anon, authenticated;
revoke all on function public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer)
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates(text, text, timestamptz, text, integer, integer),
  public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text, integer),
  public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer)
  to service_role;

commit;
