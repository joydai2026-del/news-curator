begin;

alter table public.feed_policy
  add column behavior_history_limit integer not null default 200
    check (behavior_history_limit between 1 and 1000);

-- Reuse the existing bounded action receipt. M1 responses remain unchanged;
-- the optional binding prevents a replay from becoming a new learned action.
alter table public.user_action_receipts
  add column behavior_request_digest text check (behavior_request_digest ~ '^[0-9a-f]{64}$'),
  add column behavior_history_generation bigint check (behavior_history_generation > 0),
  add column behavior_response jsonb check (jsonb_typeof(behavior_response)='object' and octet_length(behavior_response::text)<=8192),
  add constraint complete_behavior_receipt check (
    (behavior_request_digest is null and behavior_history_generation is null and behavior_response is null)
    or (behavior_request_digest is not null and behavior_history_generation is not null and behavior_response is not null));

create table public.user_behavior_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  learning_enabled boolean not null default false,
  provider_processing_enabled boolean not null default false,
  provider_policy_id text check (provider_policy_id is null or
    (provider_policy_id <> '' and octet_length(provider_policy_id) <= 512)),
  consent_revision bigint not null default 1 check (consent_revision > 0),
  updated_at timestamptz not null default now()
);

create table public.user_behavior_revisions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  latest_revision bigint not null default 0 check (latest_revision >= 0),
  history_generation bigint not null default 1 check (history_generation > 0)
);

create table public.user_behavior_events (
  user_id uuid not null references auth.users(id) on delete cascade,
  event_id text not null check (event_id ~ '^event:[0-9a-f]{64}$'),
  event_revision bigint not null check (event_revision > 0),
  schema_version integer not null check (schema_version = 1),
  actor_kind text not null check (actor_kind in ('human', 'agent')),
  event_type text not null check (event_type in (
    'read_more', 'save', 'open_original', 'search_query',
    'search_zero_results', 'search_result_click',
    'more_like_this', 'less_like_this'
  )),
  payload jsonb not null check (
    jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 32768
  ),
  request_digest char(64) not null check (request_digest ~ '^[0-9a-f]{64}$'),
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  primary key (user_id, event_id),
  unique (user_id, event_revision)
);
create index user_behavior_events_order_idx
  on public.user_behavior_events(user_id, event_revision desc);

create table public.user_behavior_profile_state (
  user_id uuid primary key references auth.users(id) on delete cascade,
  history_revision bigint not null default 0 check (history_revision >= 0),
  profile jsonb not null default '{}'::jsonb check (jsonb_typeof(profile) = 'object'),
  updated_at timestamptz not null default now()
);

alter table public.user_behavior_settings enable row level security;
alter table public.user_behavior_settings force row level security;
alter table public.user_behavior_revisions enable row level security;
alter table public.user_behavior_revisions force row level security;
alter table public.user_behavior_events enable row level security;
alter table public.user_behavior_events force row level security;
alter table public.user_behavior_profile_state enable row level security;
alter table public.user_behavior_profile_state force row level security;

create policy user_behavior_settings_owner on public.user_behavior_settings to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());
create policy user_behavior_revisions_owner on public.user_behavior_revisions to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());
create policy user_behavior_events_owner on public.user_behavior_events to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());
create policy user_behavior_profile_state_owner on public.user_behavior_profile_state to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());

create or replace function public.append_behavior_event(
  p_event_id text, p_event_type text,
  p_payload jsonb, p_occurred_at timestamptz, p_expected_history_generation bigint,
  p_schema_version integer default 1
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); caller_actor_kind text := coalesce(auth.jwt()->'app_metadata'->>'actor_kind','human');
  digest_value text; existing public.user_behavior_events%rowtype;
  assigned_revision bigint; current_generation bigint; allowed_keys text[];
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_event_id is null or p_event_id !~ '^event:[0-9a-f]{64}$'
     or caller_actor_kind not in ('human', 'agent')
     or p_event_type not in ('read_more', 'save', 'open_original', 'search_query',
       'search_zero_results', 'search_result_click', 'more_like_this', 'less_like_this')
     or jsonb_typeof(p_payload) is distinct from 'object'
     or octet_length(p_payload::text) > 32768
     or p_occurred_at is null or p_occurred_at > now() + interval '10 minutes'
     or p_schema_version is distinct from 1 then
    raise exception 'invalid behavior event';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':behavior', 0));
  select coalesce((select history_generation from public.user_behavior_revisions
    where user_id=caller),1) into current_generation;
  if p_expected_history_generation is null or p_expected_history_generation <> current_generation then
    raise exception 'history generation conflict'; end if;
  if not coalesce((select learning_enabled from public.user_behavior_settings
    where user_id = caller), false) then raise exception 'learning consent required' using errcode = '42501'; end if;
  allowed_keys := case p_event_type
    when 'read_more' then array['story_id','surface']
    when 'save' then array['story_id','saved','surface']
    when 'open_original' then array['story_id','exposure_event_id','surface']
    when 'search_query' then array['query','surface']
    when 'search_zero_results' then array['query','result_count','surface']
    when 'search_result_click' then array['query','story_id','result_position','surface']
    else array['story_id','topic_id','surface'] end;
  if exists (select 1 from jsonb_object_keys(p_payload) key where not key = any(allowed_keys))
     or p_payload->>'surface' is null or p_payload->>'surface' = ''
     or octet_length(p_payload->>'surface') > 128
     or ((p_event_type in ('read_more','save','open_original','search_result_click','more_like_this','less_like_this'))
       and coalesce(p_payload->>'story_id','') !~ '^story:[0-9a-f]{64}$')
     or ((p_event_type in ('search_query','search_zero_results','search_result_click'))
       and (coalesce(p_payload->>'query','') = '' or octet_length(p_payload->>'query') > 4000))
     or (p_event_type = 'save' and jsonb_typeof(p_payload->'saved') is distinct from 'boolean')
     or (p_event_type = 'search_zero_results' and (jsonb_typeof(p_payload->'result_count') is distinct from 'number'
       or p_payload->>'result_count' is distinct from '0'))
     or (p_event_type = 'search_result_click' and (jsonb_typeof(p_payload->'result_position') is distinct from 'number'
       or (p_payload->>'result_position') !~ '^[1-9][0-9]*$')) then
    raise exception 'invalid behavior event payload';
  end if;
  digest_value := encode(extensions.digest(convert_to(jsonb_build_object(
    'actor_kind', caller_actor_kind, 'event_type', p_event_type, 'payload', p_payload,
    'occurred_at', p_occurred_at, 'schema_version', p_schema_version)::text, 'UTF8'), 'sha256'), 'hex');
  select * into existing from public.user_behavior_events
    where user_id = caller and event_id = p_event_id;
  if found then
    if existing.request_digest <> digest_value then raise exception 'event replay mismatch'; end if;
    return jsonb_build_object('status','replayed','event_id',existing.event_id,
      'event_revision',existing.event_revision,'recorded_at',existing.recorded_at);
  end if;
  insert into public.user_behavior_revisions(user_id, latest_revision) values (caller, 1)
  on conflict (user_id) do update set latest_revision = public.user_behavior_revisions.latest_revision + 1
  returning latest_revision into assigned_revision;
  insert into public.user_behavior_events(user_id,event_id,event_revision,schema_version,
    actor_kind,event_type,payload,request_digest,occurred_at)
  values (caller,p_event_id,assigned_revision,p_schema_version,caller_actor_kind,p_event_type,
    p_payload,digest_value,p_occurred_at);
  return jsonb_build_object('status','recorded','event_id',p_event_id,
    'event_revision',assigned_revision);
end;
$$;

create or replace function public.m2_history_snapshot(p_limit integer default null)
returns jsonb language sql stable security definer set search_path = pg_catalog, public as $$
  with settings as (
    select fp.behavior_history_limit, coalesce(bs.learning_enabled, false) learning_enabled,
      coalesce(bs.provider_processing_enabled, false) provider_processing_enabled,
      bs.provider_policy_id, coalesce(bs.consent_revision, 0) consent_revision
    from public.feed_policy fp left join public.user_behavior_settings bs on bs.user_id = auth.uid()
    where fp.singleton and auth.uid() is not null
  ), bounded as (
    select least(coalesce(p_limit, behavior_history_limit), behavior_history_limit) take
    from settings where learning_enabled and coalesce(p_limit, behavior_history_limit) > 0
  ), selected as (
    select e.*, s.title as story_title, s.summary as story_summary, src.source_id
    from public.user_behavior_events e cross join bounded b
    left join public.canonical_stories s on s.story_id = e.payload->>'story_id'
    left join lateral (
      select cm.source_id from public.coverage_mentions cm
      where cm.story_id = e.payload->>'story_id'
      order by cm.mentioned_at, cm.source_id limit 1
    ) src on true
    where e.user_id = auth.uid() order by e.event_revision desc limit (select take from bounded)
  ) select jsonb_build_object(
    'history_revision', coalesce((select latest_revision from public.user_behavior_revisions where user_id=auth.uid()),0),
    'server_commit_revision', coalesce((select latest_revision from public.user_behavior_revisions where user_id=auth.uid()),0),
    'included_history_revision', coalesce((select max(event_revision) from selected),0),
    'learning_enabled', coalesce((select learning_enabled from settings),false),
    'history_generation', coalesce((select history_generation from public.user_behavior_revisions where user_id=auth.uid()),1),
    'consent_revision', coalesce((select consent_revision from settings),0),
    'provider_processing_enabled', coalesce((select provider_processing_enabled from settings),false),
    'provider_policy_id', (select provider_policy_id from settings),
    'newest_event_id', (select event_id from selected order by event_revision desc limit 1),
    'events', coalesce((select jsonb_agg(jsonb_build_object('event_id',event_id,
      'event_revision',event_revision,'schema_version',schema_version,'actor_kind',actor_kind,
      'event_type',event_type,'payload',payload,'occurred_at',occurred_at,
      'story_title',story_title,'story_summary',story_summary,'source_id',source_id)
      order by event_revision) from selected), '[]'::jsonb))
$$;

create or replace function public.set_behavior_consent(
  p_learning_enabled boolean,p_provider_processing_enabled boolean,p_provider_policy_id text
)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); next_revision bigint; previous public.user_behavior_settings%rowtype;
  revoked boolean;
begin
  if caller is null then raise exception 'authentication required' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':behavior', 0));
  if p_learning_enabled is null or p_provider_processing_enabled is null
     or (p_provider_processing_enabled and (p_provider_policy_id is null or p_provider_policy_id=''
       or octet_length(p_provider_policy_id)>512))
     or (not p_provider_processing_enabled and p_provider_policy_id is not null) then
    raise exception 'invalid consent value'; end if;
  select * into previous from public.user_behavior_settings where user_id=caller;
  revoked := not p_learning_enabled
    or (coalesce(previous.provider_processing_enabled,false) and
      (not p_provider_processing_enabled or previous.provider_policy_id is distinct from p_provider_policy_id));
  if revoked then
    -- Withdrawal removes derived personalization, not raw locally retained
    -- events. Only clear_behavior_history is the owner's deletion operation.
    perform pg_advisory_xact_lock(hashtextextended('private-discovery:'||caller::text,0));
    delete from public.user_behavior_profile_state where user_id=caller;
    delete from public.user_story_interests where user_id=caller;
    -- Keep key/digest tombstones so a late plain M1 retry cannot recreate the
    -- erased interest. Remove the learned signal and event response themselves.
    update public.user_action_receipts set response=jsonb_build_object('status','conflict','revision',0),
      behavior_response=case when behavior_response is not null then jsonb_build_object('status','conflict','revision',0) end
      where user_id=caller and operation='set_story_interest';
    insert into public.user_behavior_revisions(user_id,latest_revision,history_generation) values(caller,0,2)
    on conflict(user_id) do update set history_generation=public.user_behavior_revisions.history_generation+1;
  end if;
  insert into public.user_behavior_settings(user_id,learning_enabled,
    provider_processing_enabled,provider_policy_id)
  values(caller,p_learning_enabled,p_provider_processing_enabled,p_provider_policy_id)
  on conflict(user_id) do update set learning_enabled=excluded.learning_enabled,
    provider_processing_enabled=excluded.provider_processing_enabled,
    provider_policy_id=excluded.provider_policy_id,
    consent_revision=public.user_behavior_settings.consent_revision+1,updated_at=now()
  returning consent_revision into next_revision;
  return jsonb_build_object('learning_enabled',p_learning_enabled,
    'provider_processing_enabled',p_provider_processing_enabled,
    'provider_policy_id',p_provider_policy_id,'consent_revision',next_revision);
end;
$$;

create or replace function public.clear_behavior_history()
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); event_count bigint; profile_count bigint;
begin
  if caller is null then raise exception 'authentication required' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':behavior', 0));
  perform pg_advisory_xact_lock(hashtextextended('private-discovery:'||caller::text,0));
  delete from public.user_behavior_events where user_id=caller; get diagnostics event_count=row_count;
  delete from public.user_behavior_profile_state where user_id=caller; get diagnostics profile_count=row_count;
  delete from public.user_story_interests where user_id=caller;
  update public.user_action_receipts set response=jsonb_build_object('status','conflict','revision',0),
    behavior_response=case when behavior_response is not null then jsonb_build_object('status','conflict','revision',0) end
    where user_id=caller and operation='set_story_interest';
  insert into public.user_behavior_revisions(user_id,latest_revision,history_generation) values(caller,0,2)
  on conflict(user_id) do update set latest_revision=0,
    history_generation=public.user_behavior_revisions.history_generation+1;
  return jsonb_build_object('events_deleted',event_count,'profiles_deleted',profile_count,
    'history_revision',0,'history_generation',(select history_generation
      from public.user_behavior_revisions where user_id=caller));
end;
$$;

create or replace function public.set_story_state_with_event(
  p_story_id text,p_read boolean,p_saved boolean,p_expected_revision bigint,p_idempotency_key text,
  p_event_id text,p_event_type text,p_surface text,p_occurred_at timestamptz,p_expected_history_generation bigint
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare state_result jsonb; event_result jsonb; answer jsonb; caller uuid := auth.uid();
  current_generation bigint; request_hash text; existing public.user_action_receipts%rowtype;
begin
  if p_event_type is null or p_event_type not in ('read_more','save') then raise exception 'invalid state event type'; end if;
  if auth.uid() is null then raise exception 'authentication required' using errcode='42501'; end if;
  if p_event_id is null or p_event_id !~ '^event:[0-9a-f]{64}$'
     or p_surface is null or p_surface='' or octet_length(p_surface)>128
     or p_occurred_at is null or p_occurred_at>now()+interval '10 minutes' then
    raise exception 'invalid combined event identity'; end if;
  perform pg_advisory_xact_lock(hashtextextended(auth.uid()::text || ':behavior', 0));
  select coalesce((select history_generation from public.user_behavior_revisions where user_id=caller),1) into current_generation;
  if p_expected_history_generation is distinct from current_generation then raise exception 'history generation conflict'; end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':state:' || p_story_id,0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key,0));
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'operation','set_story_state','story_id',p_story_id,'read',p_read,'saved',p_saved,
    'expected_revision',p_expected_revision,'event_id',p_event_id,'event_type',p_event_type,
    'surface',p_surface,'occurred_at',p_occurred_at,'history_generation',p_expected_history_generation,
    'actor_kind',coalesce(auth.jwt()->'app_metadata'->>'actor_kind','human'))::text,'UTF8'),'sha256'),'hex');
  select * into existing from public.user_action_receipts where user_id=caller and idempotency_key=p_idempotency_key;
  if found then
    if existing.behavior_request_digest is distinct from request_hash then raise exception 'combined action replay mismatch'; end if;
    return existing.behavior_response;
  end if;
  state_result := public.set_story_state(p_story_id,p_read,p_saved,p_expected_revision,p_idempotency_key);
  if state_result->>'status' = 'updated' and coalesce((select learning_enabled from public.user_behavior_settings where user_id=auth.uid()),false) then
    event_result := public.append_behavior_event(p_event_id,p_event_type,
      jsonb_build_object('story_id',p_story_id,'saved',p_saved,'surface',p_surface)-
        case when p_event_type='read_more' then 'saved' else '' end,p_occurred_at,p_expected_history_generation,1);
  elsif state_result->>'status' = 'updated' then event_result := jsonb_build_object('status','learning_disabled'); end if;
  answer := state_result || case when event_result is null then '{}'::jsonb else jsonb_build_object('behavior_event',event_result) end;
  update public.user_action_receipts set behavior_request_digest=request_hash,
    behavior_history_generation=current_generation,behavior_response=answer
    where user_id=caller and idempotency_key=p_idempotency_key;
  return answer;
end;
$$;

create or replace function public.set_story_interest_with_event(
  p_story_id text,p_topic_id text,p_signal text,p_expected_revision bigint,p_idempotency_key text,
  p_event_id text,p_surface text,p_occurred_at timestamptz,p_expected_history_generation bigint
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare state_result jsonb; event_result jsonb; mapped_type text; answer jsonb; caller uuid := auth.uid();
  current_generation bigint; request_hash text; existing public.user_action_receipts%rowtype;
begin
  mapped_type := case p_signal when 'more_like' then 'more_like_this' when 'less_like' then 'less_like_this' end;
  if mapped_type is null then raise exception 'invalid interest event type'; end if;
  if auth.uid() is null then raise exception 'authentication required' using errcode='42501'; end if;
  if p_event_id is null or p_event_id !~ '^event:[0-9a-f]{64}$'
     or p_surface is null or p_surface='' or octet_length(p_surface)>128
     or p_occurred_at is null or p_occurred_at>now()+interval '10 minutes' then
    raise exception 'invalid combined event identity'; end if;
  perform pg_advisory_xact_lock(hashtextextended(auth.uid()::text || ':behavior', 0));
  select coalesce((select history_generation from public.user_behavior_revisions where user_id=caller),1) into current_generation;
  if p_expected_history_generation is distinct from current_generation then raise exception 'history generation conflict'; end if;
  perform pg_advisory_xact_lock(hashtextextended('private-discovery:'||caller::text,0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':interest:' || p_story_id || ':' || p_topic_id,0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key,0));
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'operation','set_story_interest','story_id',p_story_id,'topic_id',p_topic_id,'signal',p_signal,
    'expected_revision',p_expected_revision,'event_id',p_event_id,'surface',p_surface,
    'occurred_at',p_occurred_at,'history_generation',p_expected_history_generation,
    'actor_kind',coalesce(auth.jwt()->'app_metadata'->>'actor_kind','human'))::text,'UTF8'),'sha256'),'hex');
  select * into existing from public.user_action_receipts where user_id=caller and idempotency_key=p_idempotency_key;
  if found then
    if existing.behavior_request_digest is distinct from request_hash then raise exception 'combined action replay mismatch'; end if;
    return existing.behavior_response;
  end if;
  state_result := public.set_story_interest(p_story_id,p_topic_id,p_signal,p_expected_revision,p_idempotency_key);
  if state_result->>'status' = 'updated' and coalesce((select learning_enabled from public.user_behavior_settings where user_id=auth.uid()),false) then
    event_result := public.append_behavior_event(p_event_id,mapped_type,
      jsonb_build_object('story_id',p_story_id,'topic_id',p_topic_id,'surface',p_surface),p_occurred_at,p_expected_history_generation,1);
  elsif state_result->>'status' = 'updated' then event_result := jsonb_build_object('status','learning_disabled'); end if;
  answer := state_result || case when event_result is null then '{}'::jsonb else jsonb_build_object('behavior_event',event_result) end;
  update public.user_action_receipts set behavior_request_digest=request_hash,
    behavior_history_generation=current_generation,behavior_response=answer
    where user_id=caller and idempotency_key=p_idempotency_key;
  return answer;
end;
$$;

revoke all on public.user_behavior_settings, public.user_behavior_revisions,
  public.user_behavior_events, public.user_behavior_profile_state from public,anon,authenticated;
revoke execute on function public.append_behavior_event(text,text,jsonb,timestamptz,bigint,integer) from public,anon,authenticated;
revoke execute on function public.m2_history_snapshot(integer) from public,anon,authenticated;
revoke execute on function public.set_behavior_consent(boolean,boolean,text) from public,anon,authenticated;
revoke execute on function public.clear_behavior_history() from public,anon,authenticated;
revoke execute on function public.set_story_state_with_event(text,boolean,boolean,bigint,text,text,text,text,timestamptz,bigint) from public,anon,authenticated;
revoke execute on function public.set_story_interest_with_event(text,text,text,bigint,text,text,text,timestamptz,bigint) from public,anon,authenticated;
grant execute on function public.append_behavior_event(text,text,jsonb,timestamptz,bigint,integer) to authenticated;
grant execute on function public.m2_history_snapshot(integer) to authenticated;
grant execute on function public.set_behavior_consent(boolean,boolean,text) to authenticated;
grant execute on function public.clear_behavior_history() to authenticated;
grant execute on function public.set_story_state_with_event(text,boolean,boolean,bigint,text,text,text,text,timestamptz,bigint) to authenticated;
grant execute on function public.set_story_interest_with_event(text,text,text,bigint,text,text,text,timestamptz,bigint) to authenticated;

commit;
