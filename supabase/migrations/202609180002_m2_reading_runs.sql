begin;

-- M2.1 Phase 2, B5: the reading run, and the owner's review of past pages.
--
-- One run per visit. The profile is computed once, at run open, and frozen on
-- the row, so every page inside the run is explainable afterwards from a single
-- stored version instead of from a profile that moved between pages.
--
-- At most one OPEN run per owner, ever: the partial unique index is the
-- constraint and the advisory lock in the RPC is what makes two concurrent first
-- ranks join the same run instead of racing to create two.
create table public.m2_reading_runs (
  run_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  opened_at timestamptz not null default now(),
  last_activity_at timestamptz not null default now(),
  profile_snapshot jsonb not null default '{}'::jsonb
    check (jsonb_typeof(profile_snapshot) = 'object' and octet_length(profile_snapshot::text) <= 262144),
  -- Stories the owner's "less like this" removed from the remaining slices.
  -- Recorded so the filter replays identically in a second tab.
  filtered_story_ids jsonb not null default '[]'::jsonb
    check (jsonb_typeof(filtered_story_ids) = 'array' and jsonb_array_length(filtered_story_ids) <= 2000),
  closed_at timestamptz
);
create unique index m2_reading_runs_one_open_per_owner
  on public.m2_reading_runs(user_id) where closed_at is null;
create index m2_reading_runs_owner_recent_idx on public.m2_reading_runs(user_id, opened_at desc);

alter table public.m2_reading_runs enable row level security;
alter table public.m2_reading_runs force row level security;
revoke all on public.m2_reading_runs from public, anon, authenticated;
grant select, insert, update, delete on public.m2_reading_runs to service_role;
-- The owner may read her own runs. She may not write them: a run is opened by
-- the ranker, never by a client that could mint its own run id.
grant select on public.m2_reading_runs to authenticated;
create policy m2_reading_runs_owner_select on public.m2_reading_runs
  for select to authenticated using (user_id = auth.uid());
create policy m2_reading_runs_service_all on public.m2_reading_runs
  for all to service_role using (true) with check (true);

create or replace function public.m2_open_or_join_reading_run(
  p_user_id uuid, p_idle_minutes integer, p_profile jsonb default '{}'::jsonb
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare existing public.m2_reading_runs%rowtype; created boolean := false;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_idle_minutes is null or p_idle_minutes < 5 or p_idle_minutes > 1440 then
    raise exception 'invalid idle window';
  end if;
  if p_profile is not null and jsonb_typeof(p_profile) <> 'object' then
    raise exception 'invalid profile snapshot';
  end if;
  -- The loser of a race waits here and then JOINS the winner's run. Without
  -- this, two first ranks would each compute and freeze their own profile.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':reading-run', 0));
  select * into existing from public.m2_reading_runs
    where user_id = p_user_id and closed_at is null for update;
  if found and existing.last_activity_at >= now() - make_interval(mins => p_idle_minutes) then
    update public.m2_reading_runs set last_activity_at = now()
      where run_id = existing.run_id returning * into existing;
  else
    if found then
      update public.m2_reading_runs set closed_at = now() where run_id = existing.run_id;
    end if;
    insert into public.m2_reading_runs(user_id, profile_snapshot)
      values (p_user_id, coalesce(p_profile, '{}'::jsonb)) returning * into existing;
    created := true;
  end if;
  return jsonb_build_object('run_id', existing.run_id, 'opened_at', existing.opened_at,
    'profile_snapshot', existing.profile_snapshot,
    'filtered_story_ids', existing.filtered_story_ids, 'created', created);
end;
$$;

create or replace function public.m2_record_reading_run_filter(
  p_user_id uuid, p_run_id uuid, p_story_ids text[]
) returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare merged jsonb;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_story_ids is null or cardinality(p_story_ids) > 500 or exists (
    select 1 from unnest(p_story_ids) story_id where story_id !~ '^story:[0-9a-f]{64}$'
  ) then raise exception 'invalid story ids'; end if;
  update public.m2_reading_runs r set filtered_story_ids = (
      select coalesce(jsonb_agg(distinct value order by value), '[]'::jsonb)
      from (select jsonb_array_elements_text(r.filtered_story_ids) as value
            union select unnest(p_story_ids)) merged_ids
    ), last_activity_at = now()
    where r.run_id = p_run_id and r.user_id = p_user_id and r.closed_at is null
    returning r.filtered_story_ids into merged;
  return case when merged is null then 0 else jsonb_array_length(merged) end;
end;
$$;

-- Every hourly page stays reviewable after the fact. The frozen order already
-- persists the cards; this returns them for one hour so the owner (or her CLI)
-- can look at what she was actually shown, with each card's pool and label.
create or replace function public.m2_owner_reading_pages(p_hour_start timestamptz, p_limit integer default 24)
returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid();
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_hour_start is null then raise exception 'invalid hour'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  return query
  select jsonb_build_object('request_id', f.request_id, 'created_at', f.created_at,
    'page_size', f.page_size, 'run_id', f.bindings->'run_id',
    'result_mode', f.bindings->>'result_mode', 'fallback_reason', f.bindings->>'fallback_reason',
    'eligibility', coalesce(f.bindings->'eligibility', '{}'::jsonb),
    'short_lane_reasons', coalesce(f.bindings->'short_lane_reasons', '[]'::jsonb),
    'cards', (select coalesce(jsonb_agg(jsonb_build_object(
        'story_id', card->>'story_id', 'title', card->>'title',
        'source_name', card->>'source_name', 'lane', card->>'lane',
        'lane_label', card->>'lane_label', 'exclusive_label', card->>'exclusive_label',
        'surprise_label', card->>'surprise_label') order by ordinality), '[]'::jsonb)
      from jsonb_array_elements(f.cards) with ordinality as entries(card, ordinality)))
  from public.m2_frozen_rankings f
  where f.user_id = caller
    and f.created_at >= date_trunc('hour', p_hour_start)
    and f.created_at < date_trunc('hour', p_hour_start) + interval '1 hour'
  order by f.created_at desc
  limit p_limit;
end;
$$;

revoke all on function public.m2_open_or_join_reading_run(uuid, integer, jsonb),
  public.m2_record_reading_run_filter(uuid, uuid, text[]) from public, anon, authenticated;
grant execute on function public.m2_open_or_join_reading_run(uuid, integer, jsonb),
  public.m2_record_reading_run_filter(uuid, uuid, text[]) to service_role;
revoke all on function public.m2_owner_reading_pages(timestamptz, integer) from public, anon;
grant execute on function public.m2_owner_reading_pages(timestamptz, integer) to authenticated;

commit;
