create table public.m2_request_health_buckets (
  bucket_start timestamptz not null,
  endpoint text not null check (endpoint in ('rank','page')),
  outcome text not null check (outcome in ('model','fallback','auth_denied','invalid_request','stale','disabled','server_error','timeout')),
  latency_band text not null check (latency_band in ('lt1s','1to3s','3to6s','6to8s','8to20s','gt20s')),
  request_count bigint not null check (request_count between 1 and 1000000000),
  latest_input_match_count bigint not null check (latest_input_match_count between 0 and request_count),
  updated_at timestamptz not null default statement_timestamp(),
  primary key (bucket_start,endpoint,outcome,latency_band)
);
alter table public.m2_request_health_buckets enable row level security;
alter table public.m2_request_health_buckets force row level security;
revoke all on public.m2_request_health_buckets from public,anon,authenticated;
grant select,insert,update,delete on public.m2_request_health_buckets to service_role;

create or replace function public.m2_record_request_health(p_endpoint text,p_outcome text,p_latency_band text,p_latest_input_match boolean)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
declare bucket timestamptz := date_bin(interval '5 minutes',statement_timestamp(),timestamptz '2000-01-01 00:00:00+00');
begin
  if p_endpoint not in ('rank','page') or p_outcome not in ('model','fallback','auth_denied','invalid_request','stale','disabled','server_error','timeout')
    or p_latency_band not in ('lt1s','1to3s','3to6s','6to8s','8to20s','gt20s') or p_latest_input_match is null then
    raise exception 'invalid request health dimension';
  end if;
  insert into public.m2_request_health_buckets(bucket_start,endpoint,outcome,latency_band,request_count,latest_input_match_count)
  values(bucket,p_endpoint,p_outcome,p_latency_band,1,case when p_latest_input_match then 1 else 0 end)
  on conflict(bucket_start,endpoint,outcome,latency_band) do update set
    request_count=public.m2_request_health_buckets.request_count+1,
    latest_input_match_count=public.m2_request_health_buckets.latest_input_match_count+excluded.latest_input_match_count,
    updated_at=statement_timestamp();
end $$;
revoke execute on function public.m2_record_request_health(text,text,text,boolean) from public,anon,authenticated;
grant execute on function public.m2_record_request_health(text,text,text,boolean) to service_role;

-- Service-only retention for non-identifying counters, called by the collector.
create or replace function public.m2_prune_request_health(p_retention_days integer)
returns integer language plpgsql security definer set search_path=pg_catalog,public as $$
declare removed integer;
begin
  if p_retention_days is null or p_retention_days < 1 or p_retention_days > 90 then
    raise exception 'invalid health retention'; end if;
  delete from public.m2_request_health_buckets
    where bucket_start < statement_timestamp() - make_interval(days => p_retention_days);
  get diagnostics removed = row_count;
  return removed;
end $$;
revoke execute on function public.m2_prune_request_health(integer) from public,anon,authenticated;
grant execute on function public.m2_prune_request_health(integer) to service_role;
