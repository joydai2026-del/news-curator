"""Migration contract for the M2.1 Phase 1 translation and grouping columns."""
from pathlib import Path


SQL = (Path(__file__).parents[1] / "supabase/migrations/202609160001_m2_translation_columns.sql").read_text()


def test_translation_columns_and_group_column_are_added():
    assert "add column title_translations jsonb not null default '{}'::jsonb" in SQL
    assert "add column summary_translations jsonb not null default '{}'::jsonb" in SQL
    assert "add column event_group_id text" in SQL


def test_translation_overlays_are_shape_checked_to_the_supported_languages():
    assert "retained_corpus_title_translations_shape" in SQL
    assert "retained_corpus_summary_translations_shape" in SQL
    assert "entry.key not in ('en', 'zh')" in SQL
    assert "event_group_id is null or event_group_id ~ '^group:[0-9a-f]{32}$'" in SQL


def test_a_later_observation_merges_translations_instead_of_erasing_them():
    assert "title_translations = public.retained_corpus_observations.title_translations || excluded.title_translations" in SQL
    assert "summary_translations = public.retained_corpus_observations.summary_translations || excluded.summary_translations" in SQL
    assert "event_group_id = coalesce(excluded.event_group_id, public.retained_corpus_observations.event_group_id)" in SQL


def test_the_read_rpc_returns_the_overlay_and_keeps_its_signature():
    assert "'title_translations', o.title_translations, 'summary_translations', o.summary_translations" in SQL
    assert "'event_group_id', o.event_group_id" in SQL
    # Same five-argument signature, so the rollback ACL drill stays valid.
    assert "create or replace function public.m2_retained_candidates(\n  p_category_id text default null, p_query text default null," in SQL


def test_the_language_exclusive_rpc_is_service_only_and_group_aware():
    assert "create or replace function public.m2_retained_candidates_language_exclusive" in SQL
    assert "p_display_language not in ('en', 'zh')" in SQL
    assert "where o.language <> p_display_language" in SQL
    assert "peer.language = p_display_language" in SQL
    assert "revoke all on function public.m2_retained_candidates_language_exclusive" in SQL
    assert "grant execute on function public.m2_retained_candidates_language_exclusive(text, text, timestamptz, text, integer) to service_role" in SQL
