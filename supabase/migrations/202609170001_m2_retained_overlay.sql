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
-- merge-not-erase in exactly the same shape as the ingest path: translations
-- are added key by key (||) and an existing event_group_id is never replaced.
-- A story that is not in the corpus is skipped, never inserted, because the
-- corpus write is the only thing allowed to create a row.
create or replace function public.m2_apply_retained_overlay(p_rows jsonb)
returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare row jsonb; touched text; updated_count integer := 0;
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
       or exists (
         select 1 from jsonb_each(coalesce(row->'title_translations', '{}'::jsonb) || coalesce(row->'summary_translations', '{}'::jsonb)) entry
         where entry.key not in ('en', 'zh') or jsonb_typeof(entry.value) <> 'string'
       ) then raise exception 'invalid translation overlay'; end if;
    touched := null;
    update public.retained_corpus_observations o set
      title_translations = o.title_translations || coalesce(row->'title_translations', '{}'::jsonb),
      summary_translations = o.summary_translations || coalesce(row->'summary_translations', '{}'::jsonb),
      event_group_id = coalesce(o.event_group_id, row->>'event_group_id'),
      last_ready_at = now()
    where o.story_id = row->>'story_id'
    returning o.story_id into touched;
    if touched is not null then updated_count := updated_count + 1; end if;
  end loop;
  return updated_count;
end;
$$;

revoke all on function public.m2_apply_retained_overlay(jsonb) from public, anon, authenticated;
grant execute on function public.m2_apply_retained_overlay(jsonb) to service_role;

commit;
