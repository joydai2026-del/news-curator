-- Read-state only, before composition spends scarce source/window slots.
-- The caller is the authenticated reader, never a supplied owner identifier.
create or replace function public.m2_opened_candidate_ids(p_story_ids text[])
returns setof text language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare caller uuid := auth.uid();
begin
  if caller is null then raise exception 'authentication required' using errcode='42501'; end if;
  -- Covers the largest validated pending pool plus one new fetch. This is a
  -- protocol ceiling, not a feed size or an invitation to scan owner history.
  if p_story_ids is null or cardinality(p_story_ids) > 10000 or exists (
    select 1 from unnest(p_story_ids) story_id
    where story_id is null or story_id !~ '^story:[0-9a-f]{64}$'
  ) then raise exception 'invalid story ids'; end if;
  return query select state.story_id from public.user_story_state state
    where state.user_id=caller and state.read_at is not null
      and state.story_id=any(p_story_ids);
end; $$;
revoke all on function public.m2_opened_candidate_ids(text[]) from public,anon,service_role;
grant execute on function public.m2_opened_candidate_ids(text[]) to authenticated;
