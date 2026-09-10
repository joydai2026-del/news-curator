-- Additive R3 settlement support for explicit, independently verified lower-share shortages.
create or replace function public.finalize_private_discovery(p_payload_text text,p_payload_digest text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,public as $$
declare p jsonb; r jsonb; row jsonb; fact jsonb; part record; band record; lane text; topic text;
  policy public.discovery_storage_policy%rowtype; prior public.private_discovery_editions%rowtype;
  owner_id uuid; edition text; digest_value text; generated timestamptz; materialized timestamptz;
  count_entries integer; distinct_count integer; position_value integer:=0; sum_score numeric;
  context jsonb; current_revision bigint; candidates jsonb; primary_expected text; quota_total integer;
  policy_limit numeric; score_value numeric; signals jsonb; current_interest_count integer;
  recomputed double precision; mapped_lane text;
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
  if ((r->'policy' ? 'qualified_shortfalls') or r->'policy'->>'revision'='3'
    or r->'policy'->>'policy_id'='discovery-policy-r3') and (r->'policy'->>'revision' is distinct from '3'
    or r->'policy'->>'policy_id' is distinct from 'discovery-policy-r3'
    or r->'policy'->'qualified_shortfalls' is distinct from '{"trend":"hot","deliberate_surprise":"surprise"}'::jsonb)
    then raise exception 'private discovery qualified shortfall policy invalid'; end if;
  for band in select x->>'band' as key,x as value from jsonb_array_elements(r->'bands') x loop
    if band.value->>'verdict' not in ('PASS','DISABLED','QUALIFIED_SHORTFALL') or band.value->>'verdict' is null then raise exception 'private discovery band did not pass'; end if;
    if (r->'policy'->'bands'->band.key->>'active')::boolean then
      if band.value->>'verdict'='QUALIFIED_SHORTFALL' then
        mapped_lane:=case band.key when 'trend' then 'hot' when 'deliberate_surprise' then 'surprise' else null end;
        if mapped_lane is null or r->'policy'->>'revision' is distinct from '3'
          or r->'policy'->>'policy_id' is distinct from 'discovery-policy-r3'
          or r->'policy'->'qualified_shortfalls' is distinct from '{"trend":"hot","deliberate_surprise":"surprise"}'::jsonb
          or jsonb_typeof(band.value->'achieved') is distinct from 'number'
          or jsonb_typeof(r->'policy'->'bands'->band.key->'floor') is distinct from 'number'
          or jsonb_typeof(r->'policy'->'bands'->band.key->'cap') is distinct from 'number'
          or jsonb_typeof(r->'policy'->'bands'->band.key->'min_distinct') is distinct from 'number'
          or band.value->'active' is distinct from r->'policy'->'bands'->band.key->'active'
          or band.value->'floor' is distinct from r->'policy'->'bands'->band.key->'floor'
          or band.value->'cap' is distinct from r->'policy'->'bands'->band.key->'cap'
          or band.value->'min_distinct' is distinct from r->'policy'->'bands'->band.key->'min_distinct'
          or band.value->>'distinct' is distinct from '0'
          or r->'policy'->'bands'->band.key->>'min_distinct' is distinct from '0'
          then raise exception 'private discovery qualified shortfall invalid'; end if;
        select count(*)::double precision/count_entries into recomputed from jsonb_array_elements(r->'entries') x where (x->'lane_scores') ? mapped_lane;
        if recomputed is distinct from (band.value->>'achieved')::double precision
          or recomputed>=(r->'policy'->'bands'->band.key->>'floor')::double precision
          or recomputed>(r->'policy'->'bands'->band.key->>'cap')::double precision
          or (band.value->>'distinct')::integer<(r->'policy'->'bands'->band.key->>'min_distinct')::integer
          or (r->'shortfalls'->>mapped_lane)::integer<=0 then raise exception 'private discovery qualified shortfall invalid'; end if;
      elsif band.value->>'verdict'<>'PASS' or jsonb_typeof(band.value->'achieved') is distinct from 'number'
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
