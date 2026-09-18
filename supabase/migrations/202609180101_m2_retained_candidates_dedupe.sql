begin;

-- B8 bug: the same story twice in one result.
--
-- Reproduced live on 2026-09-15 (world + 中国 returned 25 cards carrying 4 pairs
-- with identical titles from rfi-zh; the 01:42 repeat carried 5, and the All
-- feed carried 1). Root cause, read at ddae2c2:
--
--   * `curator/dedup.py` `dedupe()` DOES collapse two identical titles in one
--     language, but it only ever sees ONE hourly batch. Two observations of the
--     same headline that arrive in different runs are never compared.
--   * The corpus is keyed by `story_id`, which is sha256 of the canonical URL,
--     so a publisher that serves one article at two addresses (or changes the
--     address) durably owns two rows. Neither is wrong; both are real
--     observations.
--   * `curator/grouping.py` `exact_matches()` assigns an `event_group_id` only
--     when a bucket spans TWO LANGUAGES, by design. Two same-language copies
--     therefore never share a group.
--   * `m2_retained_candidates` then projects observations one-for-one, so both
--     reach the ranker and both are shown.
--
-- The fix is a projection rule, not a new heuristic, and it is deliberately not
-- fuzzy: `curator/translation/grouping.py` and `curator/grouping.py` are exact
-- pre-filters by design, and a wrong merge silently deletes a story. Two rows
-- are one story here only when their titles are IDENTICAL after case folding
-- and whitespace collapsing, in the SAME language, inside the same window the
-- in-batch deduper already uses (`sources.yaml` `dedup.time_bucket_hours: 36`).
--
-- One representative is chosen per VISIBLE SET, never per page.
--
-- "Visible set" is load-bearing and was got wrong in the first draft of this
-- migration: the peer subquery read the whole observations table, so the
-- winner could be a row the caller cannot see, and the story then showed ZERO
-- times instead of once. Two ways that happened:
--
--   * twins in different categories. Filtering to one category left the loser,
--     and its winner was not in the filtered set, so the category lost the
--     story entirely.
--   * the exclusive lane. A newer twin with no `exclusivity_decisions` row
--     suppressed the older twin that HAD one, so a genuinely
--     Chinese-exclusive story fell out of "Only in Chinese press".
--
-- The representative is therefore chosen inside a CTE that has already applied
-- the caller's own filters (category, query, and for the lane the decision
-- join and the group check), and the CURSOR is applied afterwards. Filters
-- first means the winner is always a row the caller can see; cursor last means
-- the choice does not depend on which page was asked for, so a duplicate
-- cannot reappear on page 2 after being collapsed on page 1.

create or replace function public.m2_story_dedupe_key(p_title text)
returns text language sql immutable parallel safe
set search_path = pg_catalog, public as $$
  select btrim(regexp_replace(lower(coalesce(p_title, '')), '\s+', ' ', 'g'))
$$;

create index if not exists retained_corpus_observations_dedupe_key_idx
  on public.retained_corpus_observations
  (language, public.m2_story_dedupe_key(title), published_at desc, story_id desc);

-- The five-argument form is dropped rather than replaced: adding a defaulted
-- parameter creates a NEW function, and leaving the old overload in place makes
-- a five-named-argument PostgREST call ambiguous. Same idiom as 202609160003.
drop function if exists public.m2_retained_candidates(text, text, timestamptz, text, integer);

create or replace function public.m2_retained_candidates(
  p_category_id text default null, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50, p_dedupe_window_hours integer default 36
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  -- 0 disables collapsing entirely and restores the one-row-per-observation
  -- behaviour, so the rule is operable without editing this function.
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  return query
  with visible as (
    -- The caller's own filters, and ONLY those. The cursor is deliberately not
    -- here: the representative must not depend on the page being asked for.
    select o.* from public.retained_corpus_observations o
    where (p_category_id is null or exists (select 1 from public.retained_corpus_categories where story_id=o.story_id and category_id=p_category_id))
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
  ), chosen as (
    select v.* from visible v
    where p_dedupe_window_hours = 0 or public.m2_story_dedupe_key(v.title) = '' or not exists (
      select 1 from visible peer
      where peer.language = v.language
        and public.m2_story_dedupe_key(peer.title) = public.m2_story_dedupe_key(v.title)
        and abs(extract(epoch from (peer.published_at - v.published_at))) <= p_dedupe_window_hours * 3600
        and (peer.published_at, peer.story_id) > (v.published_at, v.story_id))
  )
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at, 'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb),
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'event_group_id', o.event_group_id)
  from chosen o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids from public.retained_corpus_categories where story_id = o.story_id) c on true
  where (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

drop function if exists public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text);

create or replace function public.m2_retained_candidates_language_exclusive(
  p_display_language text, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50, p_policy_id text default null,
  p_dedupe_window_hours integer default 36
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public, translation_private as $$
begin
  if p_display_language is null or p_display_language not in ('en', 'zh') then raise exception 'invalid display language'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if p_policy_id is null or p_policy_id !~ '^[A-Za-z0-9._-]{1,64}$' then
    raise exception 'invalid policy id';
  end if;
  if p_dedupe_window_hours is null or p_dedupe_window_hours < 0 or p_dedupe_window_hours > 720 then
    raise exception 'invalid dedupe window';
  end if;
  return query
  with visible as (
    -- Everything that makes a row part of THIS lane: the model's exclusivity
    -- decision under the current policy, the language, and the group check.
    -- A row outside this set must never be able to suppress a row inside it.
    select o.* from public.retained_corpus_observations o
    join translation_private.exclusivity_decisions d
      on d.story_id = o.story_id
     and d.display_language = p_display_language
     and d.outcome = 'exclusive'
     and d.policy_id = p_policy_id
    where o.language <> p_display_language
      and not exists (
        select 1 from public.retained_corpus_observations peer
        where o.event_group_id is not null and peer.event_group_id = o.event_group_id
          and peer.language = p_display_language)
      and (p_query is null or btrim(p_query) = '' or
        (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
        (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
  ), chosen as (
    select v.* from visible v
    where p_dedupe_window_hours = 0 or public.m2_story_dedupe_key(v.title) = '' or not exists (
      select 1 from visible peer
      where peer.language = v.language
        and public.m2_story_dedupe_key(peer.title) = public.m2_story_dedupe_key(v.title)
        and abs(extract(epoch from (peer.published_at - v.published_at))) <= p_dedupe_window_hours * 3600
        and (peer.published_at, peer.story_id) > (v.published_at, v.story_id))
  )
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at,
    'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb),
    'title_translations', o.title_translations, 'summary_translations', o.summary_translations,
    'event_group_id', o.event_group_id)
  from chosen o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids
                     from public.retained_corpus_categories where story_id = o.story_id) c on true
  where (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

revoke all on function public.m2_story_dedupe_key(text) from public, anon, authenticated;
revoke all on function public.m2_retained_candidates(text, text, timestamptz, text, integer, integer)
  from public, anon, authenticated;
revoke all on function public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text, integer)
  from public, anon, authenticated;
grant execute on function public.m2_retained_candidates(text, text, timestamptz, text, integer, integer),
  public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer, text, integer),
  public.m2_story_dedupe_key(text) to service_role;

commit;
