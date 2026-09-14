from pathlib import Path

SQL=(Path(__file__).parents[1]/'supabase/migrations/202609140004_m2_owner_export.sql').read_text()


def test_export_is_authenticated_owner_only_with_private_uncached_response():
    assert 'caller uuid:=auth.uid()' in SQL
    assert "raise exception 'authentication required'" in SQL
    assert "cursor_value->>'owner_id' is distinct from caller::text" in SQL
    assert 'revoke all on function public.m2_owner_export_rows() from public,anon,authenticated' in SQL
    assert 'grant execute on function public.m2_owner_export_page(text,text) to authenticated' in SQL
    assert 'private, no-store' in SQL


def test_export_covers_requested_tables_without_history_window_or_model_output():
    projection=SQL.split('create function public.m2_owner_export_rows()',1)[1].split('$$;',1)[0]
    for name in ('user_behavior_events','user_behavior_settings','user_behavior_revisions',
                 'user_behavior_profile_state','user_preferences','user_story_state','user_story_interests'):
        assert 'public.'+name in projection
    assert projection.count('where user_id=auth.uid()')==7
    for excluded in ('m2_frozen_rankings','user_action_receipts','m2_ranker_reservations','behavior_history_limit'):
        assert excluded not in SQL


def test_export_fence_and_rows_come_from_same_statement_snapshot():
    assert 'with rows as materialized' in SQL
    assert 'order by section,row_key' in SQL
    assert "required_fence<>fence then raise exception 'export changed; restart download'" in SQL
    assert 'where bytes<=page_bytes' in SQL
    assert 'owner_export_download_bytes' in SQL
