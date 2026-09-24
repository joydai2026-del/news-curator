begin;

-- Return the frozen order in the same owner-scoped transaction that takes the
-- continuation claim. The caller otherwise makes another network trip solely
-- to reread this row after the claim. Behavior is locked first, as in response
-- reservation and frozen extension, so privacy deletion cannot interleave with
-- this snapshot and lock order remains consistent.
create function public.m2_claim_continuation_snapshot(
  p_user_id uuid, p_run_id uuid, p_eligibility_key text,
  p_frozen_order_id uuid, p_token uuid, p_ttl_seconds integer
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare
  claim jsonb;
  frozen jsonb;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_user_id is null or p_run_id is null or p_frozen_order_id is null
     or p_eligibility_key is null or p_eligibility_key !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid continuation claim';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior', 0));
  claim := public.m2_claim_run_ranking(
    p_user_id, p_run_id, p_eligibility_key, p_token, p_ttl_seconds);
  if coalesce((claim ->> 'granted')::boolean, false) is not true then
    return jsonb_build_object('granted', false, 'frozen_order', null);
  end if;

  if claim ->> 'frozen_order_id' = p_frozen_order_id::text then
    select jsonb_build_object('bindings', f.bindings, 'cards', f.cards,
      'page_size', f.page_size, 'expires_at', f.expires_at)
      into frozen
      from public.m2_frozen_rankings f
      where f.frozen_order_id = p_frozen_order_id
        and f.user_id = p_user_id and f.run_id = p_run_id;
  end if;
  return jsonb_build_object('granted', true, 'frozen_order', frozen);
end;
$$;

revoke all on function public.m2_claim_continuation_snapshot(
  uuid, uuid, text, uuid, uuid, integer) from public, anon, authenticated;
grant execute on function public.m2_claim_continuation_snapshot(
  uuid, uuid, text, uuid, uuid, integer) to service_role;

commit;
