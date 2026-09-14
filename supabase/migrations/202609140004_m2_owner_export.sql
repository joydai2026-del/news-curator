begin;

alter table public.feed_policy
  add column owner_export_page_size integer not null default 100 check (owner_export_page_size between 1 and 500),
  add column owner_export_page_bytes integer not null default 196608 check (owner_export_page_bytes between 32768 and 786432),
  add column owner_export_download_bytes bigint not null default 33554432 check (owner_export_download_bytes between 1048576 and 134217728);

-- Internal row projection has no owner parameter and exposes no credentials,
-- action receipts, model outputs or other owners. The public RPC owns paging.
create function public.m2_owner_export_rows()
returns table(section text, row_key text, value jsonb)
language sql stable security definer set search_path=pg_catalog,public as $$
  select 'behavior_events',lpad(event_revision::text,20,'0'),
    jsonb_build_object('event_id',event_id,'event_revision',event_revision,'schema_version',schema_version,
      'actor_kind',actor_kind,'event_type',event_type,'payload',payload,'occurred_at',occurred_at,'recorded_at',recorded_at)
    from public.user_behavior_events where user_id=auth.uid()
  union all select 'behavior_settings','settings',to_jsonb(s)-'user_id'
    from public.user_behavior_settings s where user_id=auth.uid()
  union all select 'behavior_revisions','revisions',to_jsonb(r)-'user_id'
    from public.user_behavior_revisions r where user_id=auth.uid()
  union all select 'behavior_profile','profile',to_jsonb(p)-'user_id'
    from public.user_behavior_profile_state p where user_id=auth.uid()
  union all select 'preferences','preferences',to_jsonb(p)-'user_id'
    from public.user_preferences p where user_id=auth.uid()
  union all select 'reading_state',story_id,to_jsonb(s)-'user_id'
    from public.user_story_state s where user_id=auth.uid()
  union all select 'story_interests',story_id||':'||topic_id,to_jsonb(i)-'user_id'
    from public.user_story_interests i where user_id=auth.uid()
$$;
revoke all on function public.m2_owner_export_rows() from public,anon,authenticated;

create function public.m2_owner_export_page(p_cursor text default null,p_expected_fence text default null)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare caller uuid:=auth.uid(); cursor_value jsonb; start_at bigint:=0; required_fence text:=p_expected_fence;
  page_size integer; page_bytes integer; download_bytes bigint; answer jsonb; fence text; total bigint; exported bigint;
begin
  if caller is null then raise exception 'authentication required' using errcode='42501'; end if;
  perform set_config('response.headers','[{"Cache-Control":"private, no-store"}]',true);
  select owner_export_page_size,owner_export_page_bytes,owner_export_download_bytes
    into page_size,page_bytes,download_bytes from public.feed_policy where singleton;
  if page_size is null then raise exception 'export policy unavailable'; end if;
  if p_cursor is not null then
    if octet_length(p_cursor)>2048 then raise exception 'invalid export cursor'; end if;
    begin cursor_value:=convert_from(decode(p_cursor,'base64'),'UTF8')::jsonb;
    exception when others then raise exception 'invalid export cursor'; end;
    if jsonb_typeof(cursor_value) is distinct from 'object'
       or cursor_value->>'owner_id' is distinct from caller::text
       or coalesce(cursor_value->>'offset','') !~ '^[0-9]+$'
       or coalesce(cursor_value->>'fence','') !~ '^[0-9a-f]{64}$' then raise exception 'invalid export cursor'; end if;
    start_at:=(cursor_value->>'offset')::bigint;
    if required_fence is not null and required_fence<>cursor_value->>'fence' then raise exception 'export fence mismatch'; end if;
    required_fence:=cursor_value->>'fence';
  end if;
  if required_fence is not null and required_fence !~ '^[0-9a-f]{64}$' then raise exception 'invalid export fence'; end if;
  -- One statement snapshot supplies both the complete fingerprint and page.
  -- No history-window limit and no persistent raw export snapshot are used.
  with rows as materialized (
    select section,row_key,value,row_number() over(order by section,row_key)-1 ordinal
    from public.m2_owner_export_rows()
  ), summary as (
    select count(*) count,encode(extensions.digest(convert_to(
      caller::text||':'||coalesce(string_agg(encode(extensions.digest(convert_to(
        jsonb_build_array(section,row_key,value)::text,'UTF8'),'sha256'),'hex'),'' order by ordinal),''),
      'UTF8'),'sha256'),'hex') hash from rows
  ), candidates as (
    select *,sum(octet_length(jsonb_build_object('section',section,'key',row_key,'value',value)::text)+2)
      over(order by ordinal) bytes from (select * from rows where ordinal>=start_at order by ordinal limit page_size) limited
  ), selected as (select * from candidates where bytes<=page_bytes)
  select summary.hash,summary.count,(select count(*) from selected),
    jsonb_build_object('schema_version',1,'owner_id',caller,'fence',summary.hash,'offset',start_at,
      'total_rows',summary.count,'max_download_bytes',download_bytes,
      'rows',coalesce((select jsonb_agg(jsonb_build_object('section',section,'key',row_key,'value',value) order by ordinal) from selected),'[]'::jsonb))
    into fence,total,exported,answer from summary;
  if required_fence is not null and required_fence<>fence then raise exception 'export changed; restart download'; end if;
  if start_at>total or (start_at<total and exported=0) then raise exception 'export page exceeds policy; contact operator'; end if;
  return answer||jsonb_build_object('next_cursor',case when start_at+exported<total then
    encode(convert_to(jsonb_build_object('owner_id',caller,'fence',fence,'offset',start_at+exported)::text,'UTF8'),'base64') end);
end; $$;
revoke all on function public.m2_owner_export_page(text,text) from public,anon,authenticated;
grant execute on function public.m2_owner_export_page(text,text) to authenticated;

commit;
