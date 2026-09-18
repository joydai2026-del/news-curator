begin;

-- M2.1 Phase 2, fix round 6: claim the run's ranking BEFORE paying for it.
--
-- Making rank() idempotent inside a run closed the refresh hole but not the
-- race. The reuse check asked whether the run already had a bound order, the
-- bind happened only AFTER the provider call, and the advisory lock in the
-- open-or-join RPC covers that RPC's transaction and nothing more. So a second
-- request arriving while the first was still in flight saw no bound order,
-- reserved, called the provider, and then raced the bind: one run, two paid
-- rankings, and whichever bind landed last silently won.
--
-- A claim is the missing step. It is taken in ONE statement before any money
-- moves, it expires on its own so a crashed request cannot wedge the run for
-- ever, and the bind is conditional on still holding it.
-- The claim columns live on the VIEW row (created in 202609180005), because a
-- category request and an All request inside one run are two different rankings
-- and must be able to run at the same time.

-- Compare-and-set. The WHERE clause is the whole mechanism: exactly one caller
-- can move the row from "unclaimed or expired" to "claimed by me", and the
-- loser learns that from an empty update rather than from a duplicate charge.
create or replace function public.m2_claim_run_ranking(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text, p_token uuid, p_ttl_seconds integer
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare claimed public.m2_reading_run_views%rowtype; current_row public.m2_reading_run_views%rowtype;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_token is null then raise exception 'invalid claim token'; end if;
  if p_ttl_seconds is null or p_ttl_seconds < 5 or p_ttl_seconds > 600 then
    raise exception 'invalid claim window';
  end if;
  -- Take the SAME row lock the claimed reservation takes, so a takeover and a
  -- reservation cannot interleave: whichever gets the lock first, the other one
  -- reads the settled answer rather than a snapshot from before it.
  perform 1 from public.m2_reading_run_views v
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
      and exists (select 1 from public.m2_reading_runs r
                  where r.run_id = v.run_id and r.user_id = p_user_id)
    for update;
  update public.m2_reading_run_views v
    set ranking_claim_token = p_token, ranking_claimed_at = now()
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
      and exists (select 1 from public.m2_reading_runs r
                  where r.run_id = v.run_id and r.user_id = p_user_id)
      and (v.ranking_claim_token is null
           or v.ranking_claimed_at is null
           -- An expired claim is taken over, not respected: a request that died
           -- mid-flight must not lock its own reader out of the feed.
           or v.ranking_claimed_at < now() - make_interval(secs => p_ttl_seconds))
    returning * into claimed;
  if found then
    return jsonb_build_object('granted', true, 'token', p_token,
                              'frozen_order_id', claimed.frozen_order_id);
  end if;
  select * into current_row from public.m2_reading_run_views v
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key;
  -- Not granted. The order id travels back anyway, because by the time the
  -- loser reads this the winner may already have bound one, and serving that is
  -- better than making her wait.
  return jsonb_build_object('granted', false, 'token', null,
                            'frozen_order_id', current_row.frozen_order_id);
end;
$$;

-- Binding is now conditional on STILL holding the claim, and it releases it.
-- Without the condition a slow loser could overwrite the winner's order after
-- the fact, which is the same race one step later.
create or replace function public.m2_bind_run_frozen_order(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text, p_frozen_order_id uuid,
  p_token uuid default null
) returns boolean language plpgsql security definer set search_path = pg_catalog, public as $$
declare bound uuid;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  update public.m2_reading_run_views v
    set frozen_order_id = p_frozen_order_id,
        ranking_claim_token = null, ranking_claimed_at = null
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
      and exists (select 1 from public.m2_reading_runs r
                  where r.run_id = v.run_id and r.user_id = p_user_id)
      and (p_token is null or v.ranking_claim_token = p_token)
    returning v.frozen_order_id into bound;
  return bound is not null;
end;
$$;

-- Releasing a claim nobody used. A ranking that ended in a fallback or an error
-- should not make the next request wait out the whole TTL.
create or replace function public.m2_release_run_ranking_claim(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text, p_token uuid
) returns boolean language plpgsql security definer set search_path = pg_catalog, public as $$
declare released uuid;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  update public.m2_reading_run_views v
    set ranking_claim_token = null, ranking_claimed_at = null
    where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
      and v.ranking_claim_token = p_token
      and exists (select 1 from public.m2_reading_runs r
                  where r.run_id = v.run_id and r.user_id = p_user_id)
    returning v.run_id into released;
  return released is not null;
end;
$$;

revoke all on function public.m2_claim_run_ranking(uuid, uuid, text, uuid, integer),
  public.m2_bind_run_frozen_order(uuid, uuid, text, uuid, uuid),
  public.m2_release_run_ranking_claim(uuid, uuid, text, uuid) from public, anon, authenticated;
grant execute on function public.m2_claim_run_ranking(uuid, uuid, text, uuid, integer),
  public.m2_bind_run_frozen_order(uuid, uuid, text, uuid, uuid),
  public.m2_release_run_ranking_claim(uuid, uuid, text, uuid) to service_role;

commit;
