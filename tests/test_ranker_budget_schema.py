from pathlib import Path


SQL = (Path(__file__).parents[1] / "supabase/migrations/202609140003_m2_ranker_budget.sql").read_text()


def test_budget_rpcs_are_service_only_and_owner_scoped():
    assert "revoke all on function public.m2_reserve_ranker_budget" in SQL
    assert "grant execute on function public.m2_reserve_ranker_budget" in SQL
    assert "request_id=p_request_id and user_id=p_user_id for update" in SQL


def test_atomic_reservation_checks_spent_plus_reserved():
    assert "spent_usd+reserved_usd+p_amount_usd <= p_daily_limit_usd" in SQL
    assert "set reserved_usd=reserved_usd+p_amount_usd" in SQL


def test_frozen_rankings_are_owner_bound_and_expiring():
    assert "user_id uuid not null references auth.users(id)" in SQL
    assert "expires_at timestamptz not null" in SQL
    assert "force row level security" in SQL


def test_owner_card_state_rpc_is_authenticated_and_auth_uid_bound():
    assert "create or replace function public.m2_owner_story_states" in SQL
    assert "caller uuid := auth.uid()" in SQL
    assert "where user_id=caller and story_id=ids.story_id" in SQL
    assert "grant execute on function public.m2_owner_story_states(text[]) to authenticated" in SQL


def test_generation_reset_deletes_derived_frozen_rankings_but_not_budget_audit():
    assert "new.history_generation <> old.history_generation" in SQL
    assert "after insert or update of history_generation" in SQL
    assert "tg_op = 'INSERT' and new.history_generation > 1" in SQL
    assert "delete from public.m2_frozen_rankings where user_id=new.user_id" in SQL
    trigger_section = SQL.split("create or replace function public.m2_clear_frozen_rankings_on_generation", 1)[1]
    assert "m2_ranker_reservations" not in trigger_section


def test_identical_settlement_retry_is_idempotent_but_mismatch_reaches_rejection():
    assert "reservation.status = p_status and reservation.actual_usd is not distinct from p_actual_usd then return" in SQL
    assert "reservation.status <> 'reserved'" in SQL


def test_late_frozen_result_is_guarded_under_same_owner_behavior_lock():
    assert 'before insert or update on public.m2_frozen_rankings' in SQL
    assert "hashtextextended(new.user_id::text || ':behavior',0)" in SQL
    assert "raise exception 'stale frozen ranking bindings'" in SQL
    for field in ('history_generation','server_commit_revision','consent_revision'):
        assert "new.bindings->'"+field+"' is distinct from" in SQL
    assert 'after insert or update of consent_revision on public.user_behavior_settings' in SQL
