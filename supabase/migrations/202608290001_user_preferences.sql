begin;

create or replace function public.valid_interests(value text[])
returns boolean
language sql
immutable
strict
set search_path = pg_catalog
as $$
  select
    coalesce(array_ndims(value), 1) = 1
    and cardinality(value) <= 20
    and not exists (
      select 1
      from unnest(value) as item
      where item is null
        or octet_length(item) < 1
        or octet_length(item) > 160
        or char_length(item) > 80
        or item ~ '^[[:space:]]|[[:space:]]$'
    )
$$;

create or replace function public.valid_saved_searches(value jsonb)
returns boolean
language plpgsql
immutable
strict
set search_path = pg_catalog
as $$
declare
  item jsonb;
  seen_ids text[] := array[]::text[];
  item_id text;
  item_query text;
begin
  if jsonb_typeof(value) <> 'array'
     or jsonb_array_length(value) > 20
     or octet_length(value::text) > 8192 then
    return false;
  end if;

  for item in select elem from jsonb_array_elements(value) as entries(elem) loop
    if jsonb_typeof(item) <> 'object'
       or not (item ?& array['id', 'query', 'enabled'])
       or exists (
         select 1
         from jsonb_object_keys(item) as keys(key_name)
         where key_name not in ('id', 'query', 'enabled')
       )
       or jsonb_typeof(item -> 'id') <> 'string'
       or jsonb_typeof(item -> 'query') <> 'string'
       or jsonb_typeof(item -> 'enabled') <> 'boolean' then
      return false;
    end if;

    item_id := item ->> 'id';
    item_query := item ->> 'query';
    if octet_length(item_id) < 1
       or octet_length(item_id) > 128
       or char_length(item_id) > 64
       or item_id ~ '^[[:space:]]|[[:space:]]$'
       or octet_length(item_query) < 1
       or octet_length(item_query) > 600
       or char_length(item_query) > 300
       or item_query ~ '^[[:space:]]|[[:space:]]$'
       or item_id = any(seen_ids) then
      return false;
    end if;
    seen_ids := array_append(seen_ids, item_id);
  end loop;
  return true;
end;
$$;

create table public.user_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  revision bigint not null default 0 check (revision >= 0),
  locale text not null default 'en' check (locale in ('en', 'zh')),
  interests text[] not null default '{}'::text[] check (public.valid_interests(interests)),
  saved_searches jsonb not null default '[]'::jsonb check (public.valid_saved_searches(saved_searches)),
  created_at timestamptz not null default statement_timestamp(),
  updated_at timestamptz not null default statement_timestamp()
);

alter table public.user_preferences enable row level security;
alter table public.user_preferences force row level security;

create policy user_preferences_select_own
on public.user_preferences for select
to authenticated
using (auth.uid() is not null and auth.uid() = user_id);

create policy user_preferences_insert_own
on public.user_preferences for insert
to authenticated
with check (auth.uid() is not null and auth.uid() = user_id);

create policy user_preferences_update_own
on public.user_preferences for update
to authenticated
using (auth.uid() is not null and auth.uid() = user_id)
with check (auth.uid() is not null and auth.uid() = user_id);

create policy user_preferences_delete_own
on public.user_preferences for delete
to authenticated
using (auth.uid() is not null and auth.uid() = user_id);

create or replace function public.set_user_preferences_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  new.updated_at := statement_timestamp();
  return new;
end;
$$;

create trigger set_user_preferences_updated_at
before update on public.user_preferences
for each row execute function public.set_user_preferences_updated_at();

create or replace function public.compare_and_swap_user_preferences(
  expected_revision bigint,
  new_locale text,
  new_interests text[],
  new_saved_searches jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
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
     or new_interests is null or not public.valid_interests(new_interests)
     or new_saved_searches is null or not public.valid_saved_searches(new_saved_searches) then
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

revoke all on table public.user_preferences from public, anon, authenticated;
revoke execute on function public.valid_interests(text[]) from public, anon, authenticated;
revoke execute on function public.valid_saved_searches(jsonb) from public, anon, authenticated;
revoke execute on function public.set_user_preferences_updated_at() from public, anon, authenticated;
revoke execute on function public.compare_and_swap_user_preferences(bigint, text, text[], jsonb) from public, anon, authenticated;

grant select, delete on table public.user_preferences to authenticated;
grant insert (user_id, locale, interests, saved_searches) on public.user_preferences to authenticated;
grant execute on function public.compare_and_swap_user_preferences(bigint, text, text[], jsonb) to authenticated;

commit;
