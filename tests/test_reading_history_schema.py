from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/202609070001_reading_history.sql"


def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_has_policy_archive_and_private_state_contracts():
    text = sql()
    for fragment in (
        "initial_window_days integer not null default 5",
        "unsaved_retention_days integer not null default 30",
        "page_size integer not null default 20",
        "refresh_poll_seconds integer not null default 300",
        "physical_purge_grace_days integer not null default 7",
        "more_like_topic_weight numeric not null default 0.8",
        "create table public.canonical_stories",
        "canonical_url_hash char(64) generated always as (",
        "create table public.story_aliases",
        "create table public.coverage_mentions",
        "create table public.story_topics",
        "create table public.publication_runs",
        "create table public.publication_entries",
        "create table public.user_story_state",
        "create table public.user_story_interests",
        "create table public.user_action_receipts",
    ):
        assert fragment in text


def test_every_table_is_forced_rls_and_base_tables_are_not_publicly_readable():
    text = sql()
    tables = (
        "feed_policy", "canonical_stories", "story_aliases", "coverage_mentions",
        "story_topics", "publication_runs", "publication_entries", "user_story_state",
        "user_story_interests", "user_action_receipts", "publication_topics",
    )
    for table in tables:
        assert f"alter table public.{table} enable row level security" in text
        assert f"alter table public.{table} force row level security" in text
        assert f"revoke all on table public.{table} from public, anon, authenticated" in text


def test_rpc_surface_is_narrow_and_finalization_is_service_only():
    text = sql()
    for name in (
        "latest_publication", "feed_page", "saved_page", "updates_since",
        "set_story_state", "set_story_interest", "finalize_archive", "prune_publication_history",
    ):
        assert f"create or replace function public.{name}" in text
        assert f"revoke execute on function public.{name}" in text
    assert "grant execute on function public.finalize_archive(jsonb, text) to service_role" in text
    assert "grant execute on function public.finalize_archive(jsonb, text) to anon" not in text
    assert "where user_id = auth.uid()" in text
    assert "on conflict (build_nonce) do nothing" in text


def test_feed_contract_is_keyset_paginated_and_saved_rows_survive_window():
    text = sql()
    assert "p_before_published_at timestamptz" in text
    assert "p_before_story_id text" in text
    assert "p_after_story_id text" in text
    assert "e.published_at < p_before_published_at" in text
    assert "us.saved_at is not null" in text
    assert "fp.unsaved_retention_days" in text
    assert "fp.initial_window_days" in text
    assert "pr.finalized_at is not null" in text
    assert "public.feed_page(text, text, integer, text, timestamptz, text, integer)" in text
