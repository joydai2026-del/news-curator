begin;

-- Additive archive and private reading-state contract.

create table public.feed_policy (
  singleton boolean primary key default true check (singleton),
  revision bigint not null default 1 check (revision > 0),
  initial_window_days integer not null default 5 check (initial_window_days between 1 and 30),
  unsaved_retention_days integer not null default 30 check (unsaved_retention_days between initial_window_days and 365),
  page_size integer not null default 20 check (page_size between 1 and 100),
  refresh_poll_seconds integer not null default 300 check (refresh_poll_seconds between 30 and 86400),
  physical_purge_grace_days integer not null default 7 check (physical_purge_grace_days between 1 and 90),
  more_like_topic_weight numeric not null default 0.8 check (more_like_topic_weight between 0 and 10),
  updated_at timestamptz not null default now()
);
insert into public.feed_policy(singleton) values (true);

create table public.canonical_stories (
  story_id text primary key check (story_id ~ '^story:[0-9a-f]{64}$'),
  canonical_url_hash char(64) generated always as (
    case when canonical_url = '' then substring(story_id from 7)
    else encode(extensions.digest(canonical_url, 'sha256'), 'hex') end
  ) stored unique,
  canonical_url text not null check (octet_length(canonical_url) <= 8192 and
    (canonical_url = '' or (canonical_url ~ '^https?://[^/@[:space:]]+(/|$)'
      and canonical_url !~ '#'
      and canonical_url !~* '[?&](utm_[^=]*|fbclid|gclid|mc_cid|mc_eid)='
      and split_part(split_part(canonical_url, '://', 2), '/', 1)
        = lower(split_part(split_part(canonical_url, '://', 2), '/', 1))
      and split_part(split_part(canonical_url, '://', 2), '/', 1) !~ '^www\\.'
      and story_id = 'story:' || encode(extensions.digest(canonical_url, 'sha256'), 'hex')))),
  title text not null check (title <> '' and octet_length(title) <= 8000),
  summary text not null default '' check (octet_length(summary) <= 32000),
  language text not null check (language in ('en', 'zh')),
  published_at timestamptz not null,
  first_archived_at timestamptz not null default now(),
  last_archived_at timestamptz not null default now()
);
create index canonical_stories_feed_idx on public.canonical_stories(published_at desc, story_id);

create table public.story_aliases (
  normalized_url text primary key check (normalized_url ~ '^https?://' and octet_length(normalized_url) <= 8192),
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  match_method text not null check (match_method in ('exact', 'fuzzy', 'reviewed')),
  confidence numeric not null default 1 check (confidence between 0 and 1),
  created_at timestamptz not null default now()
);
create index story_aliases_story_idx on public.story_aliases(story_id);

create table public.coverage_mentions (
  mention_id text primary key check (mention_id ~ '^mention:[0-9a-f]{64}$'),
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  source_kind text not null check (source_kind in ('outlet', 'newsletter')),
  source_id text not null check (source_id <> '' and octet_length(source_id) <= 512),
  source_name text not null check (source_name <> '' and octet_length(source_name) <= 1000),
  article_url text not null check (article_url ~ '^https?://' and octet_length(article_url) <= 8192),
  headline text not null check (octet_length(headline) <= 8000),
  mentioned_at timestamptz not null
);
create index coverage_mentions_story_source_idx on public.coverage_mentions(story_id, source_kind, source_id);

create table public.story_topics (
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  topic_id text not null check (topic_id ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
  topic_name text not null check (topic_name <> '' and octet_length(topic_name) <= 1000),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  primary key (story_id, topic_id)
);
create index story_topics_topic_idx on public.story_topics(topic_id, story_id);

create table public.publication_runs (
  publication_seq bigint generated always as identity primary key,
  build_nonce text not null unique check (build_nonce <> '' and octet_length(build_nonce) <= 1000),
  commit_sha text not null check (commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  deployed_url text not null check (deployed_url ~ '^https://' and octet_length(deployed_url) <= 8192),
  built_at timestamptz not null,
  finalized_at timestamptz not null default now()
  ,candidate_digest text not null check (candidate_digest ~ '^[0-9a-f]{64}$')
  ,site_sha256 text not null check (site_sha256 ~ '^[0-9a-f]{64}$')
);
create index publication_runs_latest_idx on public.publication_runs(finalized_at desc, publication_seq desc);

create table public.publication_topics (
  publication_seq bigint not null references public.publication_runs(publication_seq) on delete restrict,
  topic_id text not null check (topic_id ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
  topic_name text not null check (topic_name <> '' and octet_length(topic_name) <= 1000),
  position integer not null check (position > 0),
  primary key (publication_seq, topic_id),
  unique (publication_seq, position)
);

create table public.publication_entries (
  publication_seq bigint not null references public.publication_runs(publication_seq) on delete restrict,
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  topic_id text not null,
  position integer not null check (position > 0),
  canonical_url text not null check (octet_length(canonical_url) <= 8192 and
    (canonical_url = '' or canonical_url ~ '^https?://[^/@[:space:]]+(/|$)')),
  title text not null check (title <> '' and octet_length(title) <= 8000),
  summary text not null check (octet_length(summary) <= 32000),
  language text not null check (language in ('en', 'zh')),
  published_at timestamptz not null,
  score_components jsonb not null check (jsonb_typeof(score_components) = 'object' and octet_length(score_components::text) <= 8192),
  ordering_mode text not null check (ordering_mode in ('weighted_total', 'preference_then_freshness', 'native_rank_then_freshness')),
  ordering_key jsonb not null check (jsonb_typeof(ordering_key) = 'object' and octet_length(ordering_key::text) <= 2048),
  primary key (publication_seq, topic_id, story_id),
  unique (publication_seq, topic_id, position),
  foreign key (publication_seq, topic_id)
    references public.publication_topics(publication_seq, topic_id) on delete restrict
);
create index publication_entries_story_idx on public.publication_entries(story_id, publication_seq desc);

create table public.user_story_state (
  user_id uuid not null references auth.users(id) on delete cascade,
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  read_at timestamptz,
  saved_at timestamptz,
  revision bigint not null default 1 check (revision > 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, story_id)
);
create index user_story_state_saved_idx on public.user_story_state(user_id, saved_at desc, story_id) where saved_at is not null;

create table public.user_story_interests (
  user_id uuid not null references auth.users(id) on delete cascade,
  story_id text not null references public.canonical_stories(story_id) on delete restrict,
  topic_id text not null check (topic_id ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
  signal text not null check (signal in ('more_like', 'less_like')),
  revision bigint not null default 1 check (revision > 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, story_id, topic_id),
  foreign key (story_id, topic_id)
    references public.story_topics(story_id, topic_id) on delete restrict
);

create table public.user_action_receipts (
  user_id uuid not null references auth.users(id) on delete cascade,
  idempotency_key text not null check (idempotency_key <> '' and octet_length(idempotency_key) <= 512),
  operation text not null check (operation in ('set_story_state', 'set_story_interest')),
  resource_key text not null check (resource_key <> '' and octet_length(resource_key) <= 512),
  request_digest text not null check (request_digest ~ '^[0-9a-f]{64}$'),
  response jsonb not null check (jsonb_typeof(response) = 'object' and octet_length(response::text) <= 8192),
  created_at timestamptz not null default now(),
  primary key (user_id, idempotency_key)
);

alter table public.feed_policy enable row level security;
alter table public.feed_policy force row level security;
alter table public.canonical_stories enable row level security;
alter table public.canonical_stories force row level security;
alter table public.story_aliases enable row level security;
alter table public.story_aliases force row level security;
alter table public.coverage_mentions enable row level security;
alter table public.coverage_mentions force row level security;
alter table public.story_topics enable row level security;
alter table public.story_topics force row level security;
alter table public.publication_runs enable row level security;
alter table public.publication_runs force row level security;
alter table public.publication_topics enable row level security;
alter table public.publication_topics force row level security;
alter table public.publication_entries enable row level security;
alter table public.publication_entries force row level security;
alter table public.user_story_state enable row level security;
alter table public.user_story_state force row level security;
alter table public.user_story_interests enable row level security;
alter table public.user_story_interests force row level security;
alter table public.user_action_receipts enable row level security;
alter table public.user_action_receipts force row level security;

create policy user_story_state_owner on public.user_story_state to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());
create policy user_story_interests_owner on public.user_story_interests to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());
create policy user_action_receipts_owner on public.user_action_receipts to authenticated
using (auth.uid() is not null and user_id = auth.uid())
with check (auth.uid() is not null and user_id = auth.uid());

create or replace function public.latest_publication() returns jsonb
language sql stable security definer set search_path = pg_catalog, public as $$
  select coalesce((select jsonb_build_object(
    'publication_seq', pr.publication_seq, 'finalized_at', pr.finalized_at,
    'topics', coalesce((select jsonb_agg(jsonb_build_object('topic_id', pt.topic_id, 'name', pt.topic_name)
      order by pt.position) from public.publication_topics pt where pt.publication_seq = pr.publication_seq), '[]'::jsonb),
    'initial_history_cursor', jsonb_build_object(
      'before_published_at', pr.built_at - make_interval(days => fp.initial_window_days),
      'before_story_id', ''),
    'poll_seconds', (select refresh_poll_seconds from public.feed_policy where singleton)
  ) from public.publication_runs pr cross join public.feed_policy fp
  where pr.finalized_at is not null and fp.singleton
  order by pr.publication_seq desc limit 1), '{}'::jsonb)
$$;

create or replace function public.feed_page(
  p_topic_id text default null,
  p_order_mode text default null,
  p_after_position integer default null,
  p_after_story_id text default null,
  p_before_published_at timestamptz default null,
  p_before_story_id text default null,
  p_limit integer default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare policy_page_size integer; requested_mode text;
begin
  select page_size into policy_page_size from public.feed_policy where singleton;
  if policy_page_size is null then raise exception 'feed policy unavailable'; end if;
  if p_limit is not null and (p_limit < 1 or p_limit > policy_page_size) then
    raise exception 'limit exceeds feed policy';
  end if;
  requested_mode := coalesce(p_order_mode,
    case when p_topic_id is null then 'history_freshness' else 'edition_rank' end);
  if requested_mode not in ('edition_rank', 'history_freshness') then
    raise exception 'invalid order mode';
  end if;
  if requested_mode = 'edition_rank' and (p_topic_id is null
     or p_before_published_at is not null or p_before_story_id is not null) then
    raise exception 'edition rank requires one topic and no freshness cursor';
  end if;
  if requested_mode = 'history_freshness'
     and (p_after_position is not null or p_after_story_id is not null) then
    raise exception 'history freshness does not accept a position cursor';
  end if;
  if (p_before_published_at is null) <> (p_before_story_id is null) then
    raise exception 'both cursor fields are required';
  end if;
  if requested_mode = 'edition_rank' and (p_after_position is null) <> (p_after_story_id is null) then
    raise exception 'position and story cursor are required together';
  end if;
  if p_topic_id is not null and p_topic_id !~ '^[a-z0-9][a-z0-9-]{0,79}$' then
    raise exception 'invalid topic';
  end if;
  if p_before_story_id is not null and p_before_story_id <> ''
     and p_before_story_id !~ '^story:[0-9a-f]{64}$' then
    raise exception 'invalid story cursor';
  end if;
  if p_after_story_id is not null and p_after_story_id !~ '^story:[0-9a-f]{64}$' then
    raise exception 'invalid story cursor';
  end if;
  if requested_mode = 'edition_rank' then
    return query
    with latest as (
      select publication_seq from public.publication_runs where finalized_at is not null
      order by publication_seq desc limit 1
    )
    select jsonb_build_object(
      'story_id', pe.story_id, 'canonical_url', pe.canonical_url, 'title', pe.title,
      'summary', pe.summary, 'language', pe.language, 'published_at', pe.published_at,
      'publication_seq', pe.publication_seq, 'position', pe.position,
      'score_components', pe.score_components, 'ordering_mode', pe.ordering_mode,
      'ordering_key', pe.ordering_key, 'page_order_mode', 'edition_rank',
      'next_cursor', jsonb_build_object('after_position', pe.position, 'after_story_id', pe.story_id),
      'topic_ids', coalesce((select jsonb_agg(topic_id order by topic_id) from (
        select distinct pe2.topic_id from public.publication_entries pe2
        where pe2.publication_seq = pe.publication_seq and pe2.story_id = pe.story_id
      ) edition_topic_ids), '[]'::jsonb),
      'coverage_mentions', coalesce((select jsonb_agg(to_jsonb(bounded_coverage) order by bounded_coverage.mentioned_at desc)
        from (select cm.source_kind, cm.source_id, cm.source_name, cm.article_url as url, cm.headline, cm.mentioned_at
          from public.coverage_mentions cm where cm.story_id = pe.story_id
          order by cm.mentioned_at desc, cm.mention_id limit 20) bounded_coverage), '[]'::jsonb),
      'read_at', us.read_at, 'saved_at', us.saved_at, 'state_revision', coalesce(us.revision, 0),
      'interests', coalesce(interests.rows, '[]'::jsonb))
    from latest join public.publication_entries pe using (publication_seq)
    left join public.user_story_state us on us.story_id = pe.story_id and us.user_id = auth.uid()
    left join lateral (
      select jsonb_agg(jsonb_build_object('topic_id', topic_id, 'signal', signal,
        'revision', revision) order by topic_id) rows
      from public.user_story_interests where user_id = auth.uid() and story_id = pe.story_id
    ) interests on true
    where pe.topic_id = p_topic_id and pe.canonical_url <> ''
      and (p_after_position is null or pe.position > p_after_position
        or (pe.position = p_after_position and pe.story_id > p_after_story_id))
    order by pe.position, pe.story_id
    limit least(coalesce(p_limit, policy_page_size), policy_page_size);
    return;
  end if;
  return query
  with fp as (select * from public.feed_policy where singleton), edition_clock as (
    select built_at from public.publication_runs where finalized_at is not null
    order by publication_seq desc limit 1
  ), eligible as (
    select pe.*,
      row_number() over (partition by pe.story_id order by pe.publication_seq desc, pe.position, pe.topic_id) as story_version
    from public.publication_entries pe
    join public.publication_runs pr using (publication_seq)
    join public.canonical_stories s using (story_id)
    left join public.user_story_state us on us.story_id = s.story_id and us.user_id = auth.uid()
    cross join fp cross join edition_clock ec
    where pr.finalized_at is not null
      and (p_topic_id is null or pe.topic_id = p_topic_id)
      and (us.saved_at is not null or pe.published_at >= ec.built_at - make_interval(days =>
        case when p_before_published_at is null then fp.initial_window_days else fp.unsaved_retention_days end))
  )
  select jsonb_build_object(
    'story_id', s.story_id, 'canonical_url', e.canonical_url, 'title', e.title,
    'summary', e.summary, 'language', e.language, 'published_at', e.published_at,
    'publication_seq', e.publication_seq, 'position', e.position, 'score_components', e.score_components,
    'ordering_mode', e.ordering_mode, 'ordering_key', e.ordering_key,
    'topic_ids', coalesce((select jsonb_agg(topic_id order by topic_id) from (
      select distinct pe2.topic_id from public.publication_entries pe2
      join public.publication_runs pr2 using (publication_seq)
      where pe2.story_id = s.story_id and pr2.finalized_at is not null
    ) story_topic_ids), '[]'::jsonb),
    'page_order_mode', 'history_freshness',
    'next_cursor', jsonb_build_object('before_published_at', e.published_at, 'before_story_id', s.story_id),
    'coverage_mentions', coalesce((select jsonb_agg(to_jsonb(bounded_coverage) order by bounded_coverage.mentioned_at desc)
      from (select cm.source_kind, cm.source_id, cm.source_name, cm.article_url as url, cm.headline, cm.mentioned_at
        from public.coverage_mentions cm where cm.story_id = s.story_id
        order by cm.mentioned_at desc, cm.mention_id limit 20) bounded_coverage), '[]'::jsonb),
    'read_at', us.read_at, 'saved_at', us.saved_at,
    'state_revision', coalesce(us.revision, 0),
    'interests', coalesce(interests.rows, '[]'::jsonb)
  )
  from eligible e join public.canonical_stories s using (story_id)
  left join public.user_story_state us on us.story_id = s.story_id and us.user_id = auth.uid()
  left join lateral (
    select jsonb_agg(jsonb_build_object('topic_id', topic_id, 'signal', signal,
      'revision', revision) order by topic_id) rows
    from public.user_story_interests where user_id = auth.uid() and story_id = s.story_id
  ) interests on true
  where e.story_version = 1
    and e.canonical_url <> ''
    and (p_before_published_at is null or e.published_at < p_before_published_at
      or (e.published_at = p_before_published_at and s.story_id > coalesce(p_before_story_id, '')))
  order by e.published_at desc, s.story_id
  limit least(coalesce(p_limit, policy_page_size), policy_page_size)
  ;
end;
$$;

create or replace function public.saved_page(
  p_before_saved_at timestamptz default null,
  p_before_story_id text default null,
  p_limit integer default null
) returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare policy_page_size integer;
begin
  if auth.uid() is null then raise exception 'authentication required' using errcode = '42501'; end if;
  select page_size into policy_page_size from public.feed_policy where singleton;
  if policy_page_size is null then raise exception 'feed policy unavailable'; end if;
  if p_limit is not null and (p_limit < 1 or p_limit > policy_page_size) then
    raise exception 'limit exceeds feed policy';
  end if;
  if (p_before_saved_at is null) <> (p_before_story_id is null) then
    raise exception 'both cursor fields are required';
  end if;
  if p_before_story_id is not null and p_before_story_id <> ''
     and p_before_story_id !~ '^story:[0-9a-f]{64}$' then
    raise exception 'invalid story cursor';
  end if;
  return query select jsonb_build_object('story_id', s.story_id,
    'title', coalesce(card.title, s.title), 'summary', coalesce(card.summary, s.summary),
    'canonical_url', coalesce(card.canonical_url, s.canonical_url),
    'language', coalesce(card.language, s.language),
    'published_at', coalesce(card.published_at, s.published_at),
    'publication_seq', coalesce(card.publication_seq, 0),
    'position', coalesce(card.position, 0),
    'topic_ids', coalesce(card.topic_ids, (select jsonb_agg(st.topic_id order by st.topic_id)
      from public.story_topics st where st.story_id = s.story_id), '[]'::jsonb),
    'coverage_mentions', coalesce((select jsonb_agg(to_jsonb(cm) - 'mention_id' order by cm.mentioned_at desc, cm.mention_id)
      from (select mention_id, source_kind, source_id, source_name, article_url as url,
        headline, mentioned_at from public.coverage_mentions
        where story_id = s.story_id order by mentioned_at desc, mention_id limit 20) cm), '[]'::jsonb),
    'score_components', coalesce(card.score_components, '{}'::jsonb),
    'ordering_mode', coalesce(card.ordering_mode, 'weighted_total'),
    'ordering_key', coalesce(card.ordering_key, '{}'::jsonb),
    'page_order_mode', 'saved_at',
    'next_cursor', jsonb_build_object('before_saved_at', us.saved_at, 'before_story_id', s.story_id),
    'saved_at', us.saved_at, 'read_at', us.read_at, 'state_revision', us.revision,
    'interests', coalesce(interests.rows, '[]'::jsonb))
  from public.user_story_state us join public.canonical_stories s using (story_id)
  left join lateral (
    select pe.publication_seq, pe.topic_id, pe.position, pe.title, pe.summary, pe.canonical_url,
      pe.language, pe.published_at, pe.score_components, pe.ordering_mode, pe.ordering_key,
      (select jsonb_agg(distinct pe2.topic_id order by pe2.topic_id)
        from public.publication_entries pe2 where pe2.publication_seq = pe.publication_seq
        and pe2.story_id = pe.story_id) topic_ids
    from public.publication_entries pe join public.publication_runs pr using (publication_seq)
    join public.publication_topics pt using (publication_seq, topic_id)
    where pe.story_id = s.story_id and pr.finalized_at is not null
    order by pe.publication_seq desc, pt.position, pe.position limit 1
  ) card on true
  left join lateral (
    select jsonb_agg(jsonb_build_object('topic_id', topic_id, 'signal', signal,
        'revision', revision) order by topic_id) rows
    from public.user_story_interests where user_id = auth.uid() and story_id = s.story_id
  ) interests on true
  where user_id = auth.uid() and us.saved_at is not null
    and coalesce(card.canonical_url, s.canonical_url) ~ '^https?://[^/@[:space:]]+(/|$)'
    and (p_before_saved_at is null or us.saved_at < p_before_saved_at
      or (us.saved_at = p_before_saved_at and s.story_id > coalesce(p_before_story_id, '')))
  order by us.saved_at desc, s.story_id
  limit least(coalesce(p_limit, policy_page_size), policy_page_size);
end;
$$;

create or replace function public.updates_since(
  p_since_publication_seq bigint,
  p_after_publication_seq bigint default null,
  p_after_published_at timestamptz default null,
  p_after_story_id text default null,
  p_limit integer default null
)
returns setof jsonb language plpgsql stable security definer set search_path = pg_catalog, public as $$
declare policy_page_size integer;
begin
  if p_since_publication_seq is null or p_since_publication_seq < 0 then
    raise exception 'publication sequence must be nonnegative';
  end if;
  if num_nonnulls(p_after_publication_seq, p_after_published_at, p_after_story_id) not in (0, 3)
     or (p_after_publication_seq is not null and p_after_publication_seq <= p_since_publication_seq)
     or (p_after_story_id is not null and p_after_story_id !~ '^story:[0-9a-f]{64}$') then
    raise exception 'invalid updates cursor';
  end if;
  select page_size into policy_page_size from public.feed_policy where singleton;
  if policy_page_size is null then raise exception 'feed policy unavailable'; end if;
  if p_limit is not null and (p_limit < 1 or p_limit > policy_page_size) then
    raise exception 'limit exceeds feed policy';
  end if;
  return query
  with changed_rows as (
    select pe.*, row_number() over (partition by pe.story_id
      order by pe.publication_seq desc, pe.position, pe.topic_id) story_version
    from public.publication_entries pe join public.publication_runs pr using (publication_seq)
    where pe.publication_seq > p_since_publication_seq and pr.finalized_at is not null
  ), changed as (
    select * from changed_rows where story_version = 1
  )
  select jsonb_build_object('publication_seq', c.publication_seq, 'story_id', c.story_id,
    'title', c.title, 'published_at', c.published_at,
    'topic_ids', coalesce((select jsonb_agg(topic_id order by topic_id) from (
      select distinct pe2.topic_id from public.publication_entries pe2
      where pe2.publication_seq = c.publication_seq and pe2.story_id = c.story_id
    ) update_topic_ids), '[]'::jsonb),
    'next_cursor', jsonb_build_object('after_publication_seq', c.publication_seq,
      'after_published_at', c.published_at, 'after_story_id', c.story_id))
  from changed c
  where p_after_publication_seq is null
    or c.publication_seq < p_after_publication_seq
    or (c.publication_seq = p_after_publication_seq and c.published_at < p_after_published_at)
    or (c.publication_seq = p_after_publication_seq and c.published_at = p_after_published_at
      and c.story_id > p_after_story_id)
  order by c.publication_seq desc, c.published_at desc, c.story_id
  limit least(coalesce(p_limit, policy_page_size), policy_page_size);
end;
$$;

create or replace function public.set_story_state(
  p_story_id text, p_read boolean, p_saved boolean, p_expected_revision bigint, p_idempotency_key text
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); current_revision bigint; answer jsonb; resource text; request_hash text;
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_story_id is null or p_story_id !~ '^story:[0-9a-f]{64}$'
     or p_read is null or p_saved is null
     or p_expected_revision is null or p_expected_revision < 0
     or p_idempotency_key is null or p_idempotency_key = ''
     or octet_length(p_idempotency_key) > 512 then
    raise exception 'invalid state write';
  end if;
  if p_saved and not exists (
    select 1 from public.story_topics where story_id = p_story_id
  ) then
    raise exception 'story is not saveable';
  end if;
  resource := p_story_id;
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'story_id', p_story_id, 'read', p_read, 'saved', p_saved,
    'expected_revision', p_expected_revision)::text, 'UTF8'), 'sha256'), 'hex');
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':state:' || resource, 0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key, 0));
  select response into answer from public.user_action_receipts where user_id = caller and idempotency_key = p_idempotency_key;
  if answer is not null then
    if not exists (select 1 from public.user_action_receipts where user_id = caller
      and idempotency_key = p_idempotency_key and operation = 'set_story_state'
      and resource_key = resource and request_digest = request_hash) then
      raise exception 'idempotency key reuse mismatch';
    end if;
    return answer;
  end if;
  select revision into current_revision from public.user_story_state
    where user_id = caller and story_id = p_story_id for update;
  if coalesce(current_revision, 0) <> p_expected_revision then
    answer := jsonb_build_object('status', 'conflict', 'revision', coalesce(current_revision, 0));
    insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
      values (caller, p_idempotency_key, 'set_story_state', resource, request_hash, answer);
    return answer;
  end if;
  insert into public.user_story_state(user_id, story_id, read_at, saved_at, revision)
  values (caller, p_story_id, case when p_read then now() end, case when p_saved then now() end, 1)
  on conflict (user_id, story_id) do update set
    read_at = case when p_read then coalesce(user_story_state.read_at, now()) else null end,
    saved_at = case when p_saved then coalesce(user_story_state.saved_at, now()) else null end,
    revision = user_story_state.revision + 1, updated_at = now()
  returning jsonb_build_object('status', 'updated', 'revision', revision, 'read_at', read_at, 'saved_at', saved_at) into answer;
  insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
    values (caller, p_idempotency_key, 'set_story_state', resource, request_hash, answer);
  return answer;
end;
$$;

create or replace function public.set_story_interest(
  p_story_id text, p_topic_id text, p_signal text, p_expected_revision bigint, p_idempotency_key text
) returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare caller uuid := auth.uid(); current_revision bigint; answer jsonb; resource text; request_hash text;
begin
  if caller is null then raise exception 'authentication required' using errcode = '42501'; end if;
  if p_story_id is null or p_story_id !~ '^story:[0-9a-f]{64}$'
     or p_topic_id is null or p_topic_id !~ '^[a-z0-9][a-z0-9-]{0,79}$'
     or p_signal is null or p_signal not in ('more_like', 'less_like')
     or p_expected_revision is null or p_expected_revision < 0
     or p_idempotency_key is null or p_idempotency_key = ''
     or octet_length(p_idempotency_key) > 512 then
    raise exception 'invalid interest write';
  end if;
  resource := p_story_id || ':' || p_topic_id;
  request_hash := encode(extensions.digest(convert_to(jsonb_build_object(
    'story_id', p_story_id, 'topic_id', p_topic_id, 'signal', p_signal,
    'expected_revision', p_expected_revision)::text, 'UTF8'), 'sha256'), 'hex');
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':interest:' || resource, 0));
  perform pg_advisory_xact_lock(hashtextextended(caller::text || ':' || p_idempotency_key, 0));
  select response into answer from public.user_action_receipts where user_id = caller and idempotency_key = p_idempotency_key;
  if answer is not null then
    if not exists (select 1 from public.user_action_receipts where user_id = caller
      and idempotency_key = p_idempotency_key and operation = 'set_story_interest'
      and resource_key = resource and request_digest = request_hash) then
      raise exception 'idempotency key reuse mismatch';
    end if;
    return answer;
  end if;
  select revision into current_revision from public.user_story_interests
    where user_id = caller and story_id = p_story_id and topic_id = p_topic_id for update;
  if coalesce(current_revision, 0) <> p_expected_revision then
    answer := jsonb_build_object('status', 'conflict', 'revision', coalesce(current_revision, 0));
    insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
      values (caller, p_idempotency_key, 'set_story_interest', resource, request_hash, answer);
    return answer;
  end if;
  insert into public.user_story_interests(user_id, story_id, topic_id, signal, revision)
  values (caller, p_story_id, p_topic_id, p_signal, 1)
  on conflict (user_id, story_id, topic_id) do update set signal = excluded.signal,
    revision = user_story_interests.revision + 1, updated_at = now()
  returning jsonb_build_object('status', 'updated', 'revision', revision, 'signal', signal) into answer;
  insert into public.user_action_receipts(user_id, idempotency_key, operation, resource_key, request_digest, response)
    values (caller, p_idempotency_key, 'set_story_interest', resource, request_hash, answer);
  return answer;
end;
$$;

create or replace function public.finalize_archive(p_candidate jsonb, p_deployed_url text)
returns jsonb language plpgsql security definer set search_path = pg_catalog, public as $$
declare seq bigint; row jsonb; candidate_digest_value text; existing public.publication_runs%rowtype; built_at_value timestamptz;
begin
  if jsonb_typeof(p_candidate) is distinct from 'object'
     or array(select key from jsonb_object_keys(p_candidate) as key order by key)
        <> array['aliases','build_nonce','built_at','commit_sha','coverage_mentions','entries','schema_version','site_sha256','stories','topics']
     or p_candidate->>'schema_version' is distinct from '1'
     or jsonb_typeof(p_candidate->'stories') is distinct from 'array'
     or jsonb_typeof(p_candidate->'aliases') is distinct from 'array'
     or jsonb_typeof(p_candidate->'coverage_mentions') is distinct from 'array'
     or jsonb_typeof(p_candidate->'topics') is distinct from 'array'
     or jsonb_typeof(p_candidate->'entries') is distinct from 'array'
     or jsonb_array_length(p_candidate->'stories') > 500
     or jsonb_array_length(p_candidate->'aliases') > 500
     or jsonb_array_length(p_candidate->'entries') > 5000
     or jsonb_array_length(p_candidate->'coverage_mentions') > 5000
     or jsonb_array_length(p_candidate->'topics') > 100
     or p_candidate->>'build_nonce' is null or p_candidate->>'build_nonce' = ''
     or octet_length(p_candidate->>'build_nonce') > 1000
     or p_candidate->>'commit_sha' is null
     or p_candidate->>'commit_sha' !~ '^[0-9a-f]{40}([0-9a-f]{24})?$'
     or p_candidate->>'site_sha256' is null
     or p_candidate->>'site_sha256' !~ '^[0-9a-f]{64}$'
     or p_candidate->>'built_at' is null
     or p_candidate->>'built_at' !~ '(z|[+-][0-9]{2}:[0-9]{2})$'
     or octet_length(p_candidate::text) > 4000000 then
    raise exception 'invalid archive candidate';
  end if;
  if p_deployed_url is null or p_deployed_url !~ '^https://'
     or octet_length(p_deployed_url) > 8192 then
    raise exception 'invalid deployed url';
  end if;
  begin
    built_at_value := (p_candidate->>'built_at')::timestamptz;
  exception when invalid_datetime_format or datetime_field_overflow then
    raise exception 'invalid archive candidate';
  end;
  if built_at_value > now() + interval '10 minutes' then
    raise exception 'archive built_at is in the future';
  end if;
  candidate_digest_value := encode(extensions.digest(convert_to(p_candidate::text, 'UTF8'), 'sha256'), 'hex');
  perform pg_advisory_xact_lock(hashtextextended(p_candidate->>'build_nonce', 0));
  select * into existing from public.publication_runs where build_nonce = p_candidate->>'build_nonce';
  if found then
    if existing.candidate_digest <> candidate_digest_value
       or existing.commit_sha <> p_candidate->>'commit_sha'
       or existing.deployed_url <> p_deployed_url
       or existing.site_sha256 <> p_candidate->>'site_sha256' then
      raise exception 'publication replay mismatch';
    end if;
    return jsonb_build_object('publication_seq', existing.publication_seq,
      'build_nonce', existing.build_nonce, 'candidate_digest', existing.candidate_digest,
      'commit_sha', existing.commit_sha, 'deployed_url', existing.deployed_url,
      'site_sha256', existing.site_sha256);
  end if;
  insert into public.publication_runs(build_nonce, commit_sha, deployed_url, built_at, candidate_digest, site_sha256)
  values (p_candidate->>'build_nonce', p_candidate->>'commit_sha', p_deployed_url,
    built_at_value, candidate_digest_value, p_candidate->>'site_sha256')
  on conflict (build_nonce) do nothing returning publication_seq into seq;
  if seq is null then raise exception 'concurrent publication replay'; end if;
  for row in select value from jsonb_array_elements(p_candidate->'stories') loop
    insert into public.canonical_stories(story_id, canonical_url, title, summary, language, published_at)
    values (row->>'story_id', row->>'canonical_url', row->>'title', row->>'summary', row->>'language', (row->>'published_at')::timestamptz)
    on conflict (story_id) do update set canonical_url=excluded.canonical_url, title=excluded.title,
      summary=excluded.summary, language=excluded.language, published_at=excluded.published_at, last_archived_at=now();
  end loop;
  for row in select value from jsonb_array_elements(p_candidate->'aliases') loop
    if exists (select 1 from public.story_aliases
      where normalized_url = row->>'normalized_url' and story_id <> row->>'story_id') then
      raise exception 'story alias ownership mismatch';
    end if;
    insert into public.story_aliases(normalized_url, story_id, match_method)
    values (row->>'normalized_url', row->>'story_id', row->>'match_method') on conflict (normalized_url) do nothing;
  end loop;
  for row in select value from jsonb_array_elements(p_candidate->'coverage_mentions') loop
    insert into public.coverage_mentions(mention_id, story_id, source_kind, source_id, source_name, article_url, headline, mentioned_at)
    values (row->>'mention_id', row->>'story_id', row->>'source_kind', row->>'source_id', row->>'source_name',
      row->>'url', row->>'headline', (row->>'mentioned_at')::timestamptz) on conflict (mention_id) do nothing;
  end loop;
  insert into public.publication_topics(publication_seq, topic_id, topic_name, position)
  select seq, topic->>'topic_id', topic->>'name', topic_position::integer
  from jsonb_array_elements(p_candidate->'topics') with ordinality as listed(topic, topic_position);
  for row in select value from jsonb_array_elements(p_candidate->'topics') loop
    insert into public.story_topics(story_id, topic_id, topic_name)
    select distinct e->>'story_id', row->>'topic_id', row->>'name'
    from jsonb_array_elements(p_candidate->'entries') e where e->>'topic_id' = row->>'topic_id'
    on conflict (story_id, topic_id) do update set topic_name=excluded.topic_name, last_seen_at=now();
  end loop;
  for row in select value from jsonb_array_elements(p_candidate->'entries') loop
    insert into public.publication_entries(publication_seq, story_id, topic_id, position,
      canonical_url, title, summary, language, published_at,
      score_components, ordering_mode, ordering_key)
    select seq, row->>'story_id', row->>'topic_id', (row->>'position')::integer,
      s.canonical_url, s.title, s.summary, s.language, s.published_at,
      row->'score_components', row->>'ordering_mode', row->'ordering_key'
    from public.canonical_stories s where s.story_id = row->>'story_id';
  end loop;
  return jsonb_build_object('publication_seq', seq, 'build_nonce', p_candidate->>'build_nonce',
    'candidate_digest', candidate_digest_value, 'commit_sha', p_candidate->>'commit_sha',
    'deployed_url', p_deployed_url, 'site_sha256', p_candidate->>'site_sha256');
exception when others then
  if seq is not null then delete from public.publication_runs where publication_seq = seq; end if;
  raise;
end;
$$;

create or replace function public.prune_publication_history() returns jsonb
language plpgsql security definer set search_path = pg_catalog, public as $$
declare cutoff timestamptz; entry_count bigint; topic_count bigint; run_count bigint; saved_count bigint;
begin
  select now() - make_interval(days => unsaved_retention_days + physical_purge_grace_days)
    into cutoff from public.feed_policy where singleton;
  if cutoff is null then raise exception 'feed policy unavailable'; end if;
  select count(*) into saved_count from public.canonical_stories s
    join public.user_story_state us using (story_id) where us.saved_at is not null;
  delete from public.publication_entries pe using public.publication_runs pr
    where pe.publication_seq = pr.publication_seq and pr.built_at < cutoff
      and not exists (select 1 from public.user_story_state us
        where us.story_id = pe.story_id and us.saved_at is not null);
  get diagnostics entry_count = row_count;
  delete from public.publication_topics pt using public.publication_runs pr
    where pt.publication_seq = pr.publication_seq and pr.built_at < cutoff
      and not exists (select 1 from public.publication_entries pe
        where pe.publication_seq = pt.publication_seq and pe.topic_id = pt.topic_id);
  get diagnostics topic_count = row_count;
  delete from public.publication_runs pr where built_at < cutoff
    and not exists (select 1 from public.publication_entries pe
      where pe.publication_seq = pr.publication_seq);
  get diagnostics run_count = row_count;
  return jsonb_build_object('cutoff', cutoff, 'entries_pruned', entry_count,
    'topics_pruned', topic_count, 'runs_pruned', run_count,
    'saved_canonical_stories_preserved', saved_count);
end;
$$;

revoke all on table public.feed_policy from public, anon, authenticated;
revoke all on table public.canonical_stories from public, anon, authenticated;
revoke all on table public.story_aliases from public, anon, authenticated;
revoke all on table public.coverage_mentions from public, anon, authenticated;
revoke all on table public.story_topics from public, anon, authenticated;
revoke all on table public.publication_runs from public, anon, authenticated;
revoke all on table public.publication_topics from public, anon, authenticated;
revoke all on table public.publication_entries from public, anon, authenticated;
revoke all on table public.user_story_state from public, anon, authenticated;
revoke all on table public.user_story_interests from public, anon, authenticated;
revoke all on table public.user_action_receipts from public, anon, authenticated;

revoke execute on function public.latest_publication() from public, anon, authenticated;
revoke execute on function public.feed_page(text, text, integer, text, timestamptz, text, integer) from public, anon, authenticated;
revoke execute on function public.saved_page(timestamptz, text, integer) from public, anon, authenticated;
revoke execute on function public.updates_since(bigint, bigint, timestamptz, text, integer) from public, anon, authenticated;
revoke execute on function public.set_story_state(text, boolean, boolean, bigint, text) from public, anon, authenticated;
revoke execute on function public.set_story_interest(text, text, text, bigint, text) from public, anon, authenticated;
revoke execute on function public.finalize_archive(jsonb, text) from public, anon, authenticated;
revoke execute on function public.prune_publication_history() from public, anon, authenticated;
grant execute on function public.latest_publication() to anon, authenticated;
grant execute on function public.feed_page(text, text, integer, text, timestamptz, text, integer) to anon, authenticated;
grant execute on function public.saved_page(timestamptz, text, integer) to authenticated;
grant execute on function public.updates_since(bigint, bigint, timestamptz, text, integer) to anon, authenticated;
grant execute on function public.set_story_state(text, boolean, boolean, bigint, text) to authenticated;
grant execute on function public.set_story_interest(text, text, text, bigint, text) to authenticated;
grant execute on function public.finalize_archive(jsonb, text) to service_role;
grant execute on function public.prune_publication_history() to service_role;

commit;
