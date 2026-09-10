begin;

-- Personal assignments remain outside publication_entries and every anonymous RPC.
create table public.discovery_storage_policy (
  singleton boolean primary key default true check(singleton),
  max_entries integer not null default 100 check(max_entries between 1 and 100),
  max_payload_bytes integer not null default 16777216 check(max_payload_bytes between 1024 and 67108864),
  max_response_bytes integer not null default 1048576 check(max_response_bytes between 1024 and 1048576),
  staleness_hours numeric not null default 27 check(staleness_hours > 0 and staleness_hours <= 8760),
  history_window_hours numeric not null default 168 check(history_window_hours > 0 and history_window_hours <= 8760),
  max_history_rows integer not null default 10000 check(max_history_rows between 1 and 100000)
);
insert into public.discovery_storage_policy(singleton) values(true);
create table public.private_discovery_editions (
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  edition_id text not null check(edition_id ~ '^[A-Za-z0-9._:-]{1,160}$'),
  payload_digest text not null check(payload_digest ~ '^[0-9a-f]{64}$'),
  payload_text text not null,
  payload jsonb not null,
  generated_at timestamptz not null,
  stored_at timestamptz not null default statement_timestamp(),
  primary key(owner_user_id,edition_id)
);
create index private_discovery_latest on public.private_discovery_editions(owner_user_id,generated_at desc,edition_id desc);
create table public.private_discovery_entries (
  owner_user_id uuid not null,
  edition_id text not null,
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  position integer not null check(position > 0),
  source_id text not null,
  primary_lane text not null check(primary_lane in ('updates','hot','interested','surprise')),
  placement jsonb not null,
  facts jsonb not null,
  primary key(owner_user_id,edition_id,story_id),
  unique(owner_user_id,edition_id,position),
  foreign key(owner_user_id,edition_id) references public.private_discovery_editions(owner_user_id,edition_id) on delete cascade
);
alter table public.discovery_storage_policy enable row level security;
alter table public.discovery_storage_policy force row level security;
alter table public.private_discovery_editions enable row level security;
alter table public.private_discovery_editions force row level security;
alter table public.private_discovery_entries enable row level security;
alter table public.private_discovery_entries force row level security;
revoke all on public.discovery_storage_policy,public.private_discovery_editions,public.private_discovery_entries from public,anon,authenticated;
grant select,update on public.discovery_storage_policy to service_role;

create function public.discovery_exact_keys(value jsonb, expected text[]) returns boolean
language sql immutable set search_path=pg_catalog as $$
  select jsonb_typeof(value) = 'object' and
    (select array_agg(key order by key) from jsonb_object_keys(value) key) =
    (select array_agg(key order by key) from unnest(expected) key)
$$;
revoke all on function public.discovery_exact_keys(jsonb,text[]) from public,anon,authenticated;

create function public.private_discovery_context(p_owner_user_id uuid) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare policy public.discovery_storage_policy%rowtype; latest public.private_discovery_editions%rowtype;
  history jsonb; rows_count bigint; answer jsonb;
begin
  if p_owner_user_id is null or not exists(select 1 from auth.users where id=p_owner_user_id) then
    raise exception 'private discovery context unavailable';
  end if;
  select * into strict policy from public.discovery_storage_policy where singleton;
  select * into latest from public.private_discovery_editions where owner_user_id=p_owner_user_id
    order by generated_at desc,edition_id desc limit 1;
  select count(*),coalesce(jsonb_agg(jsonb_build_object('story_id',e.story_id,'source_id',e.source_id,
    'shown_at',to_char(d.generated_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
    order by d.generated_at,d.edition_id,e.position),'[]'::jsonb)
    into rows_count,history
    from public.private_discovery_entries e join public.private_discovery_editions d using(owner_user_id,edition_id)
    where e.owner_user_id=p_owner_user_id and d.generated_at >= statement_timestamp()-make_interval(secs=>(policy.history_window_hours*3600)::double precision);
  if rows_count > policy.max_history_rows then raise exception 'private discovery history exceeds policy'; end if;
  answer := jsonb_build_object('schema_version',1,'owner_user_id',p_owner_user_id,'history_available',true,
    'first_edition',latest.edition_id is null,
    'latest',case when latest.edition_id is null then null else jsonb_build_object('edition_id',latest.edition_id,
      'payload_digest',latest.payload_digest,'receipt_digest',latest.payload->'receipt'->>'receipt_digest',
      'generated_at',latest.generated_at) end,'history',history,
    'storage_policy',to_jsonb(policy)-'singleton');
  if octet_length(answer::text)>policy.max_payload_bytes then raise exception 'private discovery context exceeds policy'; end if;
  return answer;
end;
$$;
revoke all on function public.private_discovery_context(uuid) from public,anon,authenticated;
grant execute on function public.private_discovery_context(uuid) to service_role;

create function public.private_discovery_identity(p_owner_user_id uuid,p_edition_id text) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare answer jsonb;
begin
  if p_owner_user_id is null or p_edition_id is null or p_edition_id !~ '^m2:[0-9a-f]{64}$'
    or not exists(select 1 from auth.users where id=p_owner_user_id) then raise exception 'private discovery identity unavailable'; end if;
  select jsonb_build_object('edition_id',edition_id,'payload_digest',payload_digest,
    'receipt_digest',payload->'receipt'->>'receipt_digest','generated_at',generated_at)
    into answer from public.private_discovery_editions where owner_user_id=p_owner_user_id and edition_id=p_edition_id;
  return answer;
end;
$$;
revoke all on function public.private_discovery_identity(uuid,text) from public,anon,authenticated;
grant execute on function public.private_discovery_identity(uuid,text) to service_role;

create function public.finalize_private_discovery(p_payload_text text,p_payload_digest text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; r jsonb; row jsonb; fact jsonb; part record; band record; lane text; topic text;
  policy public.discovery_storage_policy%rowtype; prior public.private_discovery_editions%rowtype;
  owner_id uuid; edition text; digest_value text; generated timestamptz; materialized timestamptz;
  count_entries integer; distinct_count integer; position_value integer:=0; sum_score numeric;
  context jsonb; current_revision bigint; candidates jsonb; primary_expected text; quota_total integer;
  policy_limit numeric; score_value numeric; signals jsonb; current_interest_count integer;
begin
  select * into strict policy from public.discovery_storage_policy where singleton;
  if p_payload_text is null or octet_length(p_payload_text)>policy.max_payload_bytes or
    p_payload_digest is null or p_payload_digest !~ '^[0-9a-f]{64}$' then raise exception 'invalid private discovery payload'; end if;
  digest_value:=encode(extensions.digest(convert_to(p_payload_text,'UTF8'),'sha256'),'hex');
  if digest_value<>p_payload_digest then raise exception 'private discovery digest mismatch'; end if;
  p:=p_payload_text::jsonb;
  if not coalesce(public.discovery_exact_keys(p,array['schema_version','kind','owner_user_id','edition_id','code_revision','profile_fingerprint','materialized_at','receipt','cards']),false)
    or p->>'schema_version' is distinct from '1' or p->>'kind' is distinct from 'owned_private_discovery'
    or jsonb_typeof(p->'cards') is distinct from 'object'
    or p->>'code_revision' is null or p->>'code_revision' !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
    or p->>'profile_fingerprint' is null or p->>'profile_fingerprint' !~ '^[0-9a-f]{64}$'
    or p->>'edition_id' is null or p->>'edition_id' !~ '^[A-Za-z0-9._:-]{1,160}$'
    or p->>'owner_user_id' is null or p->>'owner_user_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or p->>'materialized_at' is null or p->>'materialized_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
    then raise exception 'invalid private discovery envelope'; end if;
  owner_id:=(p->>'owner_user_id')::uuid; edition:=p->>'edition_id';
  materialized:=(p->>'materialized_at')::timestamptz;
  if materialized>statement_timestamp() or not exists(select 1 from auth.users where id=owner_id) then
    raise exception 'private discovery owner or clock unavailable'; end if;
  r:=p->'receipt';
  -- All tuple strings are constrained ASCII ids/digests. Removing array separator
  -- spaces matches Python canonical JSON without rewriting any string contents.
  if r->>'language' is null or r->>'language' not in ('en','zh')
    or r->'bindings'->>'snapshot_digest' is null or r->'bindings'->>'snapshot_digest' !~ '^[0-9a-f]{64}$'
    or r->'bindings'->>'policy_digest' is null or r->'bindings'->>'policy_digest' !~ '^[0-9a-f]{64}$'
    or r->'bindings'->>'ranking_configuration_digest' is null or r->'bindings'->>'ranking_configuration_digest' !~ '^[0-9a-f]{64}$'
    or r->'bindings'->>'display_dedup_digest' is null or r->'bindings'->>'display_dedup_digest' !~ '^[0-9a-f]{64}$'
    or r->'bindings'->>'code_digest' is null or r->'bindings'->>'code_digest' !~ '^[0-9a-f]{64}$'
    or r->'bindings'->>'profile_revision' is null or r->'bindings'->>'profile_revision' !~ '^[0-9]+$'
    or (r->'bindings'->>'previous_snapshot_digest' is not null and r->'bindings'->>'previous_snapshot_digest' !~ '^[0-9a-f]{64}$')
    then raise exception 'private discovery identity binding invalid'; end if;
  if edition <> 'm2:'||encode(extensions.digest(convert_to(replace(jsonb_build_array(
    owner_id::text,r->'bindings'->>'snapshot_digest',r->'bindings'->>'previous_snapshot_digest',
    (r->'bindings'->>'profile_revision')::bigint,p->>'profile_fingerprint',r->'bindings'->>'policy_digest',
    p->>'code_revision',r->>'language',r->'bindings'->>'ranking_configuration_digest',
    r->'bindings'->>'display_dedup_digest',r->'bindings'->>'code_digest')::text,', ',','),'UTF8'),'sha256'),'hex') then
    raise exception 'private discovery edition identity mismatch'; end if;
  perform pg_advisory_xact_lock(hashtextextended('private-discovery:'||owner_id::text,0));
  select * into prior from public.private_discovery_editions where owner_user_id=owner_id and edition_id=edition;
  if found then
    if prior.payload_digest<>digest_value or prior.payload_text<>p_payload_text then raise exception 'private discovery edition conflict'; end if;
    return jsonb_build_object('schema_version',1,'status','already_stored','edition_id',edition,'payload_digest',digest_value);
  end if;
  r:=p->'receipt';
  if jsonb_typeof(r) is distinct from 'object' or r->>'schema_version' is distinct from '2'
    or r->>'artifact_scope' is distinct from 'local_private_preview' or r->'publishable' is distinct from 'false'::jsonb
    or r->>'verdict' is distinct from 'PASS' or not coalesce((r->'profile_available'='true'::jsonb or
      (r->'profile_available'='false'::jsonb and r->>'profile_status'='settled_empty'
       and r->'profile_input'->>'interest_count'='0' and r->'profile_input'->>'matched_story_count'='0'
       and r->'profile_input'->'scores'='{}'::jsonb)),false)
    or r->'history_available' is distinct from 'true'::jsonb
    or r->>'receipt_digest' is null or r->>'receipt_digest' !~ '^[0-9a-f]{64}$'
    or jsonb_typeof(r->'entries') is distinct from 'array' or jsonb_typeof(r->'candidates') is distinct from 'array'
    or jsonb_typeof(r->'disclosures') is distinct from 'array'
    or jsonb_typeof(r->'profile_input') is distinct from 'object'
    or r->>'generated_at' is null or r->>'generated_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
    then raise exception 'private discovery requires a verified passing receipt'; end if;
  generated:=(r->>'generated_at')::timestamptz;
  if generated>statement_timestamp() or generated<statement_timestamp()-make_interval(secs=>(policy.staleness_hours*3600)::double precision) then
    raise exception 'private discovery receipt stale'; end if;
  if exists(select 1 from public.private_discovery_editions where owner_user_id=owner_id and generated_at>=generated) then
    raise exception 'private discovery edition clock did not advance'; end if;
  foreach lane in array array['snapshot_digest','configuration_digest','ranking_configuration_digest','profile_digest','policy_digest','code_digest'] loop
    if r->'bindings'->>lane is null or r->'bindings'->>lane !~ '^[0-9a-f]{64}$' then raise exception 'private discovery binding missing'; end if;
  end loop;
  if r->'profile_input'->>'source_snapshot_digest' is distinct from r->'bindings'->>'snapshot_digest'
     or r->'profile_input'->>'configuration_digest' is distinct from r->'bindings'->>'ranking_configuration_digest'
     or r->'profile_input'->>'preference_revision' is distinct from r->'bindings'->>'profile_revision'
     then raise exception 'private discovery profile binding mismatch'; end if;
  -- Keep declared preference edits from racing the verified materialization.
  perform 1 from public.user_preferences where user_id=owner_id for share;
  signals:=public.materialize_user_interest_signals(owner_id);
  select greatest(up.revision,(signals->>'revision')::bigint),cardinality(up.interests)+jsonb_array_length(signals->'topic_adjustments')
    into current_revision,current_interest_count from public.user_preferences up where up.user_id=owner_id;
  if current_revision is null or current_revision is distinct from (r->'bindings'->>'profile_revision')::bigint
     or current_interest_count is distinct from (r->'profile_input'->>'interest_count')::integer
     or (r->'profile_available') is distinct from to_jsonb(current_interest_count>0)
     or exists(select 1 from public.user_preferences where user_id=owner_id and updated_at>materialized)
     or exists(select 1 from public.user_story_interests where user_id=owner_id and updated_at>materialized)
     then raise exception 'private discovery profile changed'; end if;
  context:=public.private_discovery_context(owner_id);
  if r->'history_input' is distinct from context->'history' then raise exception 'private discovery history changed'; end if;
  if (r->'policy'->'windows'->>'repetition')::numeric>policy.history_window_hours or
     (r->'policy'->'windows'->>'source_fatigue')::numeric>policy.history_window_hours then raise exception 'private discovery history window insufficient'; end if;
  if not coalesce(public.discovery_exact_keys(r->'policy'->'lane_quotas',array['updates','hot','interested','surprise']),false)
    or not coalesce(public.discovery_exact_keys(r->'shortfalls',array['updates','hot','interested','surprise']),false)
    or jsonb_typeof(r->'policy'->'lane_priority') is distinct from 'array'
    or (select array_agg(x order by x) from jsonb_array_elements_text(r->'policy'->'lane_priority') x) is distinct from array['hot','interested','surprise','updates']
    then raise exception 'private discovery lane policy invalid'; end if;
  count_entries:=jsonb_array_length(r->'entries');
  select count(distinct x->>'story_id') into distinct_count from jsonb_array_elements(r->'entries') x;
  if count_entries<1 or count_entries>policy.max_entries or count_entries<>distinct_count
    or (select count(*) from jsonb_object_keys(p->'cards'))<>count_entries then raise exception 'private discovery entry count invalid'; end if;
  quota_total:=0;
  foreach lane in array array['updates','hot','interested','surprise'] loop
    if r->'policy'->'lane_quotas'->>lane !~ '^[0-9]+$' or r->'shortfalls'->>lane !~ '^[0-9]+$' then raise exception 'private discovery quota invalid'; end if;
    quota_total:=quota_total+(r->'policy'->'lane_quotas'->>lane)::integer;
    select count(*) into distinct_count from jsonb_array_elements(r->'entries') x where x->>'primary_lane'=lane;
    if distinct_count+(r->'shortfalls'->>lane)::integer<>(r->'policy'->'lane_quotas'->>lane)::integer then raise exception 'private discovery quota mismatch'; end if;
  end loop;
  if quota_total>policy.max_entries or quota_total<>(r->'policy'->>'size')::integer then raise exception 'private discovery quota size invalid'; end if;
  if jsonb_typeof(r->'bands') is distinct from 'array' or
    (select array_agg(x->>'band' order by x->>'band') from jsonb_array_elements(r->'bands') x)
    is distinct from array['deliberate_surprise','freshness','relevance','repetition','source_diversity','topic_diversity','trend'] then
    raise exception 'private discovery bands incomplete'; end if;
  for band in select x->>'band' as key,x as value from jsonb_array_elements(r->'bands') x loop
    if band.value->>'verdict' not in ('PASS','DISABLED') or band.value->>'verdict' is null then raise exception 'private discovery band did not pass'; end if;
    if (r->'policy'->'bands'->band.key->>'active')::boolean then
      if band.value->>'verdict'<>'PASS' or jsonb_typeof(band.value->'achieved') is distinct from 'number'
        or (band.value->>'achieved')::numeric<(r->'policy'->'bands'->band.key->>'floor')::numeric
        or (band.value->>'achieved')::numeric>(r->'policy'->'bands'->band.key->>'cap')::numeric
        or (band.value->>'distinct')::integer<(r->'policy'->'bands'->band.key->>'min_distinct')::integer then raise exception 'private discovery band bounds missed'; end if;
    elsif band.value->>'verdict'<>'DISABLED' or coalesce(r->'policy'->'band_exceptions'->>band.key,'')='' then
      raise exception 'private discovery band exception missing';
    end if;
  end loop;
  -- Validate all rows before any source-fact insertion. Transaction rollback remains the final guard.
  for row in select * from jsonb_array_elements(r->'entries') loop
    position_value:=position_value+1;
    fact:=p->'cards'->(row->>'story_id');
    if exists(select 1 from unnest(array['canonical_url','title','summary','language','published_at','source_kind','source_name']) k where jsonb_typeof(fact->k) is distinct from 'string')
      or row->>'position' is distinct from position_value::text or row->>'story_id' is null or row->>'story_id' !~ '^story:[0-9a-f]{64}$'
      or row->>'primary_lane' not in ('updates','hot','interested','surprise')
      or coalesce(row->>'independent_source','')='' or octet_length(row->>'independent_source')>512
      or coalesce(row->>'plain_reason','')='' or octet_length(row->>'plain_reason')>8000
      or not coalesce(public.discovery_exact_keys(fact,array['canonical_url','title','summary','language','published_at','topic_ids','source_kind','source_name','coverage_mentions']),false)
      or jsonb_typeof(fact->'topic_ids') is distinct from 'array' or jsonb_array_length(fact->'topic_ids')>20
      or jsonb_typeof(fact->'coverage_mentions') is distinct from 'array' or jsonb_array_length(fact->'coverage_mentions')>20
      or octet_length((fact->'coverage_mentions')::text)>32768
      or fact->>'source_kind' is distinct from 'outlet'
      or fact->>'language' not in ('en','zh') or coalesce(fact->>'title','')='' or length(fact->>'title')>2000
      or jsonb_typeof(fact->'summary') is distinct from 'string' or length(fact->>'summary')>8000
      or coalesce(fact->>'source_name','')='' or length(fact->>'source_name')>200
      or fact->>'canonical_url' is distinct from row->>'url' or fact->>'title' is distinct from row->>'title'
      or fact->>'summary' is distinct from row->>'description' or fact->>'language' is distinct from row->>'language'
      or fact->>'source_name' is distinct from row->>'source_name' or fact->>'published_at' is distinct from row->>'published_at'
      or fact->'topic_ids' is distinct from row->'topic_ids'
      then raise exception 'private discovery card invalid'; end if;
    if fact->>'canonical_url' !~ '^https?://[^/@[:space:]]+(/|$)' or fact->>'canonical_url' ~ '#'
      or length(fact->>'canonical_url')>2048
      or row->>'story_id'<>'story:'||encode(extensions.digest(convert_to(fact->>'canonical_url','UTF8'),'sha256'),'hex')
      then raise exception 'private discovery story identity invalid'; end if;
    foreach topic in array array(select jsonb_array_elements_text(fact->'topic_ids')) loop
      if topic !~ '^[a-z0-9][a-z0-9-]{0,79}$' then raise exception 'private discovery topic invalid'; end if;
    end loop;
    if exists(select 1 from jsonb_array_elements(fact->'topic_ids') t where jsonb_typeof(t) is distinct from 'string') then raise exception 'private discovery topic type invalid'; end if;
    if (select count(*) from jsonb_array_elements(fact->'topic_ids'))<>(select count(distinct x) from jsonb_array_elements_text(fact->'topic_ids') x) then raise exception 'private discovery duplicate topic'; end if;
    for part in select value from jsonb_array_elements(fact->'coverage_mentions') loop
      if not coalesce(public.discovery_exact_keys(part.value,array['source_kind','source_id','source_name','url','headline','mentioned_at']),false)
        or exists(select 1 from jsonb_each(part.value) k where jsonb_typeof(k.value) is distinct from 'string')
        or part.value->>'source_kind' not in ('outlet','newsletter')
        or coalesce(part.value->>'source_id','')='' or length(part.value->>'source_id')>160
        or coalesce(part.value->>'source_name','')='' or length(part.value->>'source_name')>200
        or coalesce(part.value->>'headline','')='' or length(part.value->>'headline')>2000
        or part.value->>'url' !~ '^https?://[^/@[:space:]]+(/|$)' or length(part.value->>'url')>2048
        or part.value->>'mentioned_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
        then raise exception 'private discovery coverage invalid'; end if;
      perform (part.value->>'mentioned_at')::timestamptz;
    end loop;
    if jsonb_typeof(row->'lane_scores') is distinct from 'object' or jsonb_typeof(row->'lane_reasons') is distinct from 'object'
      or jsonb_typeof(row->'secondary_lanes') is distinct from 'array' then raise exception 'private discovery lane metadata invalid'; end if;
    if (select array_agg(k order by k) from jsonb_object_keys(row->'lane_scores') k) is distinct from
       (select array_agg(k order by k) from jsonb_object_keys(row->'lane_reasons') k) then raise exception 'private discovery lane reasons missing'; end if;
    for part in select * from jsonb_each(row->'lane_scores') loop
      if part.key not in ('updates','hot','interested','surprise') or jsonb_typeof(part.value) is distinct from 'number'
        or (part.value::text)::numeric not between 0 and 1
        or jsonb_typeof(row->'lane_reasons'->part.key) is distinct from 'string'
        or coalesce(row->'lane_reasons'->>part.key,'')='' or octet_length(row->'lane_reasons'->>part.key)>8000
        then raise exception 'private discovery lane score invalid'; end if;
    end loop;
    if row->'secondary_lanes' is distinct from (select coalesce(jsonb_agg(value order by ord),'[]'::jsonb)
      from jsonb_array_elements_text(r->'policy'->'lane_priority') with ordinality t(value,ord)
      where (row->'lane_scores') ? value and value<>row->>'primary_lane') then raise exception 'private discovery secondary lane invalid'; end if;
    if not coalesce(public.discovery_exact_keys(row->'components',array['relevance','freshness','trend','editor_consensus','deliberate_surprise','diversity','repetition_penalty','source_fatigue_penalty','final_score']),false) then raise exception 'private discovery scores incomplete'; end if;
    sum_score:=0;
    for part in select * from jsonb_each(row->'components') loop
      if jsonb_typeof(part.value)<>'number' then raise exception 'private discovery score invalid'; end if;
      if part.key='final_score' then continue; end if;
      score_value:=(part.value::text)::numeric;
      policy_limit:=(r->'policy'->'components'->part.key->>'weight')::numeric*(r->'policy'->'components'->part.key->>'cap')::numeric;
      if policy_limit is null or score_value<0 or score_value>policy_limit or
        ((r->'policy'->'components'->part.key->>'enabled')::boolean=false and score_value<>0) then raise exception 'private discovery component bound invalid'; end if;
      sum_score:=sum_score+case when part.key in ('repetition_penalty','source_fatigue_penalty') then -score_value else score_value end;
    end loop;
    if abs(sum_score-(row->'components'->>'final_score')::numeric)>0.000000001 then raise exception 'private discovery score composition invalid'; end if;
    select value into primary_expected from jsonb_array_elements_text(r->'policy'->'lane_priority') with ordinality t(value,ord)
      where (row->'lane_scores') ? value order by ord limit 1;
    if primary_expected is distinct from row->>'primary_lane' or row->'lane_reasons'->>primary_expected is distinct from row->>'plain_reason' then raise exception 'private discovery lane winner invalid'; end if;
    if not exists(select 1 from jsonb_array_elements(r->'candidates') x where x->>'story_id'=row->>'story_id'
      and x=(row-'position'-'backfilled')) then raise exception 'private discovery candidate mismatch'; end if;
  end loop;
  insert into public.private_discovery_editions(owner_user_id,edition_id,payload_digest,payload_text,payload,generated_at)
    values(owner_id,edition,digest_value,p_payload_text,p,generated);
  for row in select * from jsonb_array_elements(r->'entries') loop
    fact:=p->'cards'->(row->>'story_id');
    -- Conflict never overwrites established public source facts using a private selection.
    insert into public.canonical_stories(story_id,canonical_url,title,summary,language,source_kind,source_name,published_at)
      values(row->>'story_id',fact->>'canonical_url',fact->>'title',fact->>'summary',fact->>'language','outlet',fact->>'source_name',(fact->>'published_at')::timestamptz)
      on conflict(story_id) do nothing;
    for topic in select jsonb_array_elements_text(fact->'topic_ids') loop
      insert into public.story_topics(story_id,topic_id,topic_name) values(row->>'story_id',topic,topic) on conflict do nothing;
    end loop;
    insert into public.private_discovery_entries(owner_user_id,edition_id,story_id,position,source_id,primary_lane,placement,facts)
      values(owner_id,edition,row->>'story_id',(row->>'position')::integer,row->>'independent_source',row->>'primary_lane',row,fact);
  end loop;
  -- Refuse an edition the authenticated reader cannot receive under its byte policy.
  perform public.private_discovery_read(owner_id,edition);
  return jsonb_build_object('schema_version',1,'status','stored','edition_id',edition,'payload_digest',digest_value);
end;
$$;
revoke all on function public.finalize_private_discovery(text,text) from public,anon,authenticated;
grant execute on function public.finalize_private_discovery(text,text) to service_role;

create function public.private_discovery_read(p_owner_user_id uuid,p_edition_id text default null) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare caller uuid:=p_owner_user_id; d public.private_discovery_editions%rowtype;
  policy public.discovery_storage_policy%rowtype; result jsonb; rows jsonb;
begin
  if caller is null then raise exception using errcode='42501',message='authentication required'; end if;
  select * into strict policy from public.discovery_storage_policy where singleton;
  if p_edition_id is not null and p_edition_id !~ '^[A-Za-z0-9._:-]{1,160}$' then raise exception 'invalid edition id'; end if;
  select * into d from public.private_discovery_editions where owner_user_id=caller
    and (p_edition_id is null or edition_id=p_edition_id) order by generated_at desc,edition_id desc limit 1;
  if not found then return jsonb_build_object('schema_version',1,'status','unavailable',
    'reason_code',case when p_edition_id is null then 'no_private_edition' else 'edition_unavailable' end,'edition',null); end if;
  select coalesce(jsonb_agg(jsonb_build_object('position',e.position,'primary_lane',e.primary_lane,
    'reason',e.placement->>'plain_reason','secondary_reasons',coalesce((select jsonb_agg(jsonb_build_object('lane',lane,'reason',e.placement->'lane_reasons'->>lane) order by ord)
      from jsonb_array_elements_text(e.placement->'secondary_lanes') with ordinality t(lane,ord)),'[]'::jsonb),
    'card',e.facts||jsonb_build_object('story_id',e.story_id,'publication_seq',0,'position',0,
      'score_components',e.placement->'components','ordering_mode','weighted_total','ordering_key','{}'::jsonb,
      'topic_ranks','{}'::jsonb,'ranking_explanation',e.placement->>'plain_reason',
      'page_order_mode','discovery','next_cursor',null,'read_at',us.read_at,'saved_at',us.saved_at,'state_revision',coalesce(us.revision,0),
      'interests',coalesce((select jsonb_agg(jsonb_build_object('topic_id',i.topic_id,'signal',i.signal,'revision',i.revision) order by i.topic_id)
        from public.user_story_interests i where i.user_id=caller and i.story_id=e.story_id),'[]'::jsonb))) order by e.position),'[]'::jsonb)
    into rows from public.private_discovery_entries e left join public.user_story_state us on us.user_id=caller and us.story_id=e.story_id
    where e.owner_user_id=caller and e.edition_id=d.edition_id;
  result:=jsonb_build_object('schema_version',1,'status','ready','reason_code','',
    'edition',jsonb_build_object('edition_id',d.edition_id,'generated_at',d.generated_at,'code_revision',d.payload->>'code_revision',
      'policy_revision',(d.payload->'receipt'->'policy'->>'revision')::integer,
      'policy_digest',d.payload->'receipt'->'bindings'->>'policy_digest','snapshot_digest',d.payload->'receipt'->'bindings'->>'snapshot_digest',
      'profile_revision',(d.payload->'receipt'->'bindings'->>'profile_revision')::bigint,'receipt_digest',d.payload->'receipt'->>'receipt_digest',
      'stale',d.generated_at<statement_timestamp()-make_interval(secs=>(policy.staleness_hours*3600)::double precision),
      'disclosures',d.payload->'receipt'->'disclosures','shortfalls',d.payload->'receipt'->'shortfalls','entries',rows));
  if jsonb_array_length(rows)>policy.max_entries or octet_length(result::text)>policy.max_response_bytes then raise exception 'private discovery response exceeds policy'; end if;
  return result;
end;
$$;
revoke all on function public.private_discovery_read(uuid,text) from public,anon,authenticated;
create function public.discovery_edition(p_edition_id text default null) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public as $$
begin
  if auth.uid() is null then raise exception using errcode='42501',message='authentication required'; end if;
  perform set_config('response.headers','[{"Cache-Control":"private, no-store"},{"Vary":"Authorization"}]',true);
  return public.private_discovery_read(auth.uid(),p_edition_id);
end;
$$;
revoke all on function public.discovery_edition(text) from public,anon,authenticated;
grant execute on function public.discovery_edition(text) to authenticated;

create function public.discovery_story_access(p_user_id uuid,p_story_id text) returns boolean
language sql stable security definer set search_path=pg_catalog,public as $$
  select p_user_id is not null and (
    exists(select 1 from public.publication_entries e join public.publication_runs r using(publication_seq) where e.story_id=p_story_id and r.finalized_at is not null)
    or exists(select 1 from public.private_discovery_entries where owner_user_id=p_user_id and story_id=p_story_id)
    or exists(select 1 from public.user_story_state where user_id=p_user_id and story_id=p_story_id)
  )
$$;
revoke all on function public.discovery_story_access(uuid,text) from public,anon,authenticated;

-- Preserve existing CAS and action idempotency; tighten only story visibility.
create or replace function public.set_story_state(
  p_story_id text, p_read boolean, p_saved boolean, p_expected_revision bigint, p_idempotency_key text
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); current_revision bigint; answer jsonb; resource text;
  request_hash text; receipt_cap integer;
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_story_id is null or p_story_id !~ '^story:[0-9a-f]{64}$'
     or p_read is null or p_saved is null
     or p_expected_revision is null or p_expected_revision < 0
     or p_idempotency_key is null or p_idempotency_key = ''
     or octet_length(p_idempotency_key) > 512 then
    raise exception 'invalid state write';
  end if;
  if not public.discovery_story_access(caller,p_story_id) then
    raise exception 'story is unavailable';
  end if;
  resource := p_story_id;
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'story_id', p_story_id, 'read', p_read, 'saved', p_saved,
    'expected_revision', p_expected_revision)::text, 'UTF8'), 'sha256'), 'hex');
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':state:' || resource, 0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key, 0));
  select response into answer from public.user_action_receipts where user_id = caller and idempotency_key = p_idempotency_key;
  if answer is not null then
    if not exists (select 1 from public.user_action_receipts where user_id = caller
      and idempotency_key = p_idempotency_key and operation = 'set_story_state'
      and resource_key = resource and request_digest = request_hash) then
      raise exception 'idempotency key reuse mismatch';
    end if;
    return answer;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':receipt-cap', 0));
  select receipt_max_per_user into receipt_cap from public.feed_policy where singleton;
  if receipt_cap is null then raise exception 'feed policy unavailable'; end if;
  if (select count(*) from public.user_action_receipts where user_id = caller) >= receipt_cap then
    raise exception 'receipt limit reached';
  end if;
  select revision into current_revision from public.user_story_state
    where user_id = caller and story_id = p_story_id for update;
  if coalesce(current_revision, 0) <> p_expected_revision then
    answer := jsonb_build_object('status', 'conflict', 'revision', coalesce(current_revision, 0));
    insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
      values (caller, p_idempotency_key, 'set_story_state', resource, request_hash, answer);
    return answer;
  end if;
  insert into public.user_story_state(user_id, story_id, read_at, saved_at, revision)
  values (caller, p_story_id, case when p_read then now() end, case when p_saved then now() end, 1)
  on conflict (user_id, story_id) do update set
    read_at = case when p_read then coalesce(user_story_state.read_at, now()) else null end,
    saved_at = case when p_saved then coalesce(user_story_state.saved_at, now()) else null end,
    revision = user_story_state.revision + 1, updated_at = now()
  returning jsonb_build_object('status', 'updated', 'revision', revision, 'read_at', read_at, 'saved_at', saved_at) into answer;
  insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
    values (caller, p_idempotency_key, 'set_story_state', resource, request_hash, answer);
  return answer;
end;
$$;

create or replace function public.set_story_interest(
  p_story_id text, p_topic_id text, p_signal text, p_expected_revision bigint, p_idempotency_key text
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); current_revision bigint; answer jsonb; resource text;
  request_hash text; receipt_cap integer;
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_story_id is null or p_story_id !~ '^story:[0-9a-f]{64}$'
     or p_topic_id is null or p_topic_id !~ '^[a-z0-9][a-z0-9-]{0,79}$'
     or p_signal is null or p_signal not in ('more_like', 'less_like')
     or p_expected_revision is null or p_expected_revision < 0
     or p_idempotency_key is null or p_idempotency_key = ''
     or octet_length(p_idempotency_key) > 512 then
    raise exception 'invalid interest write';
  end if;
  -- Same per-owner lock as finalization closes insertion/update races in profile signals.
  perform pg_advisory_xact_lock(hashtextextended('private-discovery:'||caller::text,0));
  if not public.discovery_story_access(caller,p_story_id) then
    raise exception 'story is unavailable';
  end if;
  if not exists (select 1 from public.story_topics
    where story_id = p_story_id and topic_id = p_topic_id) then
    raise exception 'story topic does not exist';
  end if;
  resource := p_story_id || ':' || p_topic_id;
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'story_id', p_story_id, 'topic_id', p_topic_id, 'signal', p_signal,
    'expected_revision', p_expected_revision)::text, 'UTF8'), 'sha256'), 'hex');
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':interest:' || resource, 0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key, 0));
  select response into answer from public.user_action_receipts where user_id = caller and idempotency_key = p_idempotency_key;
  if answer is not null then
    if not exists (select 1 from public.user_action_receipts where user_id = caller
      and idempotency_key = p_idempotency_key and operation = 'set_story_interest'
      and resource_key = resource and request_digest = request_hash) then
      raise exception 'idempotency key reuse mismatch';
    end if;
    return answer;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':receipt-cap', 0));
  select receipt_max_per_user into receipt_cap from public.feed_policy where singleton;
  if receipt_cap is null then raise exception 'feed policy unavailable'; end if;
  if (select count(*) from public.user_action_receipts where user_id = caller) >= receipt_cap then
    raise exception 'receipt limit reached';
  end if;
  select revision into current_revision from public.user_story_interests
    where user_id = caller and story_id = p_story_id and topic_id = p_topic_id for update;
  if coalesce(current_revision, 0) <> p_expected_revision then
    answer := jsonb_build_object('status', 'conflict', 'revision', coalesce(current_revision, 0));
    insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
      values (caller, p_idempotency_key, 'set_story_interest', resource, request_hash, answer);
    return answer;
  end if;
  insert into public.user_story_interests(user_id, story_id, topic_id, signal, revision)
  values (caller, p_story_id, p_topic_id, p_signal, 1)
  on conflict (user_id, story_id, topic_id) do update set signal = excluded.signal,
    revision = user_story_interests.revision + 1, updated_at = now()
  returning jsonb_build_object('status', 'updated', 'revision', revision, 'signal', signal) into answer;
  insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
    values (caller, p_idempotency_key, 'set_story_interest', resource, request_hash, answer);
  return answer;
end;
$$;


commit;
