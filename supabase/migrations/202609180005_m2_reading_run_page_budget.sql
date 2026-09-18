begin;

-- M2.1 Phase 2, fix round 5: the per-run page budget lives on the RUN.
--
-- The cap was enforced against the cursor of the current frozen order, and
-- rank() minted a new frozen order (and a new cursor) on every call while
-- joining the SAME open run. So a refresh reset the cap: load to the last page,
-- refresh, and the whole budget was available again. The budget has to be
-- counted where the run is, not where the cursor is.
--
-- Two columns. `frozen_order_id` is the run's one ranking, so a refresh inside a
-- run can return the order it already paid for instead of buying another.
-- `pages_served` is the high-water mark of pages that run has handed over.
alter table public.m2_reading_runs
  add column if not exists frozen_order_id uuid;
alter table public.m2_reading_runs
  add column if not exists pages_served integer not null default 0 check (pages_served >= 0);

create or replace function public.m2_bind_run_frozen_order(
  p_user_id uuid, p_run_id uuid, p_frozen_order_id uuid
) returns boolean language plpgsql security definer set search_path = pg_catalog, public as $$
declare bound uuid;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  update public.m2_reading_runs r set frozen_order_id = p_frozen_order_id
    where r.run_id = p_run_id and r.user_id = p_user_id
    returning r.frozen_order_id into bound;
  return bound is not null;
end;
$$;

-- Returns the count BEFORE this page, so the caller can decide whether to serve
-- it. Recording first and then checking would let the very request that trips
-- the cap also be the one that inflates it.
create or replace function public.m2_record_run_page(
  p_user_id uuid, p_run_id uuid, p_pages integer
) returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare previous integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_pages is null or p_pages < 0 or p_pages > 1000 then
    raise exception 'invalid page count';
  end if;
  select r.pages_served into previous from public.m2_reading_runs r
    where r.run_id = p_run_id and r.user_id = p_user_id for update;
  if not found then
    return 0;
  end if;
  -- A high-water mark, not a counter: re-reading page one must not spend the
  -- budget, and paging out of order must not either.
  update public.m2_reading_runs r set pages_served = greatest(r.pages_served, p_pages),
    last_activity_at = now()
    where r.run_id = p_run_id and r.user_id = p_user_id;
  return previous;
end;
$$;

create or replace function public.m2_open_or_join_reading_run(
  p_user_id uuid, p_idle_minutes integer, p_profile jsonb default '{}'::jsonb,
  p_max_minutes integer default 60
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare existing public.m2_reading_runs%rowtype; created boolean := false;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_idle_minutes is null or p_idle_minutes < 5 or p_idle_minutes > 1440 then
    raise exception 'invalid idle window';
  end if;
  if p_max_minutes is null or p_max_minutes < 15 or p_max_minutes > 240 then
    raise exception 'invalid run age cap';
  end if;
  if p_profile is not null and jsonb_typeof(p_profile) <> 'object' then
    raise exception 'invalid profile snapshot';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':reading-run', 0));
  select * into existing from public.m2_reading_runs
    where user_id = p_user_id and closed_at is null for update;
  if found
     and existing.last_activity_at > now() - make_interval(mins => p_idle_minutes)
     and existing.opened_at > now() - make_interval(mins => p_max_minutes) then
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
  -- The run's own ranking and its page budget travel with it, so a refresh can
  -- be answered from what this run already has.
  return jsonb_build_object('run_id', existing.run_id, 'opened_at', existing.opened_at,
    'profile_snapshot', existing.profile_snapshot,
    'filtered_story_ids', existing.filtered_story_ids,
    'frozen_order_id', existing.frozen_order_id,
    'pages_served', existing.pages_served, 'created', created);
end;
$$;

revoke all on function public.m2_bind_run_frozen_order(uuid, uuid, uuid),
  public.m2_record_run_page(uuid, uuid, integer) from public, anon, authenticated;
grant execute on function public.m2_bind_run_frozen_order(uuid, uuid, uuid),
  public.m2_record_run_page(uuid, uuid, integer) to service_role;

commit;
