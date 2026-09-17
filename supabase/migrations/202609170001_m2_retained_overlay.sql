begin;

-- The overlay write, split off from the corpus write.
--
-- The hourly ingest now writes the retained corpus FIRST and decides pairing
-- and translation afterwards, so a cancelled run still updates the corpus
-- (2026-09-17: the pairing loop ran past the job's 14-minute timeout and the
-- whole run, corpus write included, was lost). The second step cannot reuse
-- m2_ingest_retained_corpus: that upsert only updates when
-- excluded.source_observed_at > the stored one, so a second call carrying the
-- SAME rows is a no-op and the overlay would never land.
--
-- This function therefore writes the three overlay columns and nothing else,
-- merge-not-erase: translations are added key by key (||), an empty string is
-- dropped before the merge so a failed translation cannot replace a stored one,
-- and an existing event_group_id is never replaced.
-- A story that is not in the corpus is skipped, never inserted, because the
-- corpus write is the only thing allowed to create a row.
create or replace function public.m2_apply_retained_overlay(p_rows jsonb)
returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare row jsonb; touched text; updated_count integer := 0; skipped_count integer := 0;
        new_title jsonb; new_summary jsonb;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) > 20000 then
    raise exception 'invalid retained overlay rows';
  end if;
  for row in select value from jsonb_array_elements(p_rows) loop
    if jsonb_typeof(row) <> 'object'
       or not (row ? 'story_id') or jsonb_typeof(row->'story_id') <> 'string'
       or exists (select 1 from jsonb_object_keys(row) key
                  where key not in ('story_id', 'title_translations', 'summary_translations', 'event_group_id'))
       or (row ? 'title_translations' and jsonb_typeof(row->'title_translations') <> 'object')
       or (row ? 'summary_translations' and jsonb_typeof(row->'summary_translations') <> 'object')
       or (row ? 'event_group_id' and row->>'event_group_id' !~ '^group:[0-9a-f]{32}$')
       -- Each overlay object is validated on its own. Concatenating them first
       -- let a duplicate language key hide an invalid value in one of them.
       or exists (
         select 1 from jsonb_each(coalesce(row->'title_translations', '{}'::jsonb)) entry
         where entry.key not in ('en', 'zh') or jsonb_typeof(entry.value) <> 'string'
       )
       or exists (
         select 1 from jsonb_each(coalesce(row->'summary_translations', '{}'::jsonb)) entry
         where entry.key not in ('en', 'zh') or jsonb_typeof(entry.value) <> 'string'
       ) then raise exception 'invalid translation overlay'; end if;
    touched := null;
    -- An empty string is a translation failure, not a translation: merging it
    -- would replace a good stored value with nothing. `||` overwrites on key
    -- collision, so the empty keys are dropped BEFORE the merge.
    select coalesce(jsonb_object_agg(entry.key, entry.value), '{}'::jsonb) into new_title
      from jsonb_each(coalesce(row->'title_translations', '{}'::jsonb)) entry
      where entry.value #>> '{}' <> '';
    select coalesce(jsonb_object_agg(entry.key, entry.value), '{}'::jsonb) into new_summary
      from jsonb_each(coalesce(row->'summary_translations', '{}'::jsonb)) entry
      where entry.value #>> '{}' <> '';
    -- Per-row isolation: one row that trips a table CHECK (an oversized
    -- translation) must not roll back every other row in the same call.
    begin
      update public.retained_corpus_observations o set
        title_translations = o.title_translations || new_title,
        summary_translations = o.summary_translations || new_summary,
        event_group_id = coalesce(o.event_group_id, row->>'event_group_id'),
        last_ready_at = now()
      where o.story_id = row->>'story_id'
        -- Only a row this actually CHANGES is written, so `updated_count` counts
        -- changes and a re-sent overlay does not bump last_ready_at for ever.
        and (o.title_translations <> o.title_translations || new_title
             or o.summary_translations <> o.summary_translations || new_summary
             or (o.event_group_id is null and row->>'event_group_id' is not null))
      returning o.story_id into touched;
      if touched is not null then updated_count := updated_count + 1; end if;
    exception when check_violation or data_exception then
      skipped_count := skipped_count + 1;
      raise warning 'retained overlay row skipped: %', row->>'story_id';
    end;
  end loop;
  if skipped_count > 0 then
    raise warning 'retained overlay skipped % row(s)', skipped_count;
  end if;
  return updated_count;
end;
$$;

revoke all on function public.m2_apply_retained_overlay(jsonb) from public, anon, authenticated;
grant execute on function public.m2_apply_retained_overlay(jsonb) to service_role;

commit;
