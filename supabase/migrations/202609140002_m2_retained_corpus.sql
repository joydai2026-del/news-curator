begin;

-- One current, public-origin candidate per canonical story. The publication
-- table remains a downstream selection and never defines corpus membership.
create table public.retained_corpus_observations (
  story_id text primary key references public.canonical_stories(story_id) on delete restrict,
  source_id text not null check (source_id <> '' and octet_length(source_id) <= 512),
  source_name text not null check (source_name <> '' and octet_length(source_name) <= 1000),
  -- Attribution only, never a relevance signal. This is the durable form of
  -- M1's publisher-first dedupe rule: submitter-written aggregator text must
  -- not replace a publisher's title or source on a partial later observation.
  source_is_aggregator boolean not null,
  origin_class text not null default 'public_outlet' check (origin_class = 'public_outlet'),
  language text not null check (language in ('en', 'zh')),
  title text not null check (title <> '' and octet_length(title) <= 8000),
  summary text not null default '' check (octet_length(summary) <= 32000),
  canonical_url text not null check (canonical_url = '' or canonical_url ~ '^https?://'),
  published_at timestamptz not null,
  first_observed_at timestamptz not null,
  source_observed_at timestamptz not null,
  first_ingested_at timestamptz not null default now(),
  last_ingested_at timestamptz not null default now(),
  first_ready_at timestamptz not null default now(),
  last_ready_at timestamptz not null default now(),
  search_document tsvector generated always as (
    to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))
  ) stored
);
create index retained_corpus_fresh_idx on public.retained_corpus_observations(published_at desc, story_id desc);
create index retained_corpus_fts_idx on public.retained_corpus_observations using gin(search_document);

create table public.retained_corpus_categories (
  story_id text not null references public.retained_corpus_observations(story_id) on delete restrict,
  category_id text not null check (category_id ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
  primary key (story_id, category_id)
);
create index retained_corpus_categories_lookup_idx on public.retained_corpus_categories(category_id, story_id);

-- Each source owns only its latest classification for a story. The public
-- category set is the union of these current per-source observations, so a
-- source can retract its own stale category without erasing another source's.
create table public.retained_corpus_source_categories (
  story_id text not null references public.retained_corpus_observations(story_id) on delete restrict,
  source_id text not null check (source_id <> '' and octet_length(source_id) <= 512),
  source_observed_at timestamptz not null,
  category_ids text[] not null,
  primary key (story_id, source_id)
);

alter table public.retained_corpus_observations enable row level security;
alter table public.retained_corpus_observations force row level security;
alter table public.retained_corpus_categories enable row level security;
alter table public.retained_corpus_categories force row level security;
alter table public.retained_corpus_source_categories enable row level security;
alter table public.retained_corpus_source_categories force row level security;
revoke all on public.retained_corpus_observations, public.retained_corpus_categories, public.retained_corpus_source_categories from public, anon, authenticated;
grant select, insert, update, delete on public.retained_corpus_observations, public.retained_corpus_categories, public.retained_corpus_source_categories to service_role;

create or replace function public.m2_ingest_retained_corpus(p_rows jsonb)
returns integer language plpgsql security definer set search_path = pg_catalog, public as $$
declare row jsonb; changed_story text; category_story text; changed boolean; inserted_count integer := 0; observed timestamptz;
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception 'service role required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) > 20000 then
    raise exception 'invalid retained corpus rows';
  end if;
  for row in select value from jsonb_array_elements(p_rows) loop
    if jsonb_typeof(row) <> 'object' or not (row ?& array['story_id','origin_class','source_kind','canonical_url','title','summary','language','source_id','source_name','source_is_aggregator','published_at','source_observed_at','category_ids']) then
      raise exception 'invalid retained corpus row';
    end if;
    if jsonb_typeof(row->'category_ids') <> 'array' or jsonb_typeof(row->'source_is_aggregator') <> 'boolean' or row->>'origin_class' <> 'public_outlet' or row->>'source_kind' <> 'outlet' or exists (select 1 from jsonb_object_keys(row) key where key not in ('story_id','origin_class','source_kind','canonical_url','title','summary','language','source_id','source_name','source_is_aggregator','published_at','source_observed_at','category_ids')) or exists (select 1 from jsonb_array_elements(row->'category_ids') category(value) where jsonb_typeof(category.value) <> 'string' or category.value #>> '{}' !~ '^[a-z0-9][a-z0-9-]{0,79}$') then raise exception 'invalid public corpus row'; end if;
    observed := (row->>'source_observed_at')::timestamptz;
    insert into public.canonical_stories(story_id, canonical_url, title, summary, language, source_kind, source_name, published_at)
    values (row->>'story_id', row->>'canonical_url', row->>'title', coalesce(row->>'summary',''), row->>'language', 'outlet', row->>'source_name', (row->>'published_at')::timestamptz)
    on conflict (story_id) do nothing;
    insert into public.retained_corpus_observations(story_id, source_id, source_name, source_is_aggregator, language, title, summary, canonical_url, published_at, first_observed_at, source_observed_at, first_ingested_at, last_ingested_at, first_ready_at, last_ready_at)
    values (row->>'story_id', row->>'source_id', row->>'source_name', (row->>'source_is_aggregator')::boolean, row->>'language', row->>'title', coalesce(row->>'summary',''), row->>'canonical_url', (row->>'published_at')::timestamptz, observed, observed, now(), now(), now(), now())
    on conflict (story_id) do update set
      source_id = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.source_id else excluded.source_id end,
      source_name = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.source_name else excluded.source_name end,
      source_is_aggregator = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then false else excluded.source_is_aggregator end,
      language = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.language else excluded.language end,
      title = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.title else excluded.title end,
      summary = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.summary else excluded.summary end,
      canonical_url = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.canonical_url else excluded.canonical_url end,
      published_at = case when not public.retained_corpus_observations.source_is_aggregator and excluded.source_is_aggregator then public.retained_corpus_observations.published_at else least(public.retained_corpus_observations.published_at, excluded.published_at) end,
      source_observed_at = excluded.source_observed_at, last_ingested_at = now(), last_ready_at = now()
      where excluded.source_observed_at > public.retained_corpus_observations.source_observed_at
    returning story_id into changed_story;
    changed := found;
    category_story := null;
    insert into public.retained_corpus_source_categories(story_id, source_id, source_observed_at, category_ids)
      select row->>'story_id', row->>'source_id', observed,
        coalesce(array_agg(distinct category.value order by category.value), '{}'::text[])
      from jsonb_array_elements_text(row->'category_ids') as category(value)
    on conflict (story_id, source_id) do update set
      source_observed_at = excluded.source_observed_at, category_ids = excluded.category_ids
      where excluded.source_observed_at > public.retained_corpus_source_categories.source_observed_at
    returning story_id into category_story;
    if category_story is not null then
      delete from public.retained_corpus_categories c where c.story_id = category_story;
      insert into public.retained_corpus_categories(story_id, category_id)
        select category_story, category_id
        from public.retained_corpus_source_categories s
        cross join lateral unnest(s.category_ids) category_id
        where s.story_id = category_story
        group by category_id;
    end if;
    if changed or category_story is not null then inserted_count := inserted_count + 1; end if;
  end loop;
  return inserted_count;
end;
$$;

create or replace function public.m2_retained_candidates(
  p_category_id text default null, p_query text default null,
  p_before_published_at timestamptz default null, p_before_story_id text default null,
  p_limit integer default 50
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
begin
  if num_nonnulls(p_before_published_at, p_before_story_id) not in (0, 2) then raise exception 'invalid cursor'; end if;
  if p_limit is null or p_limit < 1 or p_limit > 100 then raise exception 'invalid limit'; end if;
  return query
  select jsonb_build_object('schema_version', 1, 'story_id', o.story_id, 'title', o.title, 'summary', o.summary,
    'language', o.language, 'canonical_url', o.canonical_url, 'source_id', o.source_id, 'source_name', o.source_name,
    'published_at', o.published_at, 'source_observed_at', o.source_observed_at, 'first_ingested_at', o.first_ingested_at, 'last_ingested_at', o.last_ingested_at, 'first_ready_at', o.first_ready_at, 'last_ready_at', o.last_ready_at,
    'category_ids', coalesce(c.category_ids, '[]'::jsonb))
  from public.retained_corpus_observations o
  left join lateral (select jsonb_agg(category_id order by category_id) category_ids from public.retained_corpus_categories where story_id = o.story_id) c on true
  where (p_category_id is null or exists (select 1 from public.retained_corpus_categories where story_id=o.story_id and category_id=p_category_id))
    and (p_query is null or btrim(p_query) = '' or
      (p_query !~ '[一-龥]' and o.search_document @@ websearch_to_tsquery('simple', p_query)) or
      (p_query ~ '[一-龥]' and position(lower(btrim(p_query)) in lower(o.title || E'\n' || o.summary)) > 0))
    and (p_before_published_at is null or o.published_at < p_before_published_at or (o.published_at = p_before_published_at and o.story_id < p_before_story_id))
  order by o.published_at desc, o.story_id desc limit p_limit;
end;
$$;
revoke all on function public.m2_ingest_retained_corpus(jsonb), public.m2_retained_candidates(text, text, timestamptz, text, integer) from public, anon, authenticated;
grant execute on function public.m2_ingest_retained_corpus(jsonb), public.m2_retained_candidates(text, text, timestamptz, text, integer) to service_role;

commit;
