begin;

create index private_discovery_retry_lookup
  on public.private_discovery_editions(owner_user_id,((payload->'receipt'->'bindings'->>'snapshot_digest')));

create function public.private_discovery_retry_identity(
  p_owner_user_id uuid,
  p_snapshot_digest text,
  p_profile_revision bigint,
  p_profile_fingerprint text,
  p_policy_digest text,
  p_code_revision text,
  p_language text,
  p_ranking_configuration_digest text,
  p_display_dedup_digest text,
  p_code_digest text
) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare answer jsonb; matches bigint;
begin
  if p_owner_user_id is null or not exists(select 1 from auth.users where id=p_owner_user_id)
    or p_snapshot_digest is null or p_snapshot_digest !~ '^[0-9a-f]{64}$'
    or p_profile_revision is null or p_profile_revision < 0
    or p_profile_fingerprint is null or p_profile_fingerprint !~ '^[0-9a-f]{64}$'
    or p_policy_digest is null or p_policy_digest !~ '^[0-9a-f]{64}$'
    or p_code_revision is null or p_code_revision !~ '^[0-9a-f]{40}$'
    or p_language is null or p_language not in ('en','zh')
    or p_ranking_configuration_digest is null or p_ranking_configuration_digest !~ '^[0-9a-f]{64}$'
    or p_display_dedup_digest is null or p_display_dedup_digest !~ '^[0-9a-f]{64}$'
    or p_code_digest is null or p_code_digest !~ '^[0-9a-f]{64}$'
    then raise exception 'private discovery retry identity unavailable'; end if;
  select count(*),min(jsonb_build_object('edition_id',edition_id,'payload_digest',payload_digest,
    'receipt_digest',payload->'receipt'->>'receipt_digest','generated_at',generated_at)::text)::jsonb
    into matches,answer
    from public.private_discovery_editions
    where owner_user_id=p_owner_user_id
      and payload->'receipt'->'bindings'->>'snapshot_digest'=p_snapshot_digest
      and (payload->'receipt'->'bindings'->>'profile_revision')::bigint=p_profile_revision
      and payload->>'profile_fingerprint'=p_profile_fingerprint
      and payload->'receipt'->'bindings'->>'policy_digest'=p_policy_digest
      and payload->>'code_revision'=p_code_revision
      and payload->'receipt'->>'language'=p_language
      and payload->'receipt'->'bindings'->>'ranking_configuration_digest'=p_ranking_configuration_digest
      and payload->'receipt'->'bindings'->>'display_dedup_digest'=p_display_dedup_digest
      and payload->'receipt'->'bindings'->>'code_digest'=p_code_digest;
  if matches>1 then raise exception 'private discovery retry identity ambiguous'; end if;
  return answer;
end;
$$;
revoke all on function public.private_discovery_retry_identity(uuid,text,bigint,text,text,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.private_discovery_retry_identity(uuid,text,bigint,text,text,text,text,text,text,text) to service_role;

commit;
