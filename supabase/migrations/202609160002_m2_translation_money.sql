-- Public ingestion translation money is held in the existing private translation
-- reservation ledger. It is deliberately separate from owner ranking budgets.
begin;

alter table translation_private.translation_usage_counters
  add column if not exists counted_microusd bigint not null default 0 check (counted_microusd >= 0);
alter table translation_private.translation_reservations
  add column if not exists charge_scope text check (charge_scope is null or charge_scope ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  add column if not exists reserved_microusd bigint check (reserved_microusd is null or reserved_microusd > 0),
  add column if not exists actual_microusd bigint check (actual_microusd is null or actual_microusd >= 0),
  add column if not exists run_limit_microusd bigint check (run_limit_microusd is null or run_limit_microusd >= 0),
  add column if not exists day_limit_microusd bigint check (day_limit_microusd is null or day_limit_microusd >= 0),
  add column if not exists month_limit_microusd bigint check (month_limit_microusd is null or month_limit_microusd >= 0),
  add constraint translation_money_complete check (
    (charge_scope is null and reserved_microusd is null and actual_microusd is null and run_limit_microusd is null and day_limit_microusd is null and month_limit_microusd is null)
    or (charge_scope is not null and reserved_microusd is not null and run_limit_microusd is not null and day_limit_microusd is not null and month_limit_microusd is not null and (actual_microusd is null or actual_microusd <= reserved_microusd))
  );

create or replace function translation_private.reservation_json(row_value translation_private.translation_reservations)
returns jsonb language sql stable set search_path = pg_catalog as $$
  select jsonb_build_object(
    'cache_key_digest', row_value.cache_key_digest, 'story_id', row_value.story_id,
    'input_digest', row_value.input_digest, 'field_selection', row_value.field_selection,
    'normalization_version', row_value.normalization_version, 'source_locale', row_value.source_locale,
    'target_locale', row_value.target_locale, 'provider', row_value.provider, 'model_version', row_value.model_version,
    'glossary_policy_version', row_value.glossary_policy_version, 'candidate_policy_version', row_value.candidate_policy_version,
    'idempotency_key', row_value.idempotency_key, 'run_id', row_value.run_id, 'counter_day', row_value.counter_day,
    'counter_month', to_char(row_value.counter_month, 'YYYY-MM'), 'reserved_characters', row_value.reserved_characters,
    'actual_characters', row_value.actual_characters, 'run_limit', row_value.run_limit, 'day_limit', row_value.day_limit,
    'month_limit', row_value.month_limit, 'charge_scope', row_value.charge_scope, 'reserved_microusd', row_value.reserved_microusd,
    'actual_microusd', row_value.actual_microusd, 'run_limit_microusd', row_value.run_limit_microusd,
    'day_limit_microusd', row_value.day_limit_microusd, 'month_limit_microusd', row_value.month_limit_microusd,
    'state', row_value.state, 'created_at', row_value.created_at, 'sent_at', row_value.sent_at, 'finalized_at', row_value.finalized_at
  )
$$;

create or replace function public.translation_reserve_money(
  idempotency_key text, charge_scope text, reserved_microusd bigint,
  run_limit_microusd bigint, day_limit_microusd bigint, month_limit_microusd bigint
) returns jsonb language plpgsql security definer set search_path = pg_catalog, auth, translation_private as $$
declare row_value translation_private.translation_reservations%rowtype; now_utc timestamptz := clock_timestamp();
  keys text[]; counts bigint[]; limits bigint[]; index_value int;
begin
  perform translation_private.require_service_role();
  if idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$' or charge_scope !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
    or reserved_microusd <= 0 or least(run_limit_microusd, day_limit_microusd, month_limit_microusd) < 0 then
    raise exception using errcode = '22023', message = 'invalid translation money reservation';
  end if;
  select r.* into row_value from translation_private.translation_reservations r where r.idempotency_key = translation_reserve_money.idempotency_key for update;
  if not found then raise exception using errcode = '22023', message = 'unknown reservation'; end if;
  if row_value.charge_scope is not null then
    if row_value.charge_scope <> charge_scope or row_value.reserved_microusd <> reserved_microusd
      or row_value.run_limit_microusd <> run_limit_microusd or row_value.day_limit_microusd <> day_limit_microusd
      or row_value.month_limit_microusd <> month_limit_microusd then raise exception using errcode = '23505', message = 'money idempotency conflict'; end if;
    return jsonb_build_object('status', case when row_value.state = 'leased' then 'leased' else 'existing' end, 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'leased' then raise exception using errcode = '55000', message = 'invalid money reservation transition'; end if;
  keys := array['money:' || charge_scope || ':run:' || row_value.run_id, 'money:' || charge_scope || ':day:' || row_value.counter_day::text, 'money:' || charge_scope || ':month:' || to_char(row_value.counter_month, 'YYYY-MM')];
  limits := array[run_limit_microusd, day_limit_microusd, month_limit_microusd];
  insert into translation_private.translation_usage_counters(scope_type, scope_key) values ('run', keys[1]), ('day', keys[2]), ('month', keys[3]) on conflict do nothing;
  for index_value in 1..3 loop
    select counted_microusd into counts[index_value] from translation_private.translation_usage_counters where scope_type = case index_value when 1 then 'run' when 2 then 'day' else 'month' end and scope_key = keys[index_value] for update;
    if counts[index_value] + reserved_microusd > limits[index_value] then
      perform translation_private.release_counted(row_value, row_value.reserved_characters);
      update translation_private.translation_reservations set state = 'failed_before_send', finalized_at = now_utc where idempotency_key = row_value.idempotency_key returning * into row_value;
      return jsonb_build_object('status', 'budget_exhausted');
    end if;
  end loop;
  for index_value in 1..3 loop
    update translation_private.translation_usage_counters set counted_microusd = counted_microusd + reserved_microusd, updated_at = now_utc where scope_type = case index_value when 1 then 'run' when 2 then 'day' else 'month' end and scope_key = keys[index_value];
  end loop;
  update translation_private.translation_reservations set charge_scope = translation_reserve_money.charge_scope, reserved_microusd = translation_reserve_money.reserved_microusd,
    run_limit_microusd = translation_reserve_money.run_limit_microusd, day_limit_microusd = translation_reserve_money.day_limit_microusd,
    month_limit_microusd = translation_reserve_money.month_limit_microusd where idempotency_key = row_value.idempotency_key returning * into row_value;
  return jsonb_build_object('status', 'leased', 'reservation', translation_private.reservation_json(row_value));
end $$;

create or replace function public.translation_settle_money(idempotency_key text, actual_microusd bigint)
returns jsonb language plpgsql security definer set search_path = pg_catalog, auth, translation_private as $$
declare row_value translation_private.translation_reservations%rowtype; release_amount bigint; keys text[]; index_value int;
begin
  perform translation_private.require_service_role();
  select r.* into row_value from translation_private.translation_reservations r where r.idempotency_key = translation_settle_money.idempotency_key for update;
  if not found or row_value.charge_scope is null or actual_microusd < 0 or actual_microusd > row_value.reserved_microusd then raise exception using errcode = '22023', message = 'invalid money settlement'; end if;
  if row_value.actual_microusd is not null then
    if row_value.actual_microusd <> actual_microusd then raise exception using errcode = '23505', message = 'money settlement conflict'; end if;
    return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
  end if;
  if row_value.state <> 'sent' then raise exception using errcode = '55000', message = 'invalid money settlement transition'; end if;
  release_amount := row_value.reserved_microusd - actual_microusd;
  keys := array['money:' || row_value.charge_scope || ':run:' || row_value.run_id, 'money:' || row_value.charge_scope || ':day:' || row_value.counter_day::text, 'money:' || row_value.charge_scope || ':month:' || to_char(row_value.counter_month, 'YYYY-MM')];
  for index_value in 1..3 loop
    update translation_private.translation_usage_counters set counted_microusd = counted_microusd - release_amount, updated_at = clock_timestamp() where scope_type = case index_value when 1 then 'run' when 2 then 'day' else 'month' end and scope_key = keys[index_value] and counted_microusd >= release_amount;
    if not found then raise exception using errcode = '23514', message = 'money counter underflow'; end if;
  end loop;
  update translation_private.translation_reservations set actual_microusd = translation_settle_money.actual_microusd where idempotency_key = row_value.idempotency_key returning * into row_value;
  return jsonb_build_object('status', row_value.state, 'reservation', translation_private.reservation_json(row_value));
end $$;

revoke execute on function public.translation_reserve_money(text, text, bigint, bigint, bigint, bigint) from public, anon, authenticated;
revoke execute on function public.translation_settle_money(text, bigint) from public, anon, authenticated;
grant execute on function public.translation_reserve_money(text, text, bigint, bigint, bigint, bigint) to service_role;
grant execute on function public.translation_settle_money(text, bigint) to service_role;
commit;
