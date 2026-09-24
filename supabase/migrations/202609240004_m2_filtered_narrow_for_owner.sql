begin;

-- Narrow category/search reads without changing lane, dedupe, cursor, owner,
-- or JSON semantics. The deployed owner RPC stays for rollback. Apply before routing.
create or replace function public.m2_retained_candidates_filtered_narrow_for_owner(
  p_owner_id uuid, p_hide_already_opened boolean,
  p_category_id text default null, p_query text default null,
  p_lane text default null,
  p_profile_categories text[] default null, p_profile_sources text[] default null,
  p_trend_window_hours integer default 24, p_trend_min_sources integer default 2,
  p_max_age_hours integer default null, p_min_age_hours integer default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_before_source_count integer default null,
  p_limit integer default 50, p_dedupe_window_hours integer default 36,
  p_excluded_story_ids text[] default null,
  p_suppressed_sources text[] default null, p_suppressed_topics text[] default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare cutoff timestamptz; floor_at timestamptz; trend_cutoff timestamptz;
begin
  -- This service-only function trusts only the backend's authenticated owner binding.
  if p_owner_id is null or p_hide_already_opened is null or not exists (
    select 1 from auth.users where id = p_owner_id
  ) then raise exception 'valid owner and opened policy required' using errcode='42501'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > (case when p_lane is null then 200 else 100 end) then
    raise exception 'invalid limit';
  end if;
  if p_lane is not null and p_lane not in ('updates','hot','interested','surprise') then
    raise exception 'invalid lane';
  end if;
  if coalesce(cardinality(p_excluded_story_ids), 0) > 1200 or
     coalesce(cardinality(p_suppressed_sources), 0) > 1000 or
     coalesce(cardinality(p_suppressed_topics), 0) > 1000 then
    raise exception 'invalid filter size';
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
    -- Keep only fields needed for lane membership, dedupe, and frozen filters.
    -- Expensive category arrays and coverage counts belong after the LIMIT.
    select o.story_id, o.title, o.language, o.published_at, o.source_id,
      o.source_is_aggregator,
      case when p_lane = 'hot' then greatest(
        coalesce((select count(*) from public.retained_corpus_coverage cv
                  where cv.story_id = o.story_id and cv.is_independent
                    and cv.first_seen_at >= trend_cutoff), 0),
        case when o.source_is_aggregator then 0 else 1 end
      )::integer else 0 end as independent_source_count,
      exists (select 1 from public.retained_corpus_categories rc
              where rc.story_id = o.story_id
                and rc.category_id = any(coalesce(p_profile_categories, '{}'::text[])))
        or o.source_id = any(coalesce(p_profile_sources, '{}'::text[])) as matches_profile
    from public.retained_corpus_observations o
    where (p_category_id is null or exists (select 1 from public.retained_corpus_categories
             where story_id = o.story_id and category_id = p_category_id))
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
      and (cutoff is null or o.published_at >= cutoff)
      and (floor_at is null or o.published_at < floor_at)
  ), visible as (
    -- Lane membership must precede deduplication, including Hot coverage.
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
  ), selected as materialized (
    select s.story_id, s.published_at, s.independent_source_count
    from chosen s
    -- Cursor and owner filters follow dedupe. Rejected newer twins cannot
    -- reveal older copies, and opened stories cannot spend the LIMIT budget.
    where (p_lane = 'hot' or p_before_published_at is null or s.published_at < p_before_published_at
           or (s.published_at = p_before_published_at and s.story_id < p_before_story_id))
      and (p_before_source_count is null or
           (s.independent_source_count, s.published_at, s.story_id)
             < (p_before_source_count, p_before_published_at, p_before_story_id))
      and (not p_hide_already_opened or not exists (
        select 1 from public.user_story_state opened
        where opened.user_id = p_owner_id and opened.story_id = s.story_id
          and opened.read_at is not null))
      and s.story_id <> all(coalesce(p_excluded_story_ids, '{}'::text[]))
      and s.source_id <> all(coalesce(p_suppressed_sources, '{}'::text[]))
      and not exists (select 1 from public.retained_corpus_categories rc
                      where rc.story_id = s.story_id
                        and rc.category_id = any(coalesce(p_suppressed_topics, '{}'::text[])))
    order by
      case when p_lane = 'hot' then s.independent_source_count else 0 end desc,
      s.published_at desc, s.story_id desc
    limit p_limit
  )
  select jsonb_build_object('schema_version', 2, 'story_id', o.story_id, 'title', o.title,
    'summary', o.summary, 'language', o.language, 'canonical_url', o.canonical_url,
    'source_id', o.source_id, 'source_name', o.source_name,
    'source_is_aggregator', o.source_is_aggregator,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at,
    'first_ingested_at', o.first_ingested_at, 'last_ingested_at', o.last_ingested_at,
    'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb), 'event_group_id', o.event_group_id,
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'independent_source_count', case when p_lane = 'hot' then x.independent_source_count
      else greatest(coalesce(cv.independent_source_count, 0),
                    case when o.source_is_aggregator then 0 else 1 end)::integer end)
  from selected x
  join public.retained_corpus_observations o on o.story_id = x.story_id
  left join lateral (
    select jsonb_agg(category_id order by category_id) as category_ids
    from public.retained_corpus_categories where story_id = o.story_id
  ) c on true
  left join lateral (
    select count(*) as independent_source_count
    from public.retained_corpus_coverage cv
    where cv.story_id = o.story_id and cv.is_independent
      and cv.first_seen_at >= trend_cutoff
  ) cv on true
  order by
    case when p_lane = 'hot' then x.independent_source_count else 0 end desc,
    x.published_at desc, x.story_id desc;
end;
$$;

revoke all on function public.m2_retained_candidates_filtered_narrow_for_owner(uuid, boolean, text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer, text[], text[], text[])
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates_filtered_narrow_for_owner(uuid, boolean, text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer, integer, integer, text[], text[], text[])
  to service_role;

commit;
