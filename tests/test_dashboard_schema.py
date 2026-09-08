from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/202609080001_dashboard_summary.sql"


def test_dashboard_summary_is_owner_only_bounded_and_exact() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert sql.startswith("begin;") and sql.rstrip().endswith("commit;")
    assert "dashboard_topic_limit integer not null default 20" in sql
    assert "check (dashboard_topic_limit between 1 and 100)" in sql
    assert "create or replace function public.dashboard_summary() returns jsonb" in sql
    assert "stable security definer" in sql
    assert "set search_path = pg_catalog, public" in sql
    assert "caller_id uuid := auth.uid()" in sql
    assert "authentication required" in sql
    assert "order by signal_count desc, topic_id" in sql
    assert "limit topic_limit" in sql
    assert "'scope', 'current_retained_state'" in sql
    assert "'snapshot_at', statement_timestamp()" in sql
    assert "9007199254740991" in sql
    assert "dashboard counts exceed safe response range" in sql
    for key in (
        "saved_count",
        "saved_unread_count",
        "read_count",
        "active_interest_signal_count",
        "topic_signals",
    ):
        assert f"'{key}'" in sql
    assert "revoke execute on function public.dashboard_summary() from public, anon, authenticated" in sql
    assert "grant execute on function public.dashboard_summary() to authenticated" in sql
    assert "grant execute on function public.dashboard_summary() to anon" not in sql
    assert "grant select" not in sql
