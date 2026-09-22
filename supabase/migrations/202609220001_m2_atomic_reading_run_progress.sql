begin;

-- A privacy epoch and its reading run must move together.  The earlier service
-- closed a stale run with one HTTP request and opened its replacement with a
-- second.  A request that had captured the old epoch could create another stale
-- run in that gap.  Validate the caller's frozen profile against the live
-- privacy rows while holding the same behavior lock used by consent/reset, then
-- close and replace an old-epoch run under the existing per-owner run lock.
create or replace function public.m2_open_or_join_reading_run_v2(
  p_user_id uuid, p_idle_minutes integer, p_profile jsonb default '{}'::jsonb,
  p_max_minutes integer default 60
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare
  existing public.m2_reading_runs%rowtype;
  created boolean := false;
  current_generation bigint;
  current_consent_revision bigint;
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
  if p_profile is null or jsonb_typeof(p_profile) <> 'object' then
    raise exception 'invalid profile snapshot';
  end if;

  -- Lock order is behavior, then reading-run.  Consent/reset already uses the
  -- behavior lock, while no existing run function takes these in reverse order.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior', 0));
  select
    coalesce((select history_generation from public.user_behavior_revisions
              where user_id = p_user_id), 1),
    coalesce((select consent_revision from public.user_behavior_settings
              where user_id = p_user_id), 0)
    into current_generation, current_consent_revision;
  if p_profile->'_history_generation' is distinct from to_jsonb(current_generation)
     or p_profile->'_consent_revision' is distinct from to_jsonb(current_consent_revision) then
    raise exception 'stale reading run epoch' using errcode = '40001';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':reading-run', 0));
  select * into existing from public.m2_reading_runs
    where user_id = p_user_id and closed_at is null for update;
  if found
     and existing.profile_snapshot->'_history_generation' is not distinct from to_jsonb(current_generation)
     and existing.profile_snapshot->'_consent_revision' is not distinct from to_jsonb(current_consent_revision)
     and existing.last_activity_at > now() - make_interval(mins => p_idle_minutes)
     and existing.opened_at > now() - make_interval(mins => p_max_minutes) then
    update public.m2_reading_runs set last_activity_at = now()
      where run_id = existing.run_id returning * into existing;
  else
    if found then
      update public.m2_reading_runs set closed_at = now() where run_id = existing.run_id;
    end if;
    insert into public.m2_reading_runs(user_id, profile_snapshot)
      values (p_user_id, p_profile) returning * into existing;
    created := true;
  end if;
  return jsonb_build_object('run_id', existing.run_id, 'opened_at', existing.opened_at,
    'profile_snapshot', existing.profile_snapshot,
    'filtered_story_ids', existing.filtered_story_ids, 'created', created);
end;
$$;

-- Reserve one readable response and persist the offset that belongs to that
-- response in the SAME transaction.  A refresh can therefore never combine a
-- newer page high-water mark with an older frozen-order offset (or vice versa).
create or replace function public.m2_reserve_run_response(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text, p_frozen_order_id uuid,
  p_response_number integer, p_offset integer, p_next_offset integer
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare
  previous integer;
  card_count integer;
  bound_response integer;
  bound_offset integer;
  bound_next_offset integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_eligibility_key is null or p_eligibility_key !~ '^[0-9a-f]{64}$'
     or p_response_number is null or p_response_number < 1 or p_response_number > 1000
     or p_offset is null or p_offset < 0
     or p_next_offset is null or p_next_offset < p_offset or p_next_offset > 1000 then
    raise exception 'invalid response reservation';
  end if;

  -- Consent withdrawal and history clearing take this lock before deleting
  -- frozen orders. Take it before either row lock below, so a page reservation
  -- and a privacy mutation cannot wait on each other in opposite order.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior', 0));

  select v.pages_served into previous
    from public.m2_reading_run_views v
    join public.m2_reading_runs r on r.run_id = v.run_id and r.user_id = p_user_id
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
      and v.frozen_order_id = p_frozen_order_id
    for update of v;
  if not found then
    return jsonb_build_object('reserved', false, 'previous', null);
  end if;

  select jsonb_array_length(f.cards),
         case when jsonb_typeof(f.bindings->'responses_served') = 'number'
              then (f.bindings->>'responses_served')::integer end,
         case when jsonb_typeof(f.bindings->'last_served_offset') = 'number'
              then (f.bindings->>'last_served_offset')::integer end,
         case when jsonb_typeof(f.bindings->'last_served_next_offset') = 'number'
              then (f.bindings->>'last_served_next_offset')::integer end
    into card_count, bound_response, bound_offset, bound_next_offset
    from public.m2_frozen_rankings f
    where f.frozen_order_id = p_frozen_order_id and f.user_id = p_user_id
      and f.run_id = p_run_id
    for update;
  if not found or p_offset > card_count then
    return jsonb_build_object('reserved', false, 'previous', previous);
  end if;

  -- Sequential reservation, idempotent exact replay, plus the one documented
  -- legacy repair where page one was returned before its count persisted.
  if not (previous in (p_response_number - 1, p_response_number)
          or (previous = 0 and p_response_number = 2)) then
    return jsonb_build_object('reserved', false, 'previous', previous);
  end if;
  if previous = p_response_number and bound_response = p_response_number
     and (bound_offset is distinct from p_offset
          or bound_next_offset is distinct from p_next_offset) then
    return jsonb_build_object('reserved', false, 'previous', previous);
  end if;

  update public.m2_reading_run_views
    set pages_served = greatest(pages_served, p_response_number)
    where run_id = p_run_id and eligibility_key = p_eligibility_key;
  update public.m2_frozen_rankings
    set bindings = bindings || jsonb_build_object(
      'responses_served', p_response_number,
      'last_served_offset', p_offset,
      'last_served_next_offset', p_next_offset)
    where frozen_order_id = p_frozen_order_id and user_id = p_user_id and run_id = p_run_id;
  update public.m2_reading_runs set last_activity_at = now()
    where run_id = p_run_id and user_id = p_user_id;
  return jsonb_build_object('reserved', true, 'previous', previous);
end;
$$;

-- The continuation RPC must use the same global owner lock order as response
-- reservation and privacy mutations.  Its UPDATE takes a frozen-ranking row
-- lock before the existing validation trigger asks for the behavior lock.  If
-- reservation already held behavior while waiting for that frozen row, the two
-- transactions could deadlock.  Acquire behavior first here, then let the
-- UPDATE and trigger proceed in that established order.
create or replace function public.m2_extend_frozen_ranking(
  p_user_id uuid, p_frozen_order_id uuid, p_cards jsonb, p_bindings jsonb
) returns integer language plpgsql security definer set search_path=pg_catalog,public as $$
declare total integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_cards) <> 'array' or jsonb_array_length(p_cards) > 200 then
    raise exception 'invalid continuation cards';
  end if;
  if p_bindings is not null and jsonb_typeof(p_bindings) <> 'object' then
    raise exception 'invalid continuation bindings';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior', 0));
  update public.m2_frozen_rankings f
    set cards = f.cards || p_cards,
        bindings = f.bindings || coalesce(p_bindings, '{}'::jsonb)
    where f.frozen_order_id = p_frozen_order_id and f.user_id = p_user_id
      and jsonb_array_length(f.cards) + jsonb_array_length(p_cards) <= 500
    returning jsonb_array_length(f.cards) into total;
  return coalesce(total, 0);
end; $$;

revoke all on function public.m2_reserve_run_response(
  uuid, uuid, text, uuid, integer, integer, integer
) from public, anon, authenticated;
grant execute on function public.m2_reserve_run_response(
  uuid, uuid, text, uuid, integer, integer, integer
) to service_role;

revoke all on function public.m2_open_or_join_reading_run_v2(
  uuid, integer, jsonb, integer
) from public, anon, authenticated;
grant execute on function public.m2_open_or_join_reading_run_v2(
  uuid, integer, jsonb, integer
) to service_role;

revoke all on function public.m2_extend_frozen_ranking(uuid, uuid, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.m2_extend_frozen_ranking(uuid, uuid, jsonb, jsonb)
  to service_role;

commit;
