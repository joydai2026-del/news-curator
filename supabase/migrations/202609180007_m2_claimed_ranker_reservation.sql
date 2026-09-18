begin;

-- M2.1 Phase 2, fix round 7: the reservation re-validates the claim.
--
-- The claim closes the concurrent-start race, but a TTL takeover could still
-- double-pay. A second caller takes the claim over purely by age; if the first
-- caller is merely slow rather than dead, both then reserve and both call the
-- provider. The late one saves an order, its conditional bind returns false, and
-- nothing was looking at that false: two paid rankings and an order nobody is
-- bound to.
--
-- Two guards, and this is the second one. The first is a config rule that the
-- claim cannot expire while its holder's provider call is still allowed to be in
-- flight. This one is the backstop for everything that rule cannot see: the
-- holder re-checks its claim IN THE SAME TRANSACTION as the reservation, so a
-- caller whose claim has moved on spends nothing at all.
-- RETURNS AN OBJECT, not a boolean, so "you lost the claim" and "you are out of
-- budget" are different answers. It deliberately does NOT raise on a lost claim:
-- losing a race is normal, the caller already knows how to serve the winner's
-- order, and raising would turn a handled race into an error page.
create or replace function public.m2_reserve_ranker_budget_claimed(
  p_user_id uuid, p_request_id uuid, p_amount_usd numeric, p_daily_limit_usd numeric,
  p_run_id uuid, p_eligibility_key text, p_claim_token uuid
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare today date := (statement_timestamp() at time zone 'utc')::date; accepted boolean := false;
        live_token uuid; locked boolean := false; remaining numeric;
begin
  if p_amount_usd <= 0 or p_daily_limit_usd <= 0 or p_amount_usd > p_daily_limit_usd then
    return jsonb_build_object('reserved', false, 'refusal', 'invalid_amount');
  end if;
  if p_claim_token is not null then
    -- LOCK THE VIEW ROW FIRST, and hold it for the rest of the transaction.
    -- A bare `exists` check decided on a snapshot and then moved money in later
    -- statements: under READ COMMITTED a takeover lands in between, and a stale
    -- holder reserves and calls the provider anyway. The claim RPC takes the
    -- same lock, so the two cannot interleave.
    select v.ranking_claim_token into live_token
      from public.m2_reading_run_views v
      where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
        and exists (select 1 from public.m2_reading_runs r
                    where r.run_id = v.run_id and r.user_id = p_user_id)
      for update;
    locked := found;
    if not locked or live_token is distinct from p_claim_token then
      return jsonb_build_object('reserved', false, 'refusal', 'claim_lost',
                                'remaining_usd', null);
    end if;
  end if;
  insert into public.m2_ranker_daily_budget(user_id,budget_date) values(p_user_id,today) on conflict do nothing;
  update public.m2_ranker_daily_budget set reserved_usd=reserved_usd+p_amount_usd
   where user_id=p_user_id and budget_date=today and spent_usd+reserved_usd+p_amount_usd <= p_daily_limit_usd
     -- Predicated on the claim as well, so even a lock that was somehow not
     -- held cannot move capacity for a holder whose token has moved on.
     and (p_claim_token is null or exists (
       select 1 from public.m2_reading_run_views v
       where v.run_id = p_run_id and v.eligibility_key = p_eligibility_key
         and v.ranking_claim_token = p_claim_token))
   returning true into accepted;
  if coalesce(accepted,false) then
    insert into public.m2_ranker_reservations(request_id,user_id,budget_date,reserved_usd,status)
      values(p_request_id,p_user_id,today,p_amount_usd,'reserved');
    return jsonb_build_object('reserved', true, 'refusal', '', 'remaining_usd', null);
  end if;
  -- What was left when the refusal happened. A refusal that only says "no" makes
  -- an operator go and query the ledger by hand to find out how close it was.
  select greatest(p_daily_limit_usd - (b.spent_usd + b.reserved_usd), 0) into remaining
    from public.m2_ranker_daily_budget b
    where b.user_id = p_user_id and b.budget_date = today;
  return jsonb_build_object('reserved', false, 'refusal', 'budget',
                            'remaining_usd', coalesce(remaining, p_daily_limit_usd));
end;
$$;

-- m2_reserve_ranker_budget is left exactly as it was, so the rollback ACL drill
-- and the pre-Phase-2 path keep working unchanged.
revoke all on function public.m2_reserve_ranker_budget_claimed(uuid, uuid, numeric, numeric, uuid, text, uuid)
  from public, anon, authenticated;
grant execute on function public.m2_reserve_ranker_budget_claimed(uuid, uuid, numeric, numeric, uuid, text, uuid)
  to service_role;

commit;
