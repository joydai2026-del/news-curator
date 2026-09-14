from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.behavior_history import BehaviorEventCommand
from curator.contracts import M2HistoryEventType

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "supabase/migrations/202609140001_m2_behavior_history.sql").read_text()
STORY = "story:" + "a" * 64
EVENT = "event:" + "b" * 64


def test_behavior_event_command_leaves_actor_identity_to_verified_jwt() -> None:
    command = BehaviorEventCommand(
        EVENT, M2HistoryEventType.OPEN_ORIGINAL,
        {"story_id": STORY, "surface": "web"},
        datetime(2026, 9, 9, 20, 6, 28, tzinfo=timezone.utc),
        1,
    )
    assert "p_actor_kind" not in command.rpc_arguments()
    assert "auth.jwt()->'app_metadata'->>'actor_kind'" in SQL


def test_command_rejects_bool_schema_naive_time_and_bad_story_identity() -> None:
    good = dict(event_id=EVENT, event_type=M2HistoryEventType.OPEN_ORIGINAL,
        payload={"story_id": STORY, "surface": "web"},
        occurred_at=datetime(2026, 9, 9, tzinfo=timezone.utc),expected_history_generation=1)
    for changes in ({"schema_version": True}, {"occurred_at": datetime(2026, 9, 9)},
                    {"payload": {"story_id": "not-canonical", "surface": "web"}}):
        with pytest.raises(ValueError):
            BehaviorEventCommand(**{**good, **changes}).rpc_arguments()


def test_migration_uses_auth_uid_owner_rls_and_cascade_erasure() -> None:
    assert SQL.count("user_id uuid") == 4
    assert SQL.count("references auth.users(id) on delete cascade") == 4
    assert SQL.count("user_id = auth.uid()") >= 8
    assert "to authenticated" in SQL
    assert "to anon" not in "\n".join(line for line in SQL.splitlines() if line.startswith("grant execute"))


def test_retries_conflict_but_distinct_events_get_owner_revisions() -> None:
    assert "primary key (user_id, event_id)" in SQL
    assert "unique (user_id, event_revision)" in SQL
    assert "raise exception 'event replay mismatch'" in SQL
    assert "raise exception 'history generation conflict'" in SQL
    assert "latest_revision = public.user_behavior_revisions.latest_revision + 1" in SQL


def test_snapshot_is_owner_bound_bounded_and_returns_chronological_order() -> None:
    assert "function public.m2_history_snapshot" in SQL
    assert "least(coalesce(p_limit, behavior_history_limit), behavior_history_limit)" in SQL
    assert "where e.user_id = auth.uid() order by e.event_revision desc" in SQL
    assert "order by event_revision) from selected" in SQL
    assert "'newest_event_id'" in SQL
    assert "'included_history_revision'" in SQL
    assert "'server_commit_revision'" in SQL and "'learning_enabled'" in SQL
    assert "'history_generation'" in SQL and "'consent_revision'" in SQL
    assert "'provider_processing_enabled'" in SQL and "'provider_policy_id'" in SQL
    assert "left join public.canonical_stories" in SQL
    assert "left join lateral" in SQL
    assert "'story_title',story_title" in SQL
    assert "'story_summary',story_summary" in SQL
    assert "'source_id',source_id" in SQL


def test_state_and_interest_wrappers_append_in_the_same_transaction() -> None:
    state = SQL[SQL.index("create or replace function public.set_story_state_with_event"):]
    interest = SQL[SQL.index("create or replace function public.set_story_interest_with_event"):]
    assert state.index("public.set_story_state(") < state.index("public.append_behavior_event(")
    assert interest.index("public.set_story_interest(") < interest.index("public.append_behavior_event(")


def test_consent_and_clear_cover_events_revision_and_derived_profile() -> None:
    assert "learning consent required" in SQL
    clear = SQL[SQL.index("create or replace function public.clear_behavior_history"):]
    for table in ("user_behavior_events", "user_behavior_profile_state"):
        assert f"delete from public.{table} where user_id=caller" in clear
    assert "history_generation=public.user_behavior_revisions.history_generation+1" in clear
    assert "delete from public.user_story_interests where user_id=caller" in clear
    assert "operation='set_story_interest'" in clear


def test_local_learning_and_external_provider_consent_are_separate() -> None:
    assert "learning_enabled boolean" in SQL
    assert "provider_processing_enabled boolean" in SQL
    assert "p_provider_processing_enabled and (p_provider_policy_id is null" in SQL
    assert "not p_provider_processing_enabled and p_provider_policy_id is not null" in SQL
    assert SQL.count("'status','learning_disabled'") == 2


def test_combined_receipts_bind_full_action_and_check_generation_before_state_write() -> None:
    assert 'add constraint complete_behavior_receipt' in SQL
    for name,write in [('set_story_state_with_event','set_story_state'),('set_story_interest_with_event','set_story_interest')]:
        wrapper=SQL.split('create or replace function public.'+name,1)[1].split('$$;',1)[0]
        assert wrapper.index('history generation conflict') < wrapper.index('state_result := public.'+write)
        assert wrapper.index('combined action replay mismatch') < wrapper.index('state_result := public.'+write)
        for field in ('event_id','occurred_at','history_generation','actor_kind','surface'):
            assert "'"+field+"'" in wrapper
        assert 'return existing.behavior_response' in wrapper


def test_revocation_preserves_raw_events_and_state_but_erases_derived_values() -> None:
    consent=SQL.split('create or replace function public.set_behavior_consent',1)[1].split('$$;',1)[0]
    assert 'delete from public.user_behavior_profile_state' in consent
    assert 'delete from public.user_story_interests' in consent
    assert 'delete from public.user_behavior_events' not in consent
    assert 'delete from public.user_story_state' not in consent
    assert 'history_generation=public.user_behavior_revisions.history_generation+1' in consent
    assert 'update public.user_action_receipts set response=' in consent
