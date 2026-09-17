begin;

-- Round-3 review, critical finding: the model's answer never reached the
-- surface. The lane decided exclusivity by "no peer shares my event_group_id",
-- which is true for a group of one, so a story the model ruled MATCHED and a
-- story it never decided at all were both shown under "Only in Chinese press".
-- That is the reported bug, inverted, and no amount of group-id plumbing fixes
-- it, because absence of a group cannot distinguish "nobody covered this" from
-- "nobody has looked yet".
--
-- Exclusivity is now READ FROM THE PERSISTED DECISION and from nothing else:
-- exactly the stories the model ruled exclusive for this display language,
-- under the current policy. Undecided and matched stories are not shown.
create or replace function public.m2_retained_candidates_language_exclusive(
  p_display_language text, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50, p_policy_id text default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if p_display_language is null or p_display_language not in ('en', 'zh') then raise exception 'invalid display language'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  -- Required: a null policy id used to mean "any policy", so decisions made by
  -- a superseded prompt kept being served after an upgrade.
  if p_policy_id is null or p_policy_id !~ '^[A-Za-z0-9._-]{1,64}$' then
    raise exception 'invalid policy id';
  end if;
  return query
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at,
    'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb),
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'event_group_id', o.event_group_id)
  from public.retained_corpus_observations o
  join translation_private.exclusivity_decisions d
    on d.story_id = o.story_id
   and d.display_language = p_display_language
   and d.outcome = 'exclusive'
   and d.policy_id = p_policy_id
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids
                     from public.retained_corpus_categories where story_id = o.story_id) c on true
  where o.language <> p_display_language
    -- Belt and braces: a decision must never outlive a group that says otherwise.
    and not exists (
      select 1 from public.retained_corpus_observations peer
      where o.event_group_id is not null and peer.event_group_id = o.event_group_id
        and peer.language = p_display_language)
    and (p_query is null or btrim(p_query) = '' or
      (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
      (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
    and (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

revoke all on function public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text)
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text)
  to service_role;

-- The five-argument form from 202609160001 is dropped: leaving it would keep a
-- second, group-id-only definition of exclusivity reachable by the service.
drop function if exists public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer);

-- Same idiom for the three signatures that changed in 202609160002. `create or
-- replace` with a new parameter list creates a NEW function and leaves the old
-- overload (and its grant) in place, so an environment that applied an earlier
-- text of that file would keep calling the superseded behaviour.
drop function if exists public.m2_settle_translation_spend(numeric, numeric);
drop function if exists public.m2_settle_translation_spend(numeric, numeric, text);
drop function if exists public.m2_release_translation_spend(numeric);
drop function if exists public.m2_mark_exclusivity_rechecked(text, text, text);
drop function if exists public.m2_record_exclusivity_decision(text, text, text, text);
drop function if exists public.m2_read_exclusivity_decisions(text[]);

commit;
