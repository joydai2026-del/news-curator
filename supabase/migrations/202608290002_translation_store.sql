begin;

create schema if not exists translation_private;
revoke all on schema translation_private from public, anon, authenticated;

create extension if not exists pgcrypto with schema extensions;

create or replace function translation_private.cache_key_digest(
  story_id text,
  input_digest text,
  field_selection text[],
  normalization_version text,
  source_locale text,
  target_locale text,
  provider text,
  model_version text,
  glossary_policy_version text,
  candidate_policy_version text
)
returns text
language sql
immutable
strict
set search_path = pg_catalog, extensions
as $$
  select encode(
    extensions.digest(
      convert_to(
        concat_ws('|',
          octet_length('translation-cache-key-v1') || ':translation-cache-key-v1',
          octet_length(story_id) || ':' || story_id,
          octet_length(input_digest) || ':' || input_digest,
          octet_length(array_to_string(field_selection, ',')) || ':' || array_to_string(field_selection, ','),
          octet_length(normalization_version) || ':' || normalization_version,
          octet_length(source_locale) || ':' || source_locale,
          octet_length(target_locale) || ':' || target_locale,
          octet_length(provider) || ':' || provider,
          octet_length(model_version) || ':' || model_version,
          octet_length(glossary_policy_version) || ':' || glossary_policy_version,
          octet_length(candidate_policy_version) || ':' || candidate_policy_version
        ),
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  )
$$;

create table translation_private.translation_cache (
  cache_key_digest text primary key check (cache_key_digest ~ '^[0-9a-f]{64}$'),
  story_id text not null check (
    octet_length(story_id) between 1 and 256
    and story_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
  ),
  input_digest text not null check (input_digest ~ '^[0-9a-f]{64}$'),
  field_selection text[] not null check (field_selection in (array['title'], array['title', 'description'])),
  normalization_version text not null check (normalization_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  source_locale text not null check (source_locale ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'),
  target_locale text not null check (target_locale ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$' and target_locale <> source_locale),
  provider text not null check (provider ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  model_version text not null check (model_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'),
  glossary_policy_version text not null check (glossary_policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  candidate_policy_version text not null check (candidate_policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  translated_title text not null check (char_length(translated_title) between 1 and 2000),
  translated_description text not null default '' check (char_length(translated_description) <= 8000),
  actual_characters bigint not null check (actual_characters >= 0),
  created_at timestamptz not null default clock_timestamp(),
  constraint translation_cache_complete_key_unique unique (
    story_id, input_digest, field_selection, normalization_version,
    source_locale, target_locale, provider, model_version,
    glossary_policy_version, candidate_policy_version
  ),
  constraint translation_cache_digest_matches check (
    cache_key_digest = translation_private.cache_key_digest(
      story_id, input_digest, field_selection, normalization_version,
      source_locale, target_locale, provider, model_version,
      glossary_policy_version, candidate_policy_version
    )
  )
);

create table translation_private.translation_cache_quarantine (
  cache_key_digest text primary key check (cache_key_digest ~ '^[0-9a-f]{64}$'),
  reason_code text not null check (reason_code ~ '^[A-Za-z0-9_-]{1,64}$'),
  quarantined_at timestamptz not null default clock_timestamp()
);

create table translation_private.translation_usage_counters (
  scope_type text not null check (scope_type in ('run', 'day', 'month')),
  scope_key text not null check (octet_length(scope_key) between 1 and 256),
  counted_characters bigint not null default 0 check (counted_characters >= 0),
  updated_at timestamptz not null default clock_timestamp(),
  primary key (scope_type, scope_key)
);

create table translation_private.translation_reservations (
  idempotency_key text primary key check (octet_length(idempotency_key) between 1 and 256),
  request_fingerprint text not null check (request_fingerprint ~ '^[0-9a-f]{64}$'),
  cache_key_digest text not null check (cache_key_digest ~ '^[0-9a-f]{64}$'),
  story_id text not null check (
    octet_length(story_id) between 1 and 256
    and story_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
  ),
  input_digest text not null check (input_digest ~ '^[0-9a-f]{64}$'),
  field_selection text[] not null check (field_selection in (array['title'], array['title', 'description'])),
  normalization_version text not null check (normalization_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  source_locale text not null check (source_locale ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'),
  target_locale text not null check (target_locale ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$' and target_locale <> source_locale),
  provider text not null check (provider ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  model_version text not null check (model_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'),
  glossary_policy_version text not null check (glossary_policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  candidate_policy_version text not null check (candidate_policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  run_id text not null check (octet_length(run_id) between 1 and 256),
  counter_day date not null,
  counter_month date not null check (counter_month = date_trunc('month', counter_month)::date),
  reserved_characters bigint not null check (reserved_characters > 0),
  actual_characters bigint check (actual_characters >= 0 and actual_characters <= reserved_characters),
  settlement_digest text check (settlement_digest is null or settlement_digest ~ '^[0-9a-f]{64}$'),
  run_limit bigint not null check (run_limit >= 0),
  day_limit bigint not null check (day_limit >= 0),
  month_limit bigint not null check (month_limit >= 0),
  state text not null check (state in ('leased', 'sent', 'settled', 'failed_before_send', 'charge_unknown', 'charged_without_cache')),
  created_at timestamptz not null default clock_timestamp(),
  sent_at timestamptz,
  finalized_at timestamptz,
  constraint translation_reservation_digest_matches check (
    cache_key_digest = translation_private.cache_key_digest(
      story_id, input_digest, field_selection, normalization_version,
      source_locale, target_locale, provider, model_version,
      glossary_policy_version, candidate_policy_version
    )
  )
);

create unique index translation_one_unresolved_per_cache
on translation_private.translation_reservations (cache_key_digest)
where state in ('leased', 'sent', 'charge_unknown', 'charged_without_cache');

create table translation_private.translation_reconciliations (
  idempotency_key text primary key references translation_private.translation_reservations(idempotency_key),
  outcome text not null check (outcome in ('charged', 'confirmed_not_sent')),
  evidence_digest text not null check (evidence_digest ~ '^[0-9a-f]{64}$'),
  actual_characters bigint check (actual_characters >= 0),
  reconciled_at timestamptz not null default clock_timestamp()
);

alter table translation_private.translation_cache enable row level security;
alter table translation_private.translation_cache force row level security;
alter table translation_private.translation_cache_quarantine enable row level security;
alter table translation_private.translation_cache_quarantine force row level security;
alter table translation_private.translation_usage_counters enable row level security;
alter table translation_private.translation_usage_counters force row level security;
alter table translation_private.translation_reservations enable row level security;
alter table translation_private.translation_reservations force row level security;
alter table translation_private.translation_reconciliations enable row level security;
alter table translation_private.translation_reconciliations force row level security;

create or replace function translation_private.reject_cache_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception using errcode = '55000', message = 'translation cache rows are immutable';
end;
$$;

create trigger translation_cache_is_immutable
before update or delete on translation_private.translation_cache
for each row execute function translation_private.reject_cache_mutation();

create or replace function translation_private.require_service_role()
returns void
language plpgsql
stable
set search_path = pg_catalog, auth
as $$
begin
  if coalesce(auth.jwt() ->> 'role', '') <> 'service_role' then
    raise exception using errcode = '42501', message = 'service role required';
  end if;
end;
$$;

create or replace function translation_private.cache_json(row_value translation_private.translation_cache)
returns jsonb
language sql
stable
strict
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'cache_key_digest', row_value.cache_key_digest,
    'story_id', row_value.story_id,
    'input_digest', row_value.input_digest,
    'field_selection', to_jsonb(row_value.field_selection),
    'normalization_version', row_value.normalization_version,
    'source_locale', row_value.source_locale,
    'target_locale', row_value.target_locale,
    'provider', row_value.provider,
    'model_version', row_value.model_version,
    'glossary_policy_version', row_value.glossary_policy_version,
    'candidate_policy_version', row_value.candidate_policy_version,
    'translated_title', row_value.translated_title,
    'translated_description', row_value.translated_description,
    'actual_characters', row_value.actual_characters,
    'created_at', row_value.created_at
  )
$$;

create or replace function translation_private.reservation_json(row_value translation_private.translation_reservations)
returns jsonb
language sql
stable
strict
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'cache_key_digest', row_value.cache_key_digest,
    'story_id', row_value.story_id,
    'input_digest', row_value.input_digest,
    'field_selection', to_jsonb(row_value.field_selection),
    'normalization_version', row_value.normalization_version,
    'source_locale', row_value.source_locale,
    'target_locale', row_value.target_locale,
    'provider', row_value.provider,
    'model_version', row_value.model_version,
    'glossary_policy_version', row_value.glossary_policy_version,
    'candidate_policy_version', row_value.candidate_policy_version,
    'idempotency_key', row_value.idempotency_key,
    'run_id', row_value.run_id,
    'counter_day', row_value.counter_day,
    'counter_month', to_char(row_value.counter_month, 'YYYY-MM'),
    'reserved_characters', row_value.reserved_characters,
    'actual_characters', row_value.actual_characters,
    'run_limit', row_value.run_limit,
    'day_limit', row_value.day_limit,
    'month_limit', row_value.month_limit,
    'state', row_value.state,
    'created_at', row_value.created_at,
    'sent_at', row_value.sent_at,
    'finalized_at', row_value.finalized_at
  )
$$;

create or replace function public.translation_cache_lookup(
  cache_key_digest text,
  story_id text,
  input_digest text,
  field_selection text[],
  normalization_version text,
  source_locale text,
  target_locale text,
  provider text,
  model_version text,
  glossary_policy_version text,
  candidate_policy_version text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare
  cached translation_private.translation_cache%rowtype;
begin
  perform translation_private.require_service_role();
  if cache_key_digest <> translation_private.cache_key_digest(
    story_id, input_digest, field_selection, normalization_version,
    source_locale, target_locale, provider, model_version,
    glossary_policy_version, candidate_policy_version
  ) then
    raise exception using errcode = '22023', message = 'invalid cache key';
  end if;
  if exists (select 1 from translation_private.translation_cache_quarantine q where q.cache_key_digest = translation_cache_lookup.cache_key_digest) then
    return jsonb_build_object('status', 'quarantined');
  end if;
  select c.* into cached
  from translation_private.translation_cache c
  where c.cache_key_digest = translation_cache_lookup.cache_key_digest;
  if not found then
    return jsonb_build_object('status', 'missing');
  end if;
  return jsonb_build_object('status', 'cache_hit', 'cache', translation_private.cache_json(cached));
end;
$$;

create or replace function public.translation_acquire(
  cache_key_digest text,
  story_id text,
  input_digest text,
  field_selection text[],
  normalization_version text,
  source_locale text,
  target_locale text,
  provider text,
  model_version text,
  glossary_policy_version text,
  candidate_policy_version text,
  idempotency_key text,
  run_id text,
  reserved_characters bigint,
  run_limit bigint,
  day_limit bigint,
  month_limit bigint
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
#variable_conflict use_variable
declare
  now_utc timestamptz := clock_timestamp();
  day_utc date := (now_utc at time zone 'UTC')::date;
  month_utc date := date_trunc('month', now_utc at time zone 'UTC')::date;
  expected_digest text;
  fingerprint text;
  cached translation_private.translation_cache%rowtype;
  existing translation_private.translation_reservations%rowtype;
  run_count bigint;
  day_count bigint;
  month_count bigint;
begin
  perform translation_private.require_service_role();
  expected_digest := translation_private.cache_key_digest(
    story_id, input_digest, field_selection, normalization_version,
    source_locale, target_locale, provider, model_version,
    glossary_policy_version, candidate_policy_version
  );
  if cache_key_digest <> expected_digest
     or story_id !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
     or input_digest !~ '^[0-9a-f]{64}$'
     or field_selection not in (array['title'], array['title', 'description'])
     or normalization_version !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
     or source_locale !~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'
     or target_locale !~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$'
     or source_locale = target_locale
     or provider !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
     or model_version !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
     or glossary_policy_version !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
     or candidate_policy_version !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
     or idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
     or run_id !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
     or reserved_characters <= 0
     or least(run_limit, day_limit, month_limit) < 0 then
    raise exception using errcode = '22023', message = 'invalid acquire request';
  end if;
  fingerprint := encode(extensions.digest(convert_to(
    cache_key_digest || '|' || idempotency_key || '|' || run_id || '|' || reserved_characters || '|' ||
    run_limit || '|' || day_limit || '|' || month_limit, 'UTF8'), 'sha256'), 'hex');

  perform pg_advisory_xact_lock(hashtextextended(cache_key_digest, 0));
  if exists (select 1 from translation_private.translation_cache_quarantine q where q.cache_key_digest = translation_acquire.cache_key_digest) then
    return jsonb_build_object('status', 'quarantined');
  end if;
  select c.* into cached from translation_private.translation_cache c
  where c.cache_key_digest = translation_acquire.cache_key_digest;
  if found then
    return jsonb_build_object('status', 'cache_hit', 'cache', translation_private.cache_json(cached));
  end if;

  select r.* into existing from translation_private.translation_reservations r
  where r.idempotency_key = translation_acquire.idempotency_key for update;
  if found then
    if existing.request_fingerprint <> fingerprint then
      raise exception using errcode = '23505', message = 'idempotency conflict';
    end if;
    return jsonb_build_object('status', 'existing', 'reservation', translation_private.reservation_json(existing));
  end if;
  select r.* into existing from translation_private.translation_reservations r
  where r.cache_key_digest = translation_acquire.cache_key_digest
    and r.state in ('leased', 'sent', 'charge_unknown', 'charged_without_cache')
  limit 1 for update;
  if found then
    return jsonb_build_object('status', 'blocked', 'reservation', translation_private.reservation_json(existing));
  end if;

  insert into translation_private.translation_usage_counters(scope_type, scope_key)
  values ('run', run_id), ('day', day_utc::text), ('month', to_char(month_utc, 'YYYY-MM'))
  on conflict do nothing;
  select counted_characters into run_count from translation_private.translation_usage_counters
    where scope_type = 'run' and scope_key = run_id for update;
  select counted_characters into day_count from translation_private.translation_usage_counters
    where scope_type = 'day' and scope_key = day_utc::text for update;
  select counted_characters into month_count from translation_private.translation_usage_counters
    where scope_type = 'month' and scope_key = to_char(month_utc, 'YYYY-MM') for update;
  if run_count + reserved_characters > run_limit
     or day_count + reserved_characters > day_limit
     or month_count + reserved_characters > month_limit then
    return jsonb_build_object('status', 'budget_exhausted');
  end if;

  update translation_private.translation_usage_counters set counted_characters = counted_characters + reserved_characters, updated_at = now_utc
    where scope_type = 'run' and scope_key = run_id;
  update translation_private.translation_usage_counters set counted_characters = counted_characters + reserved_characters, updated_at = now_utc
    where scope_type = 'day' and scope_key = day_utc::text;
  update translation_private.translation_usage_counters set counted_characters = counted_characters + reserved_characters, updated_at = now_utc
    where scope_type = 'month' and scope_key = to_char(month_utc, 'YYYY-MM');

  insert into translation_private.translation_reservations(
    idempotency_key, request_fingerprint, cache_key_digest, story_id, input_digest, field_selection,
    normalization_version, source_locale, target_locale, provider, model_version,
    glossary_policy_version, candidate_policy_version, run_id, counter_day, counter_month,
    reserved_characters, run_limit, day_limit, month_limit, state, created_at
  ) values (
    idempotency_key, fingerprint, cache_key_digest, story_id, input_digest, field_selection,
    normalization_version, source_locale, target_locale, provider, model_version,
    glossary_policy_version, candidate_policy_version, run_id, day_utc, month_utc,
    reserved_characters, run_limit, day_limit, month_limit, 'leased', now_utc
  ) returning * into existing;
  return jsonb_build_object('status', 'leased', 'reservation', translation_private.reservation_json(existing));
end;
$$;

create or replace function public.translation_mark_sent(idempotency_key text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare
  row_value translation_private.translation_reservations%rowtype;
begin
  perform translation_private.require_service_role();
  select r.* into row_value from translation_private.translation_reservations r
    where r.idempotency_key = translation_mark_sent.idempotency_key for update;
  if not found then raise exception using errcode = '22023', message = 'unknown reservation'; end if;
  if row_value.state = 'leased' then
    update translation_private.translation_reservations r set state = 'sent', sent_at = clock_timestamp()
      where r.idempotency_key = translation_mark_sent.idempotency_key returning * into row_value;
  elsif row_value.state not in ('sent', 'settled', 'charge_unknown', 'charged_without_cache') then
    raise exception using errcode = '55000', message = 'invalid reservation transition';
  end if;
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end;
$$;

create or replace function translation_private.release_counted(row_value translation_private.translation_reservations, release_amount bigint)
returns void
language plpgsql
set search_path = pg_catalog, translation_private
as $$
begin
  if release_amount < 0 then raise exception using errcode = '22023', message = 'invalid release'; end if;
  update translation_private.translation_usage_counters set counted_characters = counted_characters - release_amount, updated_at = clock_timestamp()
    where scope_type = 'run' and scope_key = row_value.run_id and counted_characters >= release_amount;
  if not found then raise exception using errcode = '23514', message = 'counter underflow'; end if;
  update translation_private.translation_usage_counters set counted_characters = counted_characters - release_amount, updated_at = clock_timestamp()
    where scope_type = 'day' and scope_key = row_value.counter_day::text and counted_characters >= release_amount;
  if not found then raise exception using errcode = '23514', message = 'counter underflow'; end if;
  update translation_private.translation_usage_counters set counted_characters = counted_characters - release_amount, updated_at = clock_timestamp()
    where scope_type = 'month' and scope_key = to_char(row_value.counter_month, 'YYYY-MM') and counted_characters >= release_amount;
  if not found then raise exception using errcode = '23514', message = 'counter underflow'; end if;
end;
$$;

create or replace function public.translation_recover_stale(
  cache_key_digest text,
  lease_timeout_seconds bigint,
  sent_timeout_seconds bigint
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare
  row_value translation_private.translation_reservations%rowtype;
  now_utc timestamptz := clock_timestamp();
begin
  perform translation_private.require_service_role();
  if cache_key_digest !~ '^[0-9a-f]{64}$'
     or lease_timeout_seconds <= 0
     or sent_timeout_seconds <= 0 then
    raise exception using errcode = '22023', message = 'invalid stale recovery request';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(cache_key_digest, 0));
  select r.* into row_value from translation_private.translation_reservations r
    where r.cache_key_digest = translation_recover_stale.cache_key_digest
      and r.state in ('leased', 'sent')
    limit 1 for update;
  if not found then return jsonb_build_object('status', 'none'); end if;
  if row_value.state = 'leased'
     and row_value.created_at + make_interval(secs => lease_timeout_seconds) <= now_utc then
    perform translation_private.release_counted(row_value, row_value.reserved_characters);
    update translation_private.translation_reservations r
      set state = 'failed_before_send', finalized_at = now_utc
      where r.idempotency_key = row_value.idempotency_key returning * into row_value;
  elsif row_value.state = 'sent'
     and coalesce(row_value.sent_at, row_value.created_at)
       + make_interval(secs => sent_timeout_seconds) <= now_utc then
    update translation_private.translation_reservations r
      set state = 'charge_unknown', finalized_at = now_utc
      where r.idempotency_key = row_value.idempotency_key returning * into row_value;
  end if;
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end;
$$;

create or replace function public.translation_settle(
  idempotency_key text,
  actual_characters bigint,
  translated_title text,
  translated_description text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare
  row_value translation_private.translation_reservations%rowtype;
  cache_value translation_private.translation_cache%rowtype;
  result_digest text;
begin
  perform translation_private.require_service_role();
  select r.* into row_value from translation_private.translation_reservations r
    where r.idempotency_key = translation_settle.idempotency_key for update;
  if not found then raise exception using errcode = '22023', message = 'unknown reservation'; end if;
  if actual_characters < 0 or actual_characters > row_value.reserved_characters
     or char_length(translated_title) not between 1 and 2000
     or char_length(translated_description) > 8000 then
    raise exception using errcode = '22023', message = 'invalid settlement';
  end if;
  result_digest := encode(extensions.digest(convert_to(
    octet_length(translated_title) || ':' || translated_title || '|' ||
    octet_length(translated_description) || ':' || translated_description || '|' || actual_characters,
    'UTF8'), 'sha256'), 'hex');
  if row_value.state = 'settled' then
    if row_value.actual_characters <> translation_settle.actual_characters
       or row_value.settlement_digest <> result_digest then
      raise exception using errcode = '23505', message = 'settlement conflict';
    end if;
    return jsonb_build_object('status', 'settled', 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'sent' then raise exception using errcode = '55000', message = 'invalid reservation transition'; end if;
  insert into translation_private.translation_cache(
    cache_key_digest, story_id, input_digest, field_selection, normalization_version,
    source_locale, target_locale, provider, model_version, glossary_policy_version,
    candidate_policy_version, translated_title, translated_description, actual_characters
  ) values (
    row_value.cache_key_digest, row_value.story_id, row_value.input_digest, row_value.field_selection,
    row_value.normalization_version, row_value.source_locale, row_value.target_locale, row_value.provider,
    row_value.model_version, row_value.glossary_policy_version, row_value.candidate_policy_version,
    translated_title, translated_description, actual_characters
  ) on conflict do nothing;
  select c.* into cache_value from translation_private.translation_cache c where c.cache_key_digest = row_value.cache_key_digest;
  if cache_value.story_id <> row_value.story_id
     or cache_value.input_digest <> row_value.input_digest
     or cache_value.field_selection <> row_value.field_selection
     or cache_value.translated_title <> translation_settle.translated_title
     or cache_value.translated_description <> translation_settle.translated_description
     or cache_value.actual_characters <> translation_settle.actual_characters then
    insert into translation_private.translation_cache_quarantine(cache_key_digest, reason_code)
      values (row_value.cache_key_digest, 'settlement_conflict') on conflict do nothing;
    update translation_private.translation_reservations r set state = 'charge_unknown', finalized_at = clock_timestamp()
      where r.idempotency_key = translation_settle.idempotency_key returning * into row_value;
    return jsonb_build_object('status', 'charge_unknown', 'reservation', translation_private.reservation_json(row_value));
  end if;
  perform translation_private.release_counted(row_value, row_value.reserved_characters - actual_characters);
  update translation_private.translation_reservations r
    set state = 'settled', actual_characters = translation_settle.actual_characters,
        settlement_digest = result_digest, finalized_at = clock_timestamp()
    where r.idempotency_key = translation_settle.idempotency_key returning * into row_value;
  return jsonb_build_object('status', 'settled', 'reservation', translation_private.reservation_json(row_value));
end;
$$;

create or replace function public.translation_mark_failed_before_send(idempotency_key text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare row_value translation_private.translation_reservations%rowtype;
begin
  perform translation_private.require_service_role();
  select r.* into row_value from translation_private.translation_reservations r
    where r.idempotency_key = translation_mark_failed_before_send.idempotency_key for update;
  if not found then raise exception using errcode = '22023', message = 'unknown reservation'; end if;
  if row_value.state = 'failed_before_send' then
    return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'leased' then raise exception using errcode = '55000', message = 'invalid reservation transition'; end if;
  perform translation_private.release_counted(row_value, row_value.reserved_characters);
  update translation_private.translation_reservations r set state = 'failed_before_send', finalized_at = clock_timestamp()
    where r.idempotency_key = translation_mark_failed_before_send.idempotency_key returning * into row_value;
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end;
$$;

create or replace function public.translation_mark_charge_unknown(idempotency_key text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare row_value translation_private.translation_reservations%rowtype;
begin
  perform translation_private.require_service_role();
  select r.* into row_value from translation_private.translation_reservations r
    where r.idempotency_key = translation_mark_charge_unknown.idempotency_key for update;
  if not found then raise exception using errcode = '22023', message = 'unknown reservation'; end if;
  if row_value.state in ('charge_unknown', 'settled', 'charged_without_cache') then
    return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'sent' then raise exception using errcode = '55000', message = 'invalid reservation transition'; end if;
  update translation_private.translation_reservations r set state = 'charge_unknown', finalized_at = clock_timestamp()
    where r.idempotency_key = translation_mark_charge_unknown.idempotency_key returning * into row_value;
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end;
$$;

create or replace function public.translation_quarantine(cache_key_digest text, reason_code text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
begin
  perform translation_private.require_service_role();
  if cache_key_digest !~ '^[0-9a-f]{64}$' or reason_code !~ '^[A-Za-z0-9_-]{1,64}$' then
    raise exception using errcode = '22023', message = 'invalid quarantine request';
  end if;
  insert into translation_private.translation_cache_quarantine(cache_key_digest, reason_code)
    values (cache_key_digest, reason_code) on conflict do nothing;
  return jsonb_build_object('status', 'quarantined');
end;
$$;

create or replace function public.translation_reconcile(
  idempotency_key text,
  outcome text,
  evidence_digest text,
  actual_characters bigint default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, auth, translation_private
as $$
declare
  row_value translation_private.translation_reservations%rowtype;
  previous translation_private.translation_reconciliations%rowtype;
begin
  perform translation_private.require_service_role();
  if outcome not in ('charged', 'confirmed_not_sent') or evidence_digest !~ '^[0-9a-f]{64}$' then
    raise exception using errcode = '22023', message = 'invalid reconciliation';
  end if;
  select r.* into row_value from translation_private.translation_reservations r
    where r.idempotency_key = translation_reconcile.idempotency_key for update;
  if not found then
    raise exception using errcode = '55000', message = 'invalid reconciliation transition';
  end if;
  select x.* into previous from translation_private.translation_reconciliations x
    where x.idempotency_key = translation_reconcile.idempotency_key;
  if found then
    if previous.outcome <> translation_reconcile.outcome
       or previous.evidence_digest <> translation_reconcile.evidence_digest
       or previous.actual_characters is distinct from translation_reconcile.actual_characters then
      raise exception using errcode = '23505', message = 'reconciliation conflict';
    end if;
    return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'charge_unknown' then
    raise exception using errcode = '55000', message = 'invalid reconciliation transition';
  end if;
  if outcome = 'charged' then
    if actual_characters is null or actual_characters < 0 or actual_characters > row_value.reserved_characters then
      raise exception using errcode = '22023', message = 'invalid reconciliation amount';
    end if;
    perform translation_private.release_counted(row_value, row_value.reserved_characters - actual_characters);
    update translation_private.translation_reservations r
      set state = 'charged_without_cache', actual_characters = translation_reconcile.actual_characters, finalized_at = clock_timestamp()
      where r.idempotency_key = translation_reconcile.idempotency_key returning * into row_value;
  else
    if actual_characters is not null and actual_characters <> 0 then
      raise exception using errcode = '22023', message = 'invalid reconciliation amount';
    end if;
    perform translation_private.release_counted(row_value, row_value.reserved_characters);
    update translation_private.translation_reservations r
      set state = 'failed_before_send', finalized_at = clock_timestamp()
      where r.idempotency_key = translation_reconcile.idempotency_key returning * into row_value;
  end if;
  insert into translation_private.translation_reconciliations(idempotency_key, outcome, evidence_digest, actual_characters)
    values (idempotency_key, outcome, evidence_digest, actual_characters);
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end;
$$;

revoke all on all tables in schema translation_private from public, anon, authenticated, service_role;
revoke all on all sequences in schema translation_private from public, anon, authenticated, service_role;
revoke execute on all functions in schema translation_private from public, anon, authenticated, service_role;
revoke execute on function public.translation_cache_lookup(text, text, text, text[], text, text, text, text, text, text, text) from public, anon, authenticated;
revoke execute on function public.translation_acquire(text, text, text, text[], text, text, text, text, text, text, text, text, text, bigint, bigint, bigint, bigint) from public, anon, authenticated;
revoke execute on function public.translation_mark_sent(text) from public, anon, authenticated;
revoke execute on function public.translation_recover_stale(text, bigint, bigint) from public, anon, authenticated;
revoke execute on function public.translation_settle(text, bigint, text, text) from public, anon, authenticated;
revoke execute on function public.translation_mark_failed_before_send(text) from public, anon, authenticated;
revoke execute on function public.translation_mark_charge_unknown(text) from public, anon, authenticated;
revoke execute on function public.translation_quarantine(text, text) from public, anon, authenticated;
revoke execute on function public.translation_reconcile(text, text, text, bigint) from public, anon, authenticated;

grant execute on function public.translation_cache_lookup(text, text, text, text[], text, text, text, text, text, text, text) to service_role;
grant execute on function public.translation_acquire(text, text, text, text[], text, text, text, text, text, text, text, text, text, bigint, bigint, bigint, bigint) to service_role;
grant execute on function public.translation_mark_sent(text) to service_role;
grant execute on function public.translation_recover_stale(text, bigint, bigint) to service_role;
grant execute on function public.translation_settle(text, bigint, text, text) to service_role;
grant execute on function public.translation_mark_failed_before_send(text) to service_role;
grant execute on function public.translation_mark_charge_unknown(text) to service_role;
grant execute on function public.translation_quarantine(text, text) to service_role;
grant execute on function public.translation_reconcile(text, text, text, bigint) to service_role;

comment on schema translation_private is
  'Private translation state. The service_role identity is broad and bypasses RLS; protected environment isolation is mandatory. RPC grants are defense in depth, not least privilege.';

alter default privileges in schema translation_private revoke all on tables from public, anon, authenticated;
alter default privileges in schema translation_private revoke all on sequences from public, anon, authenticated;
alter default privileges in schema translation_private revoke execute on functions from public, anon, authenticated;

commit;
