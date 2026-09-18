begin;

-- M2.1 Phase 2, B1: independent coverage, and the lane-aware candidate RPC.
--
-- "Hot" has to be a COUNT of distinct publishers, not a heuristic. The corpus
-- keeps one row per canonical story, so the coverage signal the deduper already
-- computes (curator/dedup.py merges every outlet that carried the event into
-- coverage_mentions) was being dropped at ingest. This table is where it lands.
--
-- One row per distinct publisher per story. The primary key is what collapses a
-- same-publisher echo: two observations from cnbeta are one coverage row, never
-- two, by construction rather than by a de-duplicating query.
create table public.retained_corpus_coverage (
  story_id text not null references public.retained_corpus_observations(story_id) on delete restrict,
  publisher_id text not null check (publisher_id <> '' and octet_length(publisher_id) <= 512),
  -- Computed at ingest from the route flags that already exist in config
  -- (aggregator / echo_eligible), never guessed at query time. A story carried
  -- by ten aggregator echoes and zero publishers is not hot.
  is_independent boolean not null,
  first_seen_at timestamptz not null,
  primary key (story_id, publisher_id)
);
create index retained_corpus_coverage_independent_idx
  on public.retained_corpus_coverage(story_id, first_seen_at desc) where is_independent;

alter table public.retained_corpus_coverage enable row level security;
alter table public.retained_corpus_coverage force row level security;
revoke all on public.retained_corpus_coverage from public, anon, authenticated;
grant select, insert, update, delete on public.retained_corpus_coverage to service_role;

create or replace function public.m2_ingest_retained_coverage(p_rows jsonb)
returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare row jsonb; touched text; written integer := 0;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) > 60000 then
    raise exception 'invalid coverage rows';
  end if;
  for row in select value from jsonb_array_elements(p_rows) loop
    if jsonb_typeof(row) <> 'object'
       or not (row ?& array['story_id','publisher_id','is_independent','first_seen_at'])
       or exists (select 1 from jsonb_object_keys(row) key
                  where key not in ('story_id','publisher_id','is_independent','first_seen_at'))
       or jsonb_typeof(row->'is_independent') <> 'boolean'
       or coalesce(row->>'publisher_id','') = '' then
      raise exception 'invalid coverage row';
    end if;
    -- A story the corpus does not carry is SKIPPED, never inserted: the corpus
    -- write is the only thing allowed to create a story row.
    insert into public.retained_corpus_coverage(story_id, publisher_id, is_independent, first_seen_at)
      select row->>'story_id', row->>'publisher_id', (row->>'is_independent')::boolean,
             (row->>'first_seen_at')::timestamptz
      where exists (select 1 from public.retained_corpus_observations o where o.story_id = row->>'story_id')
    on conflict (story_id, publisher_id) do update set
      is_independent = excluded.is_independent,
      -- First seen is the earliest sighting, so a later re-observation cannot
      -- push a story back into the trend window it had already left.
      first_seen_at = least(public.retained_corpus_coverage.first_seen_at, excluded.first_seen_at)
      where public.retained_corpus_coverage.is_independent is distinct from excluded.is_independent
         or public.retained_corpus_coverage.first_seen_at > excluded.first_seen_at
    returning story_id into touched;
    if touched is not null then written := written + 1; end if;
  end loop;
  return written;
end;
$$;

-- The lane-aware candidate RPC. m2_retained_candidates stays in place untouched,
-- so the rollback ACL drill (scripts/verify_m2_rollback_acl.py) stays valid and a
-- rollback is a config change rather than a migration.
--
-- One lane per call. The service asks for each lane and merges, because the four
-- pools order by four different things and a single "newest 50" window can only
-- ever express one of them (which is the bug this replaces).
create or replace function public.m2_retained_candidates_v2(
  p_category_id text default null, p_query text default null,
  p_lane text default null,
  p_profile_categories text[] default null, p_profile_sources text[] default null,
  p_trend_window_hours integer default 24, p_trend_min_sources integer default 2,
  p_max_age_hours integer default null, p_min_age_hours integer default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50
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
  cutoff := case when p_max_age_hours is null then null
                 else now() - make_interval(hours => p_max_age_hours) end;
  -- The age FLOOR is how a lane avoids spending its fetch budget on rows a
  -- higher-priority lane will claim anyway. A story fresh enough to be "fresh"
  -- is fresh, whatever else it also qualifies for, so the other three lanes ask
  -- for stories older than the freshness window.
  floor_at := case when p_min_age_hours is null then null
                   else now() - make_interval(hours => p_min_age_hours) end;
  trend_cutoff := now() - make_interval(hours => p_trend_window_hours);
  return query
  with scored as (
    select o.*,
      coalesce(c.category_ids, '[]'::jsonb) as category_ids,
      -- A story's own publisher IS one independent source. The coverage table
      -- adds the others. Without this floor a story nobody else carried would
      -- read as zero sources and could never clear an exploration gate.
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
      and (p_before_published_at is null or o.published_at < p_before_published_at
           or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
      and (cutoff is null or o.published_at >= cutoff)
      and (floor_at is null or o.published_at < floor_at)
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
  where (p_lane is distinct from 'hot' or (s.independent_source_count >= p_trend_min_sources
                                           and s.published_at >= trend_cutoff))
    and (p_lane is distinct from 'interested' or s.matches_profile)
    and (p_lane is distinct from 'surprise' or (not s.matches_profile and not s.source_is_aggregator))
  order by
    case when p_lane = 'hot' then s.independent_source_count else 0 end desc,
    s.published_at desc, s.story_id desc
  limit p_limit;
end;
$$;

revoke all on function public.m2_ingest_retained_coverage(jsonb),
  public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer)
  from public, anon, authenticated;
grant execute on function public.m2_ingest_retained_coverage(jsonb),
  public.m2_retained_candidates_v2(text, text, text, text[], text[], integer, integer, integer, integer, timestamptz, text, integer)
  to service_role;

commit;
