begin;

-- Two facts the hourly job must remember ACROSS runs, because an in-memory
-- ledger resets twelve times an hour and is therefore not a daily cap at all.
--
-- 1. Dollars reserved and settled per UTC day, mirroring the character
--    counters that already live beside them.
-- 2. One exclusivity decision per story, so the model is asked ONCE and every
--    later run reuses the answer rather than paying for it again.

create or replace function public.m2_decision_is_settled(p_outcome text)
returns boolean language sql immutable parallel safe as $$
  select p_outcome in ('exclusive', 'matched');
$$;

create table translation_private.translation_spend_counters (
  scope_type text not null check (scope_type in ('day', 'month')),
  scope_key text not null check (octet_length(scope_key) between 1 and 256),
  usd_reserved numeric(12, 6) not null default 0 check (usd_reserved >= 0),
  usd_settled numeric(12, 6) not null default 0 check (usd_settled >= 0),
  -- Pairing is a paid call too. Its count is per UTC day, not per process.
  pairing_calls integer not null default 0 check (pairing_calls >= 0),
  updated_at timestamptz not null default clock_timestamp(),
  primary key (scope_type, scope_key)
);

-- One decision per (story, display language, policy). A prompt or model change
-- mints a new policy_id and therefore a new decision rather than silently
-- inheriting an answer produced by different instructions.
create table translation_private.exclusivity_decisions (
  story_id text not null check (story_id ~ '^story:[0-9a-f]{64}$'),
  display_language text not null check (display_language in ('en', 'zh')),
  policy_id text not null check (policy_id ~ '^[A-Za-z0-9._-]{1,64}$'),
  decided_at timestamptz not null default clock_timestamp(),
  model text not null check (model <> '' and octet_length(model) <= 256),
  -- exclusive: no display-language outlet carried this event.
  -- matched: match_story_id names the display-language story.
  -- undecided: the model gave no usable answer; retry_after bounds the re-ask.
  outcome text not null check (outcome in ('exclusive', 'matched', 'undecided')),
  match_story_id text check (match_story_id is null or match_story_id ~ '^story:[0-9a-f]{64}$'),
  attempts integer not null default 1 check (attempts >= 0),
  retry_after timestamptz,
  rechecked_at timestamptz,
  primary key (story_id, display_language, policy_id),
  constraint exclusivity_decision_is_not_self check (match_story_id is null or match_story_id <> story_id),
  constraint exclusivity_match_has_a_story check (
    (outcome = 'matched' and match_story_id is not null)
    or (outcome <> 'matched' and match_story_id is null))
);
create index exclusivity_decisions_decided_idx on translation_private.exclusivity_decisions(decided_at desc);
create index exclusivity_decisions_exclusive_idx on translation_private.exclusivity_decisions(display_language, story_id)
  where outcome = 'exclusive';

alter table translation_private.translation_spend_counters enable row level security;
alter table translation_private.translation_spend_counters force row level security;
alter table translation_private.exclusivity_decisions enable row level security;
alter table translation_private.exclusivity_decisions force row level security;
revoke all on translation_private.translation_spend_counters, translation_private.exclusivity_decisions
  from public, anon, authenticated;

-- Reserve dollars for one attempt. Refuses rather than clamps, exactly like the
-- character ledger, and the caller then shows the story untranslated.
create or replace function public.m2_reserve_translation_spend(p_amount_usd numeric, p_daily_limit_usd numeric)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text; reserved numeric; settled numeric;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_amount_usd is null or p_amount_usd < 0 or p_daily_limit_usd is null or p_daily_limit_usd < 0 then
    raise exception 'invalid translation spend request';
  end if;
  insert into translation_private.translation_spend_counters(scope_type, scope_key)
    values ('day', day_key) on conflict do nothing;
  select usd_reserved, usd_settled into reserved, settled
    from translation_private.translation_spend_counters
    where scope_type = 'day' and scope_key = day_key for update;
  if settled + reserved + p_amount_usd > p_daily_limit_usd then
    return jsonb_build_object('status', 'cost_limit_reached',
      'usd_reserved', reserved, 'usd_settled', settled);
  end if;
  update translation_private.translation_spend_counters
    set usd_reserved = usd_reserved + p_amount_usd, updated_at = now()
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('status', 'reserved', 'usd_reserved', reserved + p_amount_usd, 'usd_settled', settled);
end;
$$;

-- Settle an attempt. `p_settled_usd` is the observed cost; passing the same
-- amount as the reservation is how an unknown charge stays charged.
create or replace function public.m2_settle_translation_spend(
  p_reserved_usd numeric, p_settled_usd numeric, p_day_key text default null)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := coalesce(p_day_key, (now() at time zone 'utc')::date::text); touched integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_reserved_usd is null or p_reserved_usd < 0 or p_settled_usd is null or p_settled_usd < 0 then
    raise exception 'invalid translation settlement';
  end if;
  -- The caller passes the day it RESERVED against, so a run that crosses
  -- midnight settles where it reserved instead of silently losing the charge.
  insert into translation_private.translation_spend_counters(scope_type, scope_key)
    values ('day', day_key) on conflict do nothing;
  update translation_private.translation_spend_counters
    set usd_reserved = greatest(0, usd_reserved - p_reserved_usd),
        usd_settled = usd_settled + p_settled_usd, updated_at = now()
    where scope_type = 'day' and scope_key = day_key;
  get diagnostics touched = row_count;
  if touched = 0 then
    raise exception 'translation settlement lost its day row';
  end if;
  return jsonb_build_object('status', 'settled', 'scope_key', day_key);
end;
$$;

create or replace function public.m2_release_translation_spend(p_reserved_usd numeric)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_reserved_usd is null or p_reserved_usd < 0 then
    raise exception 'invalid translation release';
  end if;
  -- A reservation released BEFORE the provider was entered cost nothing, so
  -- holding it would let a flaky store burn the day's cap on free failures.
  update translation_private.translation_spend_counters
    set usd_reserved = greatest(0, usd_reserved - p_reserved_usd), updated_at = now()
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('status', 'released');
end;
$$;

create or replace function public.m2_reserve_pairing_call(p_amount_usd numeric, p_daily_limit_usd numeric, p_daily_call_limit integer)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text; reserved numeric; settled numeric; calls integer;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_amount_usd is null or p_amount_usd < 0 or p_daily_limit_usd is null or p_daily_limit_usd < 0
     or p_daily_call_limit is null or p_daily_call_limit < 0 then
    raise exception 'invalid pairing reservation';
  end if;
  insert into translation_private.translation_spend_counters(scope_type, scope_key)
    values ('day', day_key) on conflict do nothing;
  select usd_reserved, usd_settled, pairing_calls into reserved, settled, calls
    from translation_private.translation_spend_counters
    where scope_type = 'day' and scope_key = day_key for update;
  -- Pairing is bounded twice: by the day's dollars and by the day's call count.
  if calls + 1 > p_daily_call_limit then
    return jsonb_build_object('status', 'call_limit_reached', 'pairing_calls', calls);
  end if;
  if settled + reserved + p_amount_usd > p_daily_limit_usd then
    return jsonb_build_object('status', 'cost_limit_reached', 'usd_reserved', reserved, 'usd_settled', settled);
  end if;
  update translation_private.translation_spend_counters
    set usd_reserved = usd_reserved + p_amount_usd, pairing_calls = pairing_calls + 1, updated_at = now()
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('status', 'reserved', 'pairing_calls', calls + 1);
end;
$$;

create or replace function public.m2_read_translation_spend()
returns jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text; reserved numeric := 0; settled numeric := 0; calls integer := 0;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  select usd_reserved, usd_settled, pairing_calls into reserved, settled, calls
    from translation_private.translation_spend_counters
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('usd_reserved', coalesce(reserved, 0), 'usd_settled', coalesce(settled, 0),
                            'pairing_calls', coalesce(calls, 0));
end;
$$;

create or replace function public.m2_record_exclusivity_decision(
  p_story_id text, p_display_language text, p_policy_id text, p_model text,
  p_outcome text, p_match_story_id text default null, p_retry_after timestamptz default null)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare current translation_private.exclusivity_decisions;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  -- A settled answer (exclusive or matched) is written once per policy. Only an
  -- UNDECIDED row may be rewritten, and each rewrite counts an attempt, so a
  -- provider that keeps answering badly is re-asked a bounded number of times
  -- rather than on every run for ever.
  insert into translation_private.exclusivity_decisions
    (story_id, display_language, policy_id, model, outcome, match_story_id, retry_after)
  values (p_story_id, p_display_language, p_policy_id, p_model, p_outcome, p_match_story_id, p_retry_after)
  on conflict (story_id, display_language, policy_id) do update
    set outcome = excluded.outcome,
        match_story_id = excluded.match_story_id,
        model = excluded.model,
        decided_at = case when public.m2_decision_is_settled(translation_private.exclusivity_decisions.outcome)
                          then translation_private.exclusivity_decisions.decided_at else now() end,
        attempts = translation_private.exclusivity_decisions.attempts + 1,
        retry_after = excluded.retry_after
    where not public.m2_decision_is_settled(translation_private.exclusivity_decisions.outcome)
  returning * into current;
  if current.story_id is null then
    select * into current from translation_private.exclusivity_decisions
      where story_id = p_story_id and display_language = p_display_language and policy_id = p_policy_id;
  end if;
  return jsonb_build_object('story_id', current.story_id, 'display_language', current.display_language,
    'policy_id', current.policy_id, 'decided_at', current.decided_at, 'model', current.model,
    'outcome', current.outcome, 'match_story_id', current.match_story_id,
    'attempts', current.attempts, 'retry_after', current.retry_after);
end;
$$;

-- Mark an exclusive decision as re-checked, so the recheck happens once rather
-- than on every run inside the window.
create or replace function public.m2_mark_exclusivity_rechecked(
  p_story_id text, p_display_language text, p_policy_id text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  update translation_private.exclusivity_decisions set rechecked_at = now()
    where story_id = p_story_id and display_language = p_display_language and policy_id = p_policy_id;
  return jsonb_build_object('status', 'rechecked');
end;
$$;

create or replace function public.m2_read_exclusivity_decisions(
  p_story_ids text[], p_display_language text, p_policy_id text)
returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_story_ids is null or array_length(p_story_ids, 1) > 20000 then
    raise exception 'invalid decision lookup';
  end if;
  -- Scoped to the policy that asked: a prompt or model upgrade must not inherit
  -- answers produced by different instructions.
  return query
  select jsonb_build_object('story_id', d.story_id, 'display_language', d.display_language,
                            'policy_id', d.policy_id, 'decided_at', d.decided_at, 'model', d.model,
                            'outcome', d.outcome, 'match_story_id', d.match_story_id,
                            'attempts', d.attempts, 'retry_after', d.retry_after,
                            'rechecked_at', d.rechecked_at)
  from translation_private.exclusivity_decisions d
  where d.story_id = any(p_story_ids)
    and d.display_language = p_display_language
    and d.policy_id = p_policy_id;
end;
$$;

revoke all on function public.m2_reserve_translation_spend(numeric, numeric),
  public.m2_settle_translation_spend(numeric, numeric, text),
  public.m2_release_translation_spend(numeric),
  public.m2_reserve_pairing_call(numeric, numeric, integer),
  public.m2_read_translation_spend(),
  public.m2_record_exclusivity_decision(text, text, text, text, text, text, timestamptz),
  public.m2_mark_exclusivity_rechecked(text, text, text),
  public.m2_read_exclusivity_decisions(text[], text, text) from public, anon, authenticated;
grant execute on function public.m2_reserve_translation_spend(numeric, numeric),
  public.m2_settle_translation_spend(numeric, numeric, text),
  public.m2_release_translation_spend(numeric),
  public.m2_reserve_pairing_call(numeric, numeric, integer),
  public.m2_read_translation_spend(),
  public.m2_record_exclusivity_decision(text, text, text, text, text, text, timestamptz),
  public.m2_mark_exclusivity_rechecked(text, text, text),
  public.m2_read_exclusivity_decisions(text[], text, text) to service_role;

commit;
