begin;

-- RESHAPED IN PLACE ON 2026-09-18, BEFORE FIRST APPLICATION. This file briefly
-- added `frozen_order_id` and `pages_served` as columns on m2_reading_runs.
-- Verified the same day through PostgREST that neither m2_reading_runs nor
-- retained_corpus_coverage exists in production (404 with the publishable key,
-- calibrated against a table known not to exist), so the earlier shape has been
-- applied nowhere but CI containers, which are created and destroyed per run.
-- There is therefore NO upgrade path from that shape, by design: writing one
-- would be migrating a state that has never existed anywhere.
--
-- M2.1 Phase 2, fix round 5 and 6: the page budget and the run's ranking live
-- per VIEW, not per run.
--
-- Round 5 fixed the refresh hole by making rank() reuse the run's ranking, and
-- put the budget on the run. Round 6 measured what that cost: a `tech` request
-- inside an open run made zero corpus calls and returned the All page
-- byte-for-byte, so for up to sixty minutes every topic tap, every search and
-- the Chinese-press section returned All.
--
-- Idempotence is keyed by (run, eligibility). A view is one of All, a category,
-- a search, or the language-exclusive section; each gets at most one paid
-- ranking, its own frozen order, its own page budget and its own claim. The run
-- still owns the profile and the idle window, which are genuinely per visit.
create table if not exists public.m2_reading_run_views (
  run_id uuid not null references public.m2_reading_runs(run_id) on delete cascade,
  -- A digest of (category, query, exclusive lane), computed by the caller. A
  -- digest rather than the values themselves because a search query is owner
  -- text and this table is an index, not a place to keep what she typed.
  eligibility_key text not null check (eligibility_key ~ '^[0-9a-f]{64}$'),
  frozen_order_id uuid,
  pages_served integer not null default 0 check (pages_served >= 0),
  ranking_claim_token uuid,
  ranking_claimed_at timestamptz,
  created_at timestamptz not null default now(),
  primary key (run_id, eligibility_key)
);

alter table public.m2_reading_run_views enable row level security;
alter table public.m2_reading_run_views force row level security;
revoke all on public.m2_reading_run_views from public, anon, authenticated;
grant select, insert, update, delete on public.m2_reading_run_views to service_role;

-- Get-or-create, so the caller learns in one round trip whether this view has
-- already been ranked in this run.
create or replace function public.m2_open_run_view(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare view_row public.m2_reading_run_views%rowtype;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_eligibility_key is null or p_eligibility_key !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid eligibility key';
  end if;
  if not exists (select 1 from public.m2_reading_runs r
                 where r.run_id = p_run_id and r.user_id = p_user_id) then
    raise exception 'unknown reading run';
  end if;
  insert into public.m2_reading_run_views(run_id, eligibility_key)
    values (p_run_id, p_eligibility_key) on conflict (run_id, eligibility_key) do nothing;
  select * into view_row from public.m2_reading_run_views v
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key;
  return jsonb_build_object('run_id', view_row.run_id, 'eligibility_key', view_row.eligibility_key,
    'frozen_order_id', view_row.frozen_order_id, 'pages_served', view_row.pages_served);
end;
$$;

-- Returns the count BEFORE this page, so the caller can decide whether to serve
-- it. Recording first and then checking would let the very request that trips
-- the cap also be the one that inflates it.
create or replace function public.m2_record_run_page(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text, p_pages integer
) returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare previous integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_pages is null or p_pages < 0 or p_pages > 1000 then
    raise exception 'invalid page count';
  end if;
  select v.pages_served into previous from public.m2_reading_run_views v
    join public.m2_reading_runs r on r.run_id = v.run_id and r.user_id = p_user_id
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key for update of v;
  if not found then
    return 0;
  end if;
  -- A high-water mark, not a counter: re-reading page one must not spend the
  -- budget, and paging out of order must not either.
  update public.m2_reading_run_views v set pages_served = greatest(v.pages_served, p_pages)
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key;
  update public.m2_reading_runs r set last_activity_at = now()
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
  -- The run owns the profile and the idle window. What was ranked, and how many
  -- pages of it have been served, belong to a VIEW and are read separately.
  return jsonb_build_object('run_id', existing.run_id, 'opened_at', existing.opened_at,
    'profile_snapshot', existing.profile_snapshot,
    'filtered_story_ids', existing.filtered_story_ids, 'created', created);
end;
$$;

revoke all on function public.m2_open_run_view(uuid, uuid, text),
  public.m2_record_run_page(uuid, uuid, text, integer) from public, anon, authenticated;
grant execute on function public.m2_open_run_view(uuid, uuid, text),
  public.m2_record_run_page(uuid, uuid, text, integer) to service_role;

commit;
