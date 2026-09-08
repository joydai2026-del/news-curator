begin;

alter table public.feed_policy
  add column dashboard_topic_limit integer not null default 20
  check (dashboard_topic_limit between 1 and 100);

create or replace function public.dashboard_summary() returns jsonb
language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare
  caller_id uuid := auth.uid();
  topic_limit integer;
  answer jsonb;
begin
  if caller_id is null then
    raise exception using errcode = '42501', message = 'authentication required';
  end if;
  select dashboard_topic_limit into topic_limit
  from public.feed_policy where singleton;
  if topic_limit is null then raise exception 'feed policy unavailable'; end if;

  with state_counts as (
    select count(*) filter (where saved_at is not null) saved_count,
      count(*) filter (where saved_at is not null and read_at is null) saved_unread_count,
      count(*) filter (where read_at is not null) read_count
    from public.user_story_state where user_id = caller_id
  ), signal_counts as (
    select topic_id,
      count(*) filter (where signal = 'more_like') more_like_count,
      count(*) filter (where signal = 'less_like') less_like_count,
      count(*) signal_count
    from public.user_story_interests where user_id = caller_id
    group by topic_id
  ), selected_signals as (
    select topic_id, more_like_count, less_like_count, signal_count
    from signal_counts order by signal_count desc, topic_id limit topic_limit
  ), signals as (
    select count(*) active_interest_signal_count
    from public.user_story_interests where user_id = caller_id
  )
  select jsonb_build_object(
    'schema_version', 1,
    'scope', 'current_retained_state',
    'snapshot_at', statement_timestamp(),
    'saved_count', state_counts.saved_count,
    'saved_unread_count', state_counts.saved_unread_count,
    'read_count', state_counts.read_count,
    'active_interest_signal_count', signals.active_interest_signal_count,
    'topic_signals', coalesce((select jsonb_agg(jsonb_build_object(
      'topic_id', topic_id,
      'more_like_count', more_like_count,
      'less_like_count', less_like_count
    ) order by signal_count desc, topic_id) from selected_signals), '[]'::jsonb)
  ) into answer
  from state_counts cross join signals;
  if (answer->>'saved_count')::numeric > 9007199254740991
     or (answer->>'saved_unread_count')::numeric > 9007199254740991
     or (answer->>'read_count')::numeric > 9007199254740991
     or (answer->>'active_interest_signal_count')::numeric > 9007199254740991
     or exists (
       select 1 from jsonb_array_elements(answer->'topic_signals') row
       where (row->>'more_like_count')::numeric > 9007199254740991
          or (row->>'less_like_count')::numeric > 9007199254740991
     ) then
    raise exception 'dashboard counts exceed safe response range';
  end if;
  return answer;
end;
$$;

revoke execute on function public.dashboard_summary() from public, anon, authenticated;
grant execute on function public.dashboard_summary() to authenticated;

commit;
