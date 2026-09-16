begin;

-- Two facts the hourly job must remember ACROSS runs, because an in-memory
-- ledger resets twelve times an hour and is therefore not a daily cap at all.
--
-- 1. Dollars reserved and settled per UTC day, mirroring the character
--    counters that already live beside them.
-- 2. One exclusivity decision per story, so the model is asked ONCE and every
--    later run reuses the answer rather than paying for it again.

create table translation_private.translation_spend_counters (
  scope_type text not null check (scope_type in ('day', 'month')),
  scope_key text not null check (octet_length(scope_key) between 1 and 256),
  usd_reserved numeric(12, 6) not null default 0 check (usd_reserved >= 0),
  usd_settled numeric(12, 6) not null default 0 check (usd_settled >= 0),
  updated_at timestamptz not null default clock_timestamp(),
  primary key (scope_type, scope_key)
);

create table translation_private.exclusivity_decisions (
  story_id text primary key check (story_id ~ '^story:[0-9a-f]{64}$'),
  decided_at timestamptz not null default clock_timestamp(),
  model text not null check (model <> '' and octet_length(model) <= 256),
  policy_id text not null check (policy_id ~ '^[A-Za-z0-9._-]{1,64}$'),
  -- NULL means the model said no display-language outlet carried this event.
  match_story_id text check (match_story_id is null or match_story_id ~ '^story:[0-9a-f]{64}$'),
  constraint exclusivity_decision_is_not_self check (match_story_id is null or match_story_id <> story_id)
);
create index exclusivity_decisions_decided_idx on translation_private.exclusivity_decisions(decided_at desc);

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
create or replace function public.m2_settle_translation_spend(p_reserved_usd numeric, p_settled_usd numeric)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_reserved_usd is null or p_reserved_usd < 0 or p_settled_usd is null or p_settled_usd < 0 then
    raise exception 'invalid translation settlement';
  end if;
  update translation_private.translation_spend_counters
    set usd_reserved = greatest(0, usd_reserved - p_reserved_usd),
        usd_settled = usd_settled + p_settled_usd, updated_at = now()
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('status', 'settled');
end;
$$;

create or replace function public.m2_read_translation_spend()
returns jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
declare day_key text := (now() at time zone 'utc')::date::text; reserved numeric := 0; settled numeric := 0;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  select usd_reserved, usd_settled into reserved, settled
    from translation_private.translation_spend_counters
    where scope_type = 'day' and scope_key = day_key;
  return jsonb_build_object('usd_reserved', coalesce(reserved, 0), 'usd_settled', coalesce(settled, 0));
end;
$$;

create or replace function public.m2_record_exclusivity_decision(
  p_story_id text, p_model text, p_policy_id text, p_match_story_id text default null)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  -- A story is decided ONCE. A replay keeps the original decision and its date,
  -- so the group id can never move under a story that is already published.
  insert into translation_private.exclusivity_decisions(story_id, model, policy_id, match_story_id)
  values (p_story_id, p_model, p_policy_id, p_match_story_id)
  on conflict (story_id) do nothing;
  return (select jsonb_build_object('story_id', story_id, 'decided_at', decided_at, 'model', model,
                                    'policy_id', policy_id, 'match_story_id', match_story_id)
          from translation_private.exclusivity_decisions where story_id = p_story_id);
end;
$$;

create or replace function public.m2_read_exclusivity_decisions(p_story_ids text[])
returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if p_story_ids is null or array_length(p_story_ids, 1) > 20000 then
    raise exception 'invalid decision lookup';
  end if;
  return query
  select jsonb_build_object('story_id', d.story_id, 'decided_at', d.decided_at, 'model', d.model,
                            'policy_id', d.policy_id, 'match_story_id', d.match_story_id)
  from translation_private.exclusivity_decisions d
  where d.story_id = any(p_story_ids);
end;
$$;

revoke all on function public.m2_reserve_translation_spend(numeric, numeric),
  public.m2_settle_translation_spend(numeric, numeric),
  public.m2_read_translation_spend(),
  public.m2_record_exclusivity_decision(text, text, text, text),
  public.m2_read_exclusivity_decisions(text[]) from public, anon, authenticated;
grant execute on function public.m2_reserve_translation_spend(numeric, numeric),
  public.m2_settle_translation_spend(numeric, numeric),
  public.m2_read_translation_spend(),
  public.m2_record_exclusivity_decision(text, text, text, text),
  public.m2_read_exclusivity_decisions(text[]) to service_role;

commit;
