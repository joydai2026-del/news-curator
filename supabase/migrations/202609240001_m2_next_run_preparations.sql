begin;

-- A model order is prepared for a later reading run. It is never a cursor or a
-- mutable order for the run that initiated it. Only the ranker service role can
-- read this private request, which may contain owner history and query text.
create table public.m2_prepared_orders (
  job_id uuid primary key default gen_random_uuid(),
  request_id uuid not null unique,
  user_id uuid not null references auth.users(id) on delete cascade,
  source_run_id uuid not null references public.m2_reading_runs(run_id) on delete cascade,
  eligibility_key text not null check (eligibility_key ~ '^[0-9a-f]{64}$'),
  policy_digest text not null check (policy_digest ~ '^[0-9a-f]{64}$'),
  history_generation bigint not null check (history_generation > 0),
  consent_revision bigint not null check (consent_revision >= 0),
  behavior_revision bigint not null check (behavior_revision >= 0),
  provider_policy_id text not null,
  request_payload jsonb not null check (jsonb_typeof(request_payload) = 'object'),
  status text not null default 'pending' check (status in ('pending','claimed','attempting','ready','failed','consumed')),
  claim_token uuid,
  ranked_candidate_ids jsonb,
  consumed_by_run_id uuid,
  created_at timestamptz not null default now(),
  claimed_at timestamptz,
  expires_at timestamptz not null,
  unique (source_run_id, eligibility_key)
);
create index m2_prepared_orders_pending_idx on public.m2_prepared_orders(created_at)
  where status = 'pending';
create index m2_prepared_orders_expired_idx on public.m2_prepared_orders(expires_at);
create index m2_prepared_orders_ready_idx on public.m2_prepared_orders(user_id, eligibility_key, created_at desc)
  where status = 'ready';
alter table public.m2_prepared_orders enable row level security;
alter table public.m2_prepared_orders force row level security;
revoke all on public.m2_prepared_orders from public, anon, authenticated;
grant select, insert, update, delete on public.m2_prepared_orders to service_role;
create policy m2_prepared_orders_service on public.m2_prepared_orders to service_role
  using (true) with check (true);

-- A prepared model is a snapshot. Later positive reading activity does not
-- invalidate it, but a negative preference or unexplained revision gap does.
-- Take the owner's behavior lock here too, so a standalone post-consume
-- check serializes with concurrent feedback and consent writes.
create or replace function public.m2_prepared_history_is_compatible(
  p_user_id uuid,p_history_generation bigint,p_behavior_revision bigint
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare live_generation bigint; live_revision bigint; event_count bigint; positive_count bigint;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_user_id is null or p_history_generation is null or p_behavior_revision is null then
    return false;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior',0));
  select history_generation,latest_revision into live_generation,live_revision
    from public.user_behavior_revisions where user_id=p_user_id;
  if not found then live_generation:=1; live_revision:=0; end if;
  if live_generation is distinct from p_history_generation
     or live_revision < p_behavior_revision then return false; end if;
  if live_revision = p_behavior_revision then return true; end if;
  select count(*), count(*) filter (
      where event_type in ('read_more','open_original','search_query',
                           'search_zero_results','search_result_click','more_like_this')
         or (event_type='save' and payload->'saved'='true'::jsonb))
    into event_count,positive_count from public.user_behavior_events
    where user_id=p_user_id and event_revision>p_behavior_revision
      and event_revision<=live_revision;
  return event_count=live_revision-p_behavior_revision
     and positive_count=event_count;
end; $$;

create or replace function public.m2_enqueue_prepared_order(
  p_user_id uuid, p_source_run_id uuid, p_eligibility_key text,
  p_policy_digest text, p_history_generation bigint, p_consent_revision bigint,
  p_behavior_revision bigint,
  p_provider_policy_id text, p_request_id uuid, p_request_payload jsonb,
  p_ttl_seconds integer
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare queued uuid;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_user_id is null or p_source_run_id is null or p_provider_policy_id is null
     or p_history_generation is null or p_history_generation < 1
     or p_consent_revision is null or p_consent_revision < 0
     or p_behavior_revision is null or p_behavior_revision < 0
     or p_eligibility_key !~ '^[0-9a-f]{64}$' or p_policy_digest !~ '^[0-9a-f]{64}$'
     or p_request_id is null or p_ttl_seconds not between 3600 and 86400
     or jsonb_typeof(p_request_payload) <> 'object'
     or octet_length(p_request_payload::text) > 262144
     or p_request_payload->>'request_id' is distinct from p_request_id::text
     or p_request_payload->'owner'->>'user_id' is distinct from p_user_id::text then
    raise exception 'invalid prepared order';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior',0));
  if not exists (select 1 from public.m2_reading_runs r
                 where r.run_id=p_source_run_id and r.user_id=p_user_id and r.closed_at is null)
     or not exists (select 1 from public.user_behavior_settings s
                    where s.user_id=p_user_id and s.learning_enabled and s.provider_processing_enabled
                      and s.provider_policy_id=p_provider_policy_id
                      and s.consent_revision=p_consent_revision)
     or not public.m2_prepared_history_is_compatible(
                  p_user_id,p_history_generation,p_behavior_revision) then
    return false;
  end if;
  insert into public.m2_prepared_orders(request_id,user_id,source_run_id,eligibility_key,
    policy_digest,history_generation,consent_revision,behavior_revision,provider_policy_id,request_payload,expires_at)
    values(p_request_id,p_user_id,p_source_run_id,p_eligibility_key,p_policy_digest,
      p_history_generation,p_consent_revision,p_behavior_revision,p_provider_policy_id,p_request_payload,
      now()+make_interval(secs=>p_ttl_seconds))
    on conflict (source_run_id,eligibility_key) do nothing returning job_id into queued;
  return queued is not null;
end; $$;

-- Expiration removes queued private request data and deletes expired model
-- inferences. The independent spend ledger and reservations remain for cost
-- reconciliation. Each call touches at most p_limit jobs; a worker calls it
-- on each claim, and operations may also call it alone.
create or replace function public.m2_scrub_expired_prepared_orders(p_limit integer default 100)
returns integer language plpgsql security definer set search_path=pg_catalog,public as $$
declare selected_ids uuid[]; deleted_count integer := 0; scrubbed_count integer := 0;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_limit is null or p_limit not between 1 and 1000 then
    raise exception 'invalid scrub limit';
  end if;
  select array_agg(job_id) into selected_ids from (
    select job_id from public.m2_prepared_orders
      where expires_at <= now()
      order by expires_at, job_id for update skip locked limit p_limit
  ) expired;
  if selected_ids is null then return 0; end if;
  delete from public.m2_prepared_orders
    where job_id=any(selected_ids) and status in ('ready','consumed','failed');
  get diagnostics deleted_count = row_count;
  update public.m2_prepared_orders
    set status='failed', request_payload='{}'::jsonb, claim_token=null
    where job_id=any(selected_ids) and status in ('pending','claimed','attempting');
  get diagnostics scrubbed_count = row_count;
  return deleted_count + scrubbed_count;
end; $$;

-- Claimed jobs are never automatically re-claimed. A crashed or ambiguous
-- provider attempt may already have charged, so a second attempt is unsafe.
create or replace function public.m2_claim_prepared_order(p_policy_digest text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare job public.m2_prepared_orders%rowtype; token uuid := gen_random_uuid();
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  perform public.m2_scrub_expired_prepared_orders(100);
  select * into job from public.m2_prepared_orders
    where status='pending' and policy_digest=p_policy_digest and expires_at>now()
    order by created_at for update skip locked limit 1;
  if not found then return null; end if;
  update public.m2_prepared_orders set status='claimed', claim_token=token, claimed_at=now()
    where job_id=job.job_id;
  return jsonb_build_object('job_id',job.job_id,'request_id',job.request_id,
    'user_id',job.user_id,'claim_token',token,'request_payload',job.request_payload);
end; $$;

-- The one-shot worker prepares the prompt before this call. Reservation and
-- claim validation happen under the owner behavior lock before any paid call.
create or replace function public.m2_reserve_prepared_budget(
  p_job_id uuid, p_claim_token uuid, p_amount_usd numeric, p_daily_limit_usd numeric
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare job public.m2_prepared_orders%rowtype; accepted boolean;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_amount_usd <= 0 or p_daily_limit_usd <= 0 or p_amount_usd > p_daily_limit_usd then
    return false;
  end if;
  select user_id into job.user_id from public.m2_prepared_orders where job_id=p_job_id;
  if not found then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended(job.user_id::text || ':behavior',0));
  select * into job from public.m2_prepared_orders where job_id=p_job_id for update;
  if not found or job.status <> 'claimed' or job.claim_token is distinct from p_claim_token
     or job.expires_at <= now()
     or exists (select 1 from public.m2_ranker_reservations b
                where b.request_id=job.request_id)
     or not exists (select 1 from public.user_behavior_settings s
                    where s.user_id=job.user_id and s.learning_enabled and s.provider_processing_enabled
                      and s.provider_policy_id=job.provider_policy_id
                      and s.consent_revision=job.consent_revision)
     or not public.m2_prepared_history_is_compatible(
                  job.user_id,job.history_generation,job.behavior_revision) then
    return false;
  end if;
  accepted := public.m2_reserve_ranker_budget(job.user_id,job.request_id,p_amount_usd,p_daily_limit_usd);
  return accepted;
end; $$;

create or replace function public.m2_mark_prepared_attempt(p_job_id uuid,p_claim_token uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare job public.m2_prepared_orders%rowtype; changed uuid;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  select user_id into job.user_id from public.m2_prepared_orders where job_id=p_job_id;
  if not found then return false; end if;
  -- This is the last database gate before the outbound provider call. Take the
  -- privacy lock FIRST so consent withdrawal cannot race the cost decision.
  perform pg_advisory_xact_lock(hashtextextended(job.user_id::text || ':behavior',0));
  select * into job from public.m2_prepared_orders where job_id=p_job_id for update;
  -- The first call moves claimed to attempting. Each later adapter retry
  -- reauthorizes the same job against current consent and behavior under the
  -- owner lock, without creating another reservation or a new claim.
  if not found or job.status not in ('claimed','attempting')
     or job.claim_token is distinct from p_claim_token or job.expires_at <= now()
     or not exists (select 1 from public.m2_ranker_reservations b
                    where b.request_id=job.request_id and b.user_id=job.user_id and b.status='reserved')
     or not exists (select 1 from public.user_behavior_settings s
                    where s.user_id=job.user_id and s.learning_enabled and s.provider_processing_enabled
                      and s.provider_policy_id=job.provider_policy_id
                      and s.consent_revision=job.consent_revision)
     or not public.m2_prepared_history_is_compatible(
                  job.user_id,job.history_generation,job.behavior_revision) then return false; end if;
  if job.status='attempting' then return true; end if;
  update public.m2_prepared_orders set status='attempting'
    where job_id=p_job_id returning job_id into changed;
  return changed is not null;
end; $$;

create or replace function public.m2_finish_prepared_order(
  p_job_id uuid,p_claim_token uuid,p_ranked_candidate_ids jsonb
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare job public.m2_prepared_orders%rowtype; changed uuid; expected_count integer;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  select user_id into job.user_id from public.m2_prepared_orders where job_id=p_job_id;
  if not found then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended(job.user_id::text || ':behavior',0));
  select * into job from public.m2_prepared_orders where job_id=p_job_id for update;
  if not found or job.status <> 'attempting' or job.claim_token is distinct from p_claim_token
     or job.expires_at <= now()
     or jsonb_typeof(p_ranked_candidate_ids) <> 'array' then return false; end if;
  expected_count := jsonb_array_length(job.request_payload->'candidates');
  if jsonb_array_length(p_ranked_candidate_ids) <> expected_count
     or (select count(distinct value) from jsonb_array_elements_text(p_ranked_candidate_ids)) <> expected_count
     or exists (select 1 from jsonb_array_elements_text(p_ranked_candidate_ids) e(value)
                where not exists (select 1 from jsonb_array_elements(job.request_payload->'candidates') c(value)
                                  where c.value->>'candidate_id'=e.value))
     or not exists (select 1 from public.user_behavior_settings s
                    where s.user_id=job.user_id and s.learning_enabled and s.provider_processing_enabled
                      and s.provider_policy_id=job.provider_policy_id
                      and s.consent_revision=job.consent_revision)
     or not public.m2_prepared_history_is_compatible(
                  job.user_id,job.history_generation,job.behavior_revision) then return false; end if;
  update public.m2_prepared_orders set status='ready', ranked_candidate_ids=p_ranked_candidate_ids,
      request_payload='{}'::jsonb, claim_token=null
    where job_id=p_job_id returning job_id into changed;
  return changed is not null;
end; $$;

create or replace function public.m2_fail_prepared_order(p_job_id uuid,p_claim_token uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare changed uuid;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  update public.m2_prepared_orders set status='failed',request_payload='{}'::jsonb,claim_token=null
    where job_id=p_job_id and claim_token=p_claim_token and status in ('claimed','attempting')
    returning job_id into changed;
  return changed is not null;
end; $$;

create or replace function public.m2_consume_prepared_order(
  p_user_id uuid,p_target_run_id uuid,p_eligibility_key text,p_policy_digest text,
  p_candidate_ids text[],p_minimum_overlap integer
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare job public.m2_prepared_orders%rowtype; overlap_count integer;
begin
  if coalesce(auth.jwt()->>'role','') <> 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_candidate_ids is null or cardinality(p_candidate_ids) not between 1 and 100
     or p_minimum_overlap is null
     or p_minimum_overlap not between 1 and cardinality(p_candidate_ids)
     or array_position(p_candidate_ids,null) is not null
     or (select count(distinct candidate_id) from unnest(p_candidate_ids) c(candidate_id))
          <> cardinality(p_candidate_ids) then return null; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':behavior',0));
  if not exists (select 1 from public.m2_reading_runs r
                 where r.run_id=p_target_run_id and r.user_id=p_user_id and r.closed_at is null) then
    return null;
  end if;
  select * into job from public.m2_prepared_orders j
    where j.user_id=p_user_id and j.eligibility_key=p_eligibility_key
      and j.policy_digest=p_policy_digest and j.source_run_id<>p_target_run_id
      and j.status='ready' and j.expires_at>now()
      and case when jsonb_typeof(j.ranked_candidate_ids)='array' then
        (select count(distinct ranked.candidate_id)
           from jsonb_array_elements_text(j.ranked_candidate_ids) ranked(candidate_id)
          where ranked.candidate_id=any(p_candidate_ids)) >= p_minimum_overlap
        else false end
    order by j.created_at desc for update of j skip locked limit 1;
  if not found then return null; end if;
  if not exists (select 1 from public.user_behavior_settings s
                 where s.user_id=p_user_id and s.learning_enabled and s.provider_processing_enabled
                   and s.provider_policy_id=job.provider_policy_id
                   and s.consent_revision=job.consent_revision)
     or not public.m2_prepared_history_is_compatible(
                  p_user_id,job.history_generation,job.behavior_revision)
     or jsonb_typeof(job.ranked_candidate_ids) is distinct from 'array' then return null; end if;
  select count(distinct candidate_id) into overlap_count
    from jsonb_array_elements_text(job.ranked_candidate_ids) ranked(candidate_id)
    where candidate_id=any(p_candidate_ids);
  if overlap_count < p_minimum_overlap then return null; end if;
  update public.m2_prepared_orders set status='consumed',consumed_by_run_id=p_target_run_id
    where job_id=job.job_id;
  return jsonb_build_object('owner_id',job.user_id,'source_run_id',job.source_run_id,
    'eligibility_key',job.eligibility_key,'policy_digest',job.policy_digest,
    'history_generation',job.history_generation,'consent_revision',job.consent_revision,
    'behavior_revision',job.behavior_revision,
    'provider_policy_id',job.provider_policy_id,'status','ready',
    'expires_at',extract(epoch from job.expires_at),
    'ranked_candidate_ids',job.ranked_candidate_ids);
end; $$;

create or replace function public.m2_clear_prepared_orders_on_privacy_change()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  -- The first positive event INSERTs the default generation 1 row. That is
  -- not a reset, so its pending and ready preparations remain usable.
  if tg_relid = 'public.user_behavior_revisions'::regclass then
    if (tg_op='INSERT' and new.history_generation > 1)
       or (tg_op='UPDATE' and new.history_generation <> old.history_generation) then
      delete from public.m2_prepared_orders where user_id=new.user_id;
    end if;
  else
    -- Consent changes keep their existing unconditional deletion boundary.
    delete from public.m2_prepared_orders where user_id=new.user_id;
  end if;
  return new;
end; $$;
create trigger m2_clear_prepared_orders_on_generation
  after insert or update of history_generation on public.user_behavior_revisions
  for each row execute function public.m2_clear_prepared_orders_on_privacy_change();
create trigger m2_clear_prepared_orders_on_consent
  after insert or update of consent_revision,learning_enabled,
    provider_processing_enabled,provider_policy_id on public.user_behavior_settings
  for each row execute function public.m2_clear_prepared_orders_on_privacy_change();

revoke execute on function public.m2_enqueue_prepared_order(uuid,uuid,text,text,bigint,bigint,bigint,text,uuid,jsonb,integer),
  public.m2_claim_prepared_order(text),
  public.m2_scrub_expired_prepared_orders(integer),
  public.m2_prepared_history_is_compatible(uuid,bigint,bigint),
  public.m2_reserve_prepared_budget(uuid,uuid,numeric,numeric),
  public.m2_mark_prepared_attempt(uuid,uuid),
  public.m2_finish_prepared_order(uuid,uuid,jsonb),
  public.m2_fail_prepared_order(uuid,uuid),
  public.m2_consume_prepared_order(uuid,uuid,text,text,text[],integer),
  public.m2_clear_prepared_orders_on_privacy_change() from public,anon,authenticated;
grant execute on function public.m2_enqueue_prepared_order(uuid,uuid,text,text,bigint,bigint,bigint,text,uuid,jsonb,integer),
  public.m2_claim_prepared_order(text),
  public.m2_scrub_expired_prepared_orders(integer),
  public.m2_prepared_history_is_compatible(uuid,bigint,bigint),
  public.m2_reserve_prepared_budget(uuid,uuid,numeric,numeric),
  public.m2_mark_prepared_attempt(uuid,uuid),
  public.m2_finish_prepared_order(uuid,uuid,jsonb),
  public.m2_fail_prepared_order(uuid,uuid),
  public.m2_consume_prepared_order(uuid,uuid,text,text,text[],integer) to service_role;

commit;
