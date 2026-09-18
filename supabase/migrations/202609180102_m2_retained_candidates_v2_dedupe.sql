begin;

-- The lane RPC inherits the dedupe rule from 202609180101.
--
-- 202609180101 fixed "the same story twice in one result" by choosing one
-- representative per visible set inside m2_retained_candidates and the
-- language-exclusive lane. m2_retained_candidates_v2 was added on the Phase 2
-- branch and is not touched by that migration, and the Phase 2 feed reads
-- EXCLUSIVELY through v2. Left alone, the duplicates would come straight back on
-- the surface that actually serves JJ, with the fix visible only on a path
-- nothing calls any more.
--
-- The rule is copied exactly, not reinterpreted, including the part that was got
-- wrong once and is easy to get wrong again: the representative is chosen inside
-- a CTE that has already applied the caller's own filters (category, query, and
-- here the lane's age bounds), and the CURSOR is applied afterwards. Filters
-- first means the winner is always a row the caller can see; cursor last means
-- the choice does not depend on which page was asked for.
--
-- The lane's age bounds count as the caller's filters and therefore belong in
-- the visible set. A representative chosen outside the age window would be
-- invisible to this call, and the story would show zero times instead of once,
-- which is the exact failure 202609180101 documents.
--
-- The Python finalizer (curator/recommendation/finalize.py) also collapses
-- duplicates, by normalized title, canonical URL and event group. That stays:
-- it is the last line of defence and it covers cases SQL cannot see (two rows
-- that reached the window through different lanes). This is the projection
-- rule, which is stronger, because it keeps a duplicate from consuming a
-- candidate slot in the first place.

-- DROP THE OLD SIGNATURE FIRST. `create or replace` with a different parameter
-- list creates a second OVERLOAD rather than replacing anything, and PostgreSQL
-- then refuses every named-argument call as ambiguous ("could not choose a best
-- candidate function"). The first draft of this file dropped it afterwards, and
-- with the type list one short, so both survived and the lane RPC stopped
-- answering at all.
drop function if exists public.m2_retained_candidates_v2(
  text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer);

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
  -- 0 disables collapsing entirely, matching 202609180101, so the rule is
  -- operable without editing this function.
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
  with visible as (
    -- The caller's own filters, and ONLY those. No cursor here.
    select o.* from public.retained_corpus_observations o
    where (p_category_id is null or exists (select 1 from public.retained_corpus_categories
             where story_id = o.story_id and category_id = p_category_id))
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
      and (cutoff is null or o.published_at >= cutoff)
      and (floor_at is null or o.published_at < floor_at)
  ), chosen as (
    select v.* from visible v
    where p_dedupe_window_hours = 0 or public.m2_story_dedupe_key(v.title) = '' or not exists (
      select 1 from visible peer
      where peer.language = v.language
        and public.m2_story_dedupe_key(peer.title) = public.m2_story_dedupe_key(v.title)
        and abs(extract(epoch from (peer.published_at - v.published_at))) <= p_dedupe_window_hours * 3600
        and (peer.published_at, peer.story_id) > (v.published_at, v.story_id))
  ), scored as (
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
    from chosen o
    left join lateral (select jsonb_agg(category_id order by category_id) category_ids
                       from public.retained_corpus_categories where story_id = o.story_id) c on true
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
  from scored s
  -- The CURSOR, applied last, exactly as 202609180101 does it.
  where (p_lane = 'hot' or p_before_published_at is null or s.published_at < p_before_published_at
         or (s.published_at = p_before_published_at and s.story_id < p_before_story_id))
    and (p_lane is distinct from 'hot' or (s.independent_source_count >= p_trend_min_sources
                                           and s.published_at >= trend_cutoff))
    and (p_lane is distinct from 'interested' or s.matches_profile)
    and (p_lane is distinct from 'surprise' or (not s.matches_profile and not s.source_is_aggregator))
    and (p_before_source_count is null or
         (s.independent_source_count, s.published_at, s.story_id)
           < (p_before_source_count, p_before_published_at, p_before_story_id))
  order by
    case when p_lane = 'hot' then s.independent_source_count else 0 end desc,
    s.published_at desc, s.story_id desc
  limit p_limit;
end;
$$;

revoke all on function public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer)
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer)
  to service_role;

commit;
