begin;

-- M2.1 Phase 2, fix round 1: the retained corpus gets a retention window.
--
-- retained_corpus_observations had no prune. The dedupe CTE scans it and the
-- coverage count joins it, so both were scanning a table that only ever grows,
-- and the coverage table added a second unbounded one beside it. A feed that
-- serves a 24-hour trend window does not need a corpus measured in months.
--
-- Children first, because every child reference is `on delete restrict`: that
-- restrict is a deliberate guard against a story vanishing out from under its
-- own coverage, and this function deletes in the order that respects it rather
-- than relaxing it.
create or replace function public.m2_prune_retained_corpus(p_retention_days integer)
returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare cutoff timestamptz; removed integer := 0;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  -- The floor is two days, not one: the trend window is 24 hours, so "hot"
  -- needs yesterday to still be in the table to count anything at all.
  if p_retention_days is null or p_retention_days < 2 or p_retention_days > 90 then
    raise exception 'invalid retention window';
  end if;
  cutoff := now() - make_interval(days => p_retention_days);
  create temporary table if not exists _m2_prune_ids(story_id text primary key) on commit drop;
  delete from _m2_prune_ids;
  -- Age is measured on PUBLICATION, the same clock the reader's windows use, so
  -- a story is pruned when it is old news rather than when it was last touched.
  insert into _m2_prune_ids(story_id)
    select o.story_id from public.retained_corpus_observations o where o.published_at < cutoff;
  delete from public.retained_corpus_coverage c
    where c.story_id in (select story_id from _m2_prune_ids);
  delete from public.retained_corpus_categories c
    where c.story_id in (select story_id from _m2_prune_ids);
  delete from public.retained_corpus_source_categories c
    where c.story_id in (select story_id from _m2_prune_ids);
  delete from public.retained_corpus_observations o
    where o.story_id in (select story_id from _m2_prune_ids);
  get diagnostics removed = row_count;
  return removed;
end;
$$;

revoke all on function public.m2_prune_retained_corpus(integer) from public, anon, authenticated;
grant execute on function public.m2_prune_retained_corpus(integer) to service_role;

commit;
