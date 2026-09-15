-- Durable owner-scoped reservations and short-lived frozen ranking orders.
create table public.m2_ranker_daily_budget (
  user_id uuid not null references auth.users(id) on delete cascade,
  budget_date date not null,
  spent_usd numeric(14,8) not null default 0 check (spent_usd >= 0),
  reserved_usd numeric(14,8) not null default 0 check (reserved_usd >= 0),
  primary key (user_id, budget_date)
);
create table public.m2_ranker_reservations (
  request_id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  budget_date date not null,
  reserved_usd numeric(14,8) not null check (reserved_usd > 0),
  actual_usd numeric(14,8) check (actual_usd >= 0),
  status text not null check (status in ('reserved','settled','released','failed')),
  created_at timestamptz not null default now(), settled_at timestamptz
);
create table public.m2_frozen_rankings (
  frozen_order_id uuid primary key default gen_random_uuid(), request_id uuid not null unique,
  user_id uuid not null references auth.users(id) on delete cascade,
  bindings jsonb not null, cards jsonb not null check (jsonb_typeof(cards)='array'),
  page_size integer not null check (page_size between 1 and 25),
  expires_at timestamptz not null, created_at timestamptz not null default now()
);
alter table public.m2_ranker_daily_budget enable row level security;
alter table public.m2_ranker_daily_budget force row level security;
alter table public.m2_ranker_reservations enable row level security;
alter table public.m2_ranker_reservations force row level security;
alter table public.m2_frozen_rankings enable row level security;
alter table public.m2_frozen_rankings force row level security;
revoke all on public.m2_ranker_daily_budget, public.m2_ranker_reservations, public.m2_frozen_rankings from public, anon, authenticated;
grant select,insert,update,delete on public.m2_ranker_daily_budget, public.m2_ranker_reservations, public.m2_frozen_rankings to service_role;

create or replace function public.m2_reserve_ranker_budget(
  p_user_id uuid, p_request_id uuid, p_amount_usd numeric, p_daily_limit_usd numeric
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare today date := (statement_timestamp() at time zone 'utc')::date; accepted boolean := false;
begin
  if p_amount_usd <= 0 or p_daily_limit_usd <= 0 or p_amount_usd > p_daily_limit_usd then return false; end if;
  insert into public.m2_ranker_daily_budget(user_id,budget_date) values(p_user_id,today) on conflict do nothing;
  update public.m2_ranker_daily_budget set reserved_usd=reserved_usd+p_amount_usd
   where user_id=p_user_id and budget_date=today and spent_usd+reserved_usd+p_amount_usd <= p_daily_limit_usd
   returning true into accepted;
  if coalesce(accepted,false) then
    insert into public.m2_ranker_reservations(request_id,user_id,budget_date,reserved_usd,status)
      values(p_request_id,p_user_id,today,p_amount_usd,'reserved');
  end if;
  return coalesce(accepted,false);
end; $$;

create or replace function public.m2_settle_ranker_budget(
  p_user_id uuid, p_request_id uuid, p_actual_usd numeric, p_status text
) returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare reservation public.m2_ranker_reservations%rowtype;
begin
  if p_actual_usd < 0 or p_status not in ('settled','released','failed') then raise exception 'invalid settlement'; end if;
  select * into reservation from public.m2_ranker_reservations where request_id=p_request_id and user_id=p_user_id for update;
  if not found then raise exception 'invalid reservation'; end if;
  if reservation.status = p_status and reservation.actual_usd is not distinct from p_actual_usd then return; end if;
  if reservation.status <> 'reserved' or p_actual_usd > reservation.reserved_usd then raise exception 'invalid reservation'; end if;
  update public.m2_ranker_daily_budget set reserved_usd=reserved_usd-reservation.reserved_usd,
    spent_usd=spent_usd+p_actual_usd where user_id=p_user_id and budget_date=reservation.budget_date;
  update public.m2_ranker_reservations set actual_usd=p_actual_usd,status=p_status,settled_at=statement_timestamp()
    where request_id=p_request_id;
end; $$;
revoke all on function public.m2_reserve_ranker_budget(uuid,uuid,numeric,numeric), public.m2_settle_ranker_budget(uuid,uuid,numeric,text) from public,anon,authenticated;
grant execute on function public.m2_reserve_ranker_budget(uuid,uuid,numeric,numeric), public.m2_settle_ranker_budget(uuid,uuid,numeric,text) to service_role;

create or replace function public.m2_owner_story_states(p_story_ids text[])
returns setof jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare caller uuid := auth.uid();
begin
  if caller is null then raise exception 'authentication required' using errcode='42501'; end if;
  if p_story_ids is null or cardinality(p_story_ids) > 200 or exists (
    select 1 from unnest(p_story_ids) story_id where story_id !~ '^story:[0-9a-f]{64}$'
  ) then raise exception 'invalid story ids'; end if;
  return query select jsonb_build_object('story_id', ids.story_id, 'read_at', state.read_at,
    'saved_at', state.saved_at, 'state_revision', coalesce(state.revision,0),
    'interests', coalesce(interests.rows,'[]'::jsonb))
  from unnest(p_story_ids) ids(story_id)
  left join public.user_story_state state on state.user_id=caller and state.story_id=ids.story_id
  left join lateral (select jsonb_agg(jsonb_build_object('topic_id',topic_id,'signal',signal,
    'revision',revision) order by topic_id) rows from public.user_story_interests
    where user_id=caller and story_id=ids.story_id) interests on true;
end; $$;
revoke all on function public.m2_owner_story_states(text[]) from public,anon;
grant execute on function public.m2_owner_story_states(text[]) to authenticated;

create or replace function public.m2_clear_frozen_rankings_on_generation()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  -- First clear may INSERT generation 2 before any behavior event exists.
  if (tg_op = 'INSERT' and new.history_generation > 1)
     or (tg_op = 'UPDATE' and new.history_generation <> old.history_generation) then
    delete from public.m2_frozen_rankings where user_id=new.user_id;
  end if;
  return new;
end; $$;
create trigger m2_clear_frozen_rankings_after_generation
after insert or update of history_generation on public.user_behavior_revisions
for each row execute function public.m2_clear_frozen_rankings_on_generation();
revoke all on function public.m2_clear_frozen_rankings_on_generation() from public,anon,authenticated;

create or replace function public.m2_clear_frozen_rankings_on_consent()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  -- The consent RPC holds the owner behavior lock before changing settings.
  delete from public.m2_frozen_rankings where user_id=new.user_id;
  return new;
end; $$;
create trigger m2_clear_frozen_rankings_after_consent
after insert or update of consent_revision on public.user_behavior_settings
for each row execute function public.m2_clear_frozen_rankings_on_consent();
revoke all on function public.m2_clear_frozen_rankings_on_consent() from public,anon,authenticated;

create or replace function public.m2_validate_frozen_ranking_epoch()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare current_generation bigint; current_revision bigint; settings public.user_behavior_settings%rowtype;
begin
  -- Close the check-then-insert race with reset/revocation. A late provider
  -- result waits behind the same lock and cannot recreate erased private cards.
  perform pg_advisory_xact_lock(hashtextextended(new.user_id::text || ':behavior',0));
  select coalesce((select history_generation from public.user_behavior_revisions where user_id=new.user_id),1),
    coalesce((select latest_revision from public.user_behavior_revisions where user_id=new.user_id),0)
    into current_generation,current_revision;
  select * into settings from public.user_behavior_settings where user_id=new.user_id;
  if new.bindings->'history_generation' is distinct from to_jsonb(current_generation)
     or new.bindings->'server_commit_revision' is distinct from to_jsonb(current_revision)
     or new.bindings->'consent_revision' is distinct from to_jsonb(coalesce(settings.consent_revision,0)) then
    raise exception 'stale frozen ranking bindings'; end if;
  if new.bindings->>'result_mode'='model' and not
    (coalesce(settings.learning_enabled,false) and coalesce(settings.provider_processing_enabled,false)) then
    raise exception 'model processing consent required'; end if;
  return new;
end; $$;
create trigger m2_validate_frozen_ranking_before_write
before insert or update on public.m2_frozen_rankings
for each row execute function public.m2_validate_frozen_ranking_epoch();
revoke all on function public.m2_validate_frozen_ranking_epoch() from public,anon,authenticated;
