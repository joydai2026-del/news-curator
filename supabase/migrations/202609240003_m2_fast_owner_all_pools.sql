begin;

-- A peer lookup can stop at the first newer twin while the outer scan follows
-- retained_corpus_fresh_idx. The older window query sorted the whole retained
-- corpus on every page even though the caller needs only 75 older stories.
create index retained_corpus_dedupe_peer_idx on public.retained_corpus_observations
  (language, md5(public.m2_story_dedupe_key(title)), published_at, story_id);

-- Optimize only the All/general path. Keep the owner-filtered RPC callable
-- for application rollback. Owner opened state is filtered after dedupe and
-- before LIMIT, exactly as in the existing owner RPC.
create or replace function public.m2_retained_candidates_general_narrow_for_owner(
  p_owner_id uuid, p_hide_already_opened boolean,
  p_trend_window_hours integer default 24,
  p_before_published_at timestamptz default null,
  p_before_story_id text default null,
  p_limit integer default 50,
  p_dedupe_window_hours integer default 36,
  p_excluded_story_ids text[] default null,
  p_suppressed_sources text[] default null,
  p_suppressed_topics text[] default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare trend_cutoff timestamptz;
begin
  if p_owner_id is null or p_hide_already_opened is null or not exists (
    select 1 from auth.users where id = p_owner_id
  ) then raise exception 'valid owner and opened policy required' using errcode='42501'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then
    raise exception 'invalid cursor';
  end if;
  if p_limit is null or p_limit < 1 or p_limit > 200 then
    raise exception 'invalid limit';
  end if;
  if p_trend_window_hours is null or p_trend_window_hours < 1 or p_trend_window_hours > 72 then
    raise exception 'invalid trend window';
  end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  if coalesce(cardinality(p_excluded_story_ids), 0) > 1200 or
     coalesce(cardinality(p_suppressed_sources), 0) > 1000 or
     coalesce(cardinality(p_suppressed_topics), 0) > 1000 then
    raise exception 'invalid filter size';
  end if;
  trend_cutoff := now() - make_interval(hours => p_trend_window_hours);
  return query
  with selected as materialized (
    select o.story_id, o.published_at
    from public.retained_corpus_observations o
    where (p_before_published_at is null or o.published_at < p_before_published_at
           or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
      and o.story_id <> all(coalesce(p_excluded_story_ids, '{}'::text[]))
      and (not p_hide_already_opened or not exists (
        select 1 from public.user_story_state opened
        where opened.user_id = p_owner_id and opened.story_id = o.story_id
          and opened.read_at is not null))
      and o.source_id <> all(coalesce(p_suppressed_sources, '{}'::text[]))
      and not exists (
        select 1 from public.retained_corpus_categories rc
        where rc.story_id = o.story_id
          and rc.category_id = any(coalesce(p_suppressed_topics, '{}'::text[])))
      -- Deduplication precedes owner filters: an opened or excluded newer
      -- twin must still suppress the older copy, exactly as in the old RPC.
      and (p_dedupe_window_hours = 0
           or public.m2_story_dedupe_key(o.title) = ''
           or not exists (
             select 1 from public.retained_corpus_observations peer
             where peer.language = o.language
               and md5(public.m2_story_dedupe_key(peer.title)) = md5(public.m2_story_dedupe_key(o.title))
               -- The fixed-width index supports titles up to the schema's 8 KB
               -- limit; exact comparison prevents even a hash collision from
               -- changing which story survives deduplication.
               and public.m2_story_dedupe_key(peer.title) = public.m2_story_dedupe_key(o.title)
               and (peer.published_at, peer.story_id) > (o.published_at, o.story_id)
               and peer.published_at <= o.published_at + make_interval(hours => p_dedupe_window_hours)
           ))
    order by o.published_at desc, o.story_id desc
    limit p_limit
  )
  select jsonb_build_object('schema_version', 2, 'story_id', s.story_id, 'title', s.title,
    'summary', s.summary, 'language', s.language, 'canonical_url', s.canonical_url,
    'source_id', s.source_id, 'source_name', s.source_name,
    'source_is_aggregator', s.source_is_aggregator,
    'published_at', s.published_at, 'source_observed_at', s.source_observed_at,
    'first_ingested_at', s.first_ingested_at, 'last_ingested_at', s.last_ingested_at,
    'first_ready_at', s.first_ready_at, 'last_ready_at', s.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb), 'event_group_id', s.event_group_id,
    'title_translations', s.title_translations, 'summary_translations', s.summary_translations,
    'independent_source_count', greatest(coalesce(cv.independent_source_count, 0),
      case when s.source_is_aggregator then 0 else 1 end)::integer)
  from selected x
  join public.retained_corpus_observations s on s.story_id = x.story_id
  left join lateral (
    select jsonb_agg(category_id order by category_id) as category_ids
    from public.retained_corpus_categories where story_id = s.story_id
  ) c on true
  left join lateral (
    select count(*) as independent_source_count
    from public.retained_corpus_coverage cv
    where cv.story_id = s.story_id and cv.is_independent
      and cv.first_seen_at >= trend_cutoff
  ) cv on true
  order by x.published_at desc, x.story_id desc;
end;
$$;

revoke all on function public.m2_retained_candidates_general_narrow_for_owner(uuid, boolean,
  integer, timestamptz, text, integer, integer, text[], text[], text[])
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates_general_narrow_for_owner(uuid, boolean,
  integer, timestamptz, text, integer, integer, text[], text[], text[])
  to service_role;


-- All/Interested has the same late enrichment opportunity as All/general.
-- Its dedupe peers must themselves match the profile and age floor; a generic
-- corpus twin outside the lane never suppresses an Interested story.
create or replace function public.m2_retained_candidates_interested_narrow_for_owner(
  p_owner_id uuid, p_hide_already_opened boolean,
  p_profile_categories text[] default null, p_profile_sources text[] default null,
  p_trend_window_hours integer default 24, p_min_age_hours integer default 6,
  p_before_published_at timestamptz default null,
  p_before_story_id text default null, p_limit integer default 66,
  p_dedupe_window_hours integer default 36,
  p_excluded_story_ids text[] default null,
  p_suppressed_sources text[] default null, p_suppressed_topics text[] default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare floor_at timestamptz; trend_cutoff timestamptz;
begin
  if p_owner_id is null or p_hide_already_opened is null or not exists (
    select 1 from auth.users where id = p_owner_id
  ) then raise exception 'valid owner and opened policy required' using errcode='42501'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then
    raise exception 'invalid cursor';
  end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if p_trend_window_hours is null or p_trend_window_hours < 1 or p_trend_window_hours > 72 then
    raise exception 'invalid trend window';
  end if;
  if p_min_age_hours is null or p_min_age_hours < 1 or p_min_age_hours > 168 then
    raise exception 'invalid age floor';
  end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  if coalesce(cardinality(p_excluded_story_ids), 0) > 1200 or
     coalesce(cardinality(p_suppressed_sources), 0) > 1000 or
     coalesce(cardinality(p_suppressed_topics), 0) > 1000 then
    raise exception 'invalid filter size';
  end if;
  floor_at := now() - make_interval(hours => p_min_age_hours);
  trend_cutoff := now() - make_interval(hours => p_trend_window_hours);
  return query
  with selected as materialized (
    select o.story_id, o.published_at
    from public.retained_corpus_observations o
    where o.published_at < floor_at
      and (o.source_id = any(coalesce(p_profile_sources, '{}'::text[]))
           or exists (select 1 from public.retained_corpus_categories match_category
                      where match_category.story_id = o.story_id
                        and match_category.category_id = any(coalesce(p_profile_categories, '{}'::text[]))))
      and (p_before_published_at is null or o.published_at < p_before_published_at
           or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
      and o.story_id <> all(coalesce(p_excluded_story_ids, '{}'::text[]))
      and (not p_hide_already_opened or not exists (
        select 1 from public.user_story_state opened
        where opened.user_id = p_owner_id and opened.story_id = o.story_id
          and opened.read_at is not null))
      and o.source_id <> all(coalesce(p_suppressed_sources, '{}'::text[]))
      and not exists (select 1 from public.retained_corpus_categories suppressed_category
                      where suppressed_category.story_id = o.story_id
                        and suppressed_category.category_id = any(coalesce(p_suppressed_topics, '{}'::text[])))
      and (p_dedupe_window_hours = 0
           or public.m2_story_dedupe_key(o.title) = ''
           or not exists (
             select 1 from public.retained_corpus_observations peer
             where peer.language = o.language
               and md5(public.m2_story_dedupe_key(peer.title)) = md5(public.m2_story_dedupe_key(o.title))
               -- The fixed-width index supports titles up to the schema's 8 KB
               -- limit; exact comparison prevents even a hash collision from
               -- changing which story survives deduplication.
               and public.m2_story_dedupe_key(peer.title) = public.m2_story_dedupe_key(o.title)
               and (peer.published_at, peer.story_id) > (o.published_at, o.story_id)
               and peer.published_at <= o.published_at + make_interval(hours => p_dedupe_window_hours)
               and peer.published_at < floor_at
               and (peer.source_id = any(coalesce(p_profile_sources, '{}'::text[]))
                    or exists (select 1 from public.retained_corpus_categories peer_category
                               where peer_category.story_id = peer.story_id
                                 and peer_category.category_id = any(coalesce(p_profile_categories, '{}'::text[]))))
           ))
    order by o.published_at desc, o.story_id desc
    limit p_limit
  )
  select jsonb_build_object('schema_version', 2, 'story_id', s.story_id, 'title', s.title,
    'summary', s.summary, 'language', s.language, 'canonical_url', s.canonical_url,
    'source_id', s.source_id, 'source_name', s.source_name,
    'source_is_aggregator', s.source_is_aggregator,
    'published_at', s.published_at, 'source_observed_at', s.source_observed_at,
    'first_ingested_at', s.first_ingested_at, 'last_ingested_at', s.last_ingested_at,
    'first_ready_at', s.first_ready_at, 'last_ready_at', s.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb), 'event_group_id', s.event_group_id,
    'title_translations', s.title_translations, 'summary_translations', s.summary_translations,
    'independent_source_count', greatest(coalesce(cv.independent_source_count, 0),
      case when s.source_is_aggregator then 0 else 1 end)::integer)
  from selected x
  join public.retained_corpus_observations s on s.story_id = x.story_id
  left join lateral (
    select jsonb_agg(category_id order by category_id) as category_ids
    from public.retained_corpus_categories where story_id = s.story_id
  ) c on true
  left join lateral (
    select count(*) as independent_source_count
    from public.retained_corpus_coverage cv
    where cv.story_id = s.story_id and cv.is_independent
      and cv.first_seen_at >= trend_cutoff
  ) cv on true
  order by x.published_at desc, x.story_id desc;
end;
$$;

revoke all on function public.m2_retained_candidates_interested_narrow_for_owner(uuid, boolean,
  text[], text[], integer, integer, timestamptz, text, integer, integer, text[], text[], text[])
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates_interested_narrow_for_owner(uuid, boolean,
  text[], text[], integer, integer, timestamptz, text, integer, integer, text[], text[], text[])
  to service_role;

commit;
