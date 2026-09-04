begin;

create schema if not exists personalization_private;
revoke all on schema personalization_private from public, anon, authenticated;

-- Preserve the CHECK-constraint function identities while moving them out of
-- PostgREST's exposed public schema. Authenticated owners need EXECUTE to have
-- PostgreSQL evaluate the checks during their first direct insert.
alter function public.valid_interests(text[]) set schema personalization_private;
alter function public.valid_saved_searches(jsonb) set schema personalization_private;

grant usage on schema personalization_private to authenticated;
grant execute on function personalization_private.valid_interests(text[]) to authenticated;
grant execute on function personalization_private.valid_saved_searches(jsonb) to authenticated;

create or replace function public.compare_and_swap_user_preferences(
  expected_revision bigint,
  new_locale text,
  new_interests text[],
  new_saved_searches jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, personalization_private
as $$
declare
  caller_id uuid := auth.uid();
  updated_row public.user_preferences%rowtype;
  current_revision bigint;
begin
  if caller_id is null then
    raise exception using errcode = '42501', message = 'authentication required';
  end if;
  if expected_revision is null or expected_revision < 0 then
    raise exception using errcode = '22023', message = 'invalid expected revision';
  end if;
  if new_locale is null or new_locale not in ('en', 'zh')
     or new_interests is null or not personalization_private.valid_interests(new_interests)
     or new_saved_searches is null or not personalization_private.valid_saved_searches(new_saved_searches) then
    raise exception using errcode = '22023', message = 'invalid preference fields';
  end if;

  update public.user_preferences
  set locale = new_locale,
      interests = new_interests,
      saved_searches = new_saved_searches,
      revision = revision + 1
  where user_id = caller_id and revision = expected_revision
  returning * into updated_row;

  if found then
    return jsonb_build_object(
      'status', 'updated',
      'revision', updated_row.revision,
      'updated_at', updated_row.updated_at
    );
  end if;

  select revision into current_revision
  from public.user_preferences
  where user_id = caller_id;

  if found then
    return jsonb_build_object('status', 'conflict', 'revision', current_revision);
  end if;
  return jsonb_build_object('status', 'not_found');
end;
$$;

commit;
