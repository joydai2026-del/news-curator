begin;

-- M2.1 Phase 2, fix round 1: a PAID ranking is never lost to a behavior write,
-- and a page turn never buys a second one.
--
-- Two findings share one root cause. The epoch trigger required the frozen
-- ranking's server_commit_revision to equal the CURRENT behavior revision at
-- insert time. Every behavior event moves that number, so a save in another tab
-- between the provider returning and the insert landing made the trigger reject
-- an order the owner had already been charged for. The same rigidity is why the
-- service fell back to calling rank() again (and paying again) to continue past
-- the end of a frozen order.
--
-- WHAT IS RELAXED, AND WHAT IS NOT. history_generation and consent_revision
-- still have to match EXACTLY, and the delete triggers that wipe frozen
-- rankings on a consent change or a history reset are untouched. Those are the
-- privacy guarantees: a model-mode order must not survive consent being
-- withdrawn or history being cleared. server_commit_revision is not a privacy
-- boundary; it only says "some behavior event happened since". Inside an OPEN
-- reading run that is exactly the state the design already expects, because the
-- run's profile is frozen and the order is deliberately not supposed to move
-- while she reads. So it may be older than current, and ONLY then.
--
-- What this refuses when its assumption is wrong: a ranking carrying a run_id
-- whose run is closed, or belongs to someone else, or does not exist, gets the
-- strict equality check exactly as before. A client cannot mint a run id: the
-- owner has no insert or update grant on m2_reading_runs.
-- Idempotent by construction, matching the repository convention: applying this
-- file twice is a no-op rather than an error, so a re-run during a recovery is
-- safe and nobody has to know whether it already ran.
alter table public.m2_frozen_rankings
  add column if not exists run_id uuid references public.m2_reading_runs(run_id) on delete set null;
create index if not exists m2_frozen_rankings_run_idx on public.m2_frozen_rankings(run_id)
  where run_id is not null;

create or replace function public.m2_validate_frozen_ranking_epoch()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare current_generation bigint; current_revision bigint; settings public.user_behavior_settings%rowtype;
        run_is_open boolean := false;
begin
  -- An UPDATE that changes neither the cards nor the bindings is not a ranking
  -- write and must not be re-validated. The FK on run_id is `on delete set
  -- null`, so closing or deleting a reading run issues exactly such an update,
  -- and re-running the epoch check there made deleting a run fail with "stale
  -- frozen ranking bindings" (caught live by the CI database job, not by
  -- reading). The guarantee is about what an order CONTAINS, so it is checked
  -- when that changes.
  if tg_op = 'UPDATE'
     and new.bindings is not distinct from old.bindings
     and new.cards is not distinct from old.cards then
    return new;
  end if;
  -- Close the check-then-insert race with reset/revocation. A late provider
  -- result waits behind the same lock and cannot recreate erased private cards.
  perform pg_advisory_xact_lock(hashtextextended(new.user_id::text || ':behavior',0));
  select coalesce((select history_generation from public.user_behavior_revisions where user_id=new.user_id),1),
    coalesce((select latest_revision from public.user_behavior_revisions where user_id=new.user_id),0)
    into current_generation,current_revision;
  select * into settings from public.user_behavior_settings where user_id=new.user_id;
  if new.run_id is not null then
    select true into run_is_open from public.m2_reading_runs r
      where r.run_id = new.run_id and r.user_id = new.user_id and r.closed_at is null;
  end if;
  if new.bindings->'history_generation' is distinct from to_jsonb(current_generation)
     or new.bindings->'consent_revision' is distinct from to_jsonb(coalesce(settings.consent_revision,0)) then
    raise exception 'stale frozen ranking bindings'; end if;
  if coalesce(run_is_open,false) then
    -- Inside an open run the order is frozen ON PURPOSE. A behavior event that
    -- landed while the provider was answering must not throw away the answer.
    -- A revision from the FUTURE is still refused: that is not lag, that is a
    -- binding nobody computed.
    if (new.bindings->>'server_commit_revision')::bigint > current_revision then
      raise exception 'stale frozen ranking bindings'; end if;
  elsif new.bindings->'server_commit_revision' is distinct from to_jsonb(current_revision) then
    raise exception 'stale frozen ranking bindings';
  end if;
  if new.bindings->>'result_mode'='model' and not
    (coalesce(settings.learning_enabled,false) and coalesce(settings.provider_processing_enabled,false)) then
    raise exception 'model processing consent required'; end if;
  return new;
end; $$;

-- Continuing past the end of a frozen order APPENDS to it, so the signed cursor
-- keeps pointing at stable offsets. The continuation carries no provider call,
-- so there is no ranking receipt to bind and nothing new to consent to; this
-- writes cards and merges binding keys, and touches nothing else.
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
  update public.m2_frozen_rankings f
    set cards = f.cards || p_cards,
        bindings = f.bindings || coalesce(p_bindings, '{}'::jsonb)
    where f.frozen_order_id = p_frozen_order_id and f.user_id = p_user_id
      and jsonb_array_length(f.cards) + jsonb_array_length(p_cards) <= 500
    returning jsonb_array_length(f.cards) into total;
  return coalesce(total, 0);
end; $$;

revoke all on function public.m2_extend_frozen_ranking(uuid, uuid, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.m2_extend_frozen_ranking(uuid, uuid, jsonb, jsonb) to service_role;

commit;
