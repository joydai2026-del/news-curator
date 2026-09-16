begin;

-- Only settled, non-quarantined translations of the exact public original
-- may become presentation text. Model inputs remain the original columns.
create index if not exists translation_cache_reader_idx
  on translation_private.translation_cache(story_id, target_locale, input_digest, created_at desc);

create or replace view translation_private.retained_localizations as
select o.*, locale.display_language,
  case when o.language = locale.display_language then o.title else t.translated_title end display_title,
  case when o.language = locale.display_language then o.summary else t.translated_description end display_summary,
  (o.language = locale.display_language or t.cache_key_digest is not null) translation_available
from public.retained_corpus_observations o
cross join (values ('en'::text), ('zh'::text)) locale(display_language)
left join lateral (
  select c.cache_key_digest, c.translated_title, c.translated_description
  from translation_private.translation_cache c
  where c.story_id = o.story_id and c.source_locale = o.language
    and c.target_locale = locale.display_language
    and c.normalization_version = 'normalized-item-v1'
    and c.field_selection = case when o.summary = '' then array['title'] else array['title','description'] end
    -- PostgreSQL text cannot contain NUL. Match TranslationInput's byte digest.
    and c.input_digest = encode(extensions.digest(
      convert_to('translation-input-v1', 'UTF8') || decode('00','hex') ||
      convert_to(o.language, 'UTF8') || decode('00','hex') ||
      convert_to(o.title, 'UTF8') || decode('00','hex') || convert_to(o.summary, 'UTF8'), 'sha256'), 'hex')
    and not exists (select 1 from translation_private.translation_cache_quarantine q
      where q.cache_key_digest = c.cache_key_digest)
  order by c.created_at desc, c.cache_key_digest limit 1
) t on o.language <> locale.display_language;
revoke all on translation_private.retained_localizations from public, anon, authenticated;

create or replace function public.m2_localized_candidates(
  p_locale text default 'en', p_category_id text default null, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50
) returns setof jsonb language plpgsql stable security definer
set search_path = pg_catalog, public as $$
begin
  if p_locale is null or p_locale not in ('en','zh') then raise exception 'invalid locale'; end if;
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  if octet_length(p_query) > 2400 then raise exception 'query too long'; end if;
  return query select
    (to_jsonb(o) - 'search_document') || jsonb_build_object('schema_version', 1,
      'category_ids', coalesce(c.category_ids, '[]'::jsonb))
  from translation_private.retained_localizations o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids
    from public.retained_corpus_categories where story_id = o.story_id) c on true
  where o.display_language = p_locale and o.translation_available
    and (p_category_id is null or exists (select 1 from public.retained_corpus_categories
      where story_id = o.story_id and category_id = p_category_id))
    and (p_query is null or btrim(p_query) = '' or
      (p_query !~ '[一-龥]' and (o.search_document @@ websearch_to_tsquery('simple', p_query)
        or to_tsvector('simple', o.display_title || ' ' || o.display_summary) @@ websearch_to_tsquery('simple', p_query))) or
      (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in
        lower(o.title || E'\n' || o.summary || E'\n' || o.display_title || E'\n' || o.display_summary)) > 0))
    and (p_before_published_at is null or o.published_at < p_before_published_at
      or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

-- This public-text overlay never changes Saved membership or exposes owner
-- state. Unknown/private story IDs are absent. Clients render a local notice.
create or replace function public.m2_localized_story_text(p_story_ids text[], p_locale text default 'en')
returns setof jsonb language plpgsql stable security definer
set search_path = pg_catalog, public as $$
begin
  if p_locale is null or p_locale not in ('en','zh') then raise exception 'invalid locale'; end if;
  if p_story_ids is null or cardinality(p_story_ids) > 100 or
    exists (select 1 from unnest(p_story_ids) s where s is null or s !~ '^story:[0-9a-f]{64}$') then
    raise exception 'invalid story ids'; end if;
  return query select jsonb_build_object('story_id', o.story_id,
    'display_language', p_locale, 'translation_available', o.translation_available,
    'title', coalesce(o.display_title, case p_locale when 'en' then 'Translation unavailable' else '翻译暂不可用' end),
    'summary', coalesce(o.display_summary, case p_locale when 'en' then 'The story is saved. You can open the original or try again later.' else '新闻已收藏。你可以阅读原文，或稍后重试。' end))
  from translation_private.retained_localizations o
  where o.story_id = any(p_story_ids) and o.display_language = p_locale;
end;
$$;

-- Bounded public-only queue for background translation; never owner history.
create or replace function public.m2_translation_queue(p_limit integer default 100, p_max_age_hours integer default 48)
returns setof jsonb language plpgsql stable security definer
set search_path = pg_catalog, public as $$
begin
  if p_limit is null or p_limit < 1 or p_limit > 1000 or p_max_age_hours is null
    or p_max_age_hours < 1 or p_max_age_hours > 720 then raise exception 'invalid queue limits'; end if;
  return query select jsonb_build_object('story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'source_id', o.source_id, 'source_name', o.source_name,
    'canonical_url', o.canonical_url, 'published_at', o.published_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb))
  from translation_private.retained_localizations o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids
    from public.retained_corpus_categories where story_id = o.story_id) c on true
  where not o.translation_available and o.published_at >= now() - make_interval(hours => p_max_age_hours)
    and char_length(o.title) <= 500 and char_length(o.summary) <= 2000
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;

revoke all on function public.m2_localized_candidates(text,text,text,timestamptz,text,integer),
  public.m2_localized_story_text(text[],text), public.m2_translation_queue(integer,integer)
  from public, anon, authenticated;
grant execute on function public.m2_localized_candidates(text,text,text,timestamptz,text,integer),
  public.m2_translation_queue(integer,integer) to service_role;
grant execute on function public.m2_localized_story_text(text[],text) to authenticated, service_role;
commit;
