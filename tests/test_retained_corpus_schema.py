from pathlib import Path


SQL=(Path(__file__).parents[1]/'supabase/migrations/202609140002_m2_retained_corpus.sql').read_text()


def test_categories_are_latest_per_source_then_rebuilt_as_a_union():
    assert 'primary key (story_id, source_id)' in SQL
    assert 'excluded.source_observed_at > public.retained_corpus_source_categories.source_observed_at' in SQL
    assert 'delete from public.retained_corpus_categories c where c.story_id = category_story' in SQL
    assert 'cross join lateral unnest(s.category_ids) category_id' in SQL
    assert 'where s.story_id = category_story' in SQL
    assert 'if changed or category_story is not null then inserted_count := inserted_count + 1' in SQL


def test_source_category_state_is_private_and_input_categories_are_strict_strings():
    assert 'alter table public.retained_corpus_source_categories force row level security' in SQL
    assert 'revoke all on public.retained_corpus_observations, public.retained_corpus_categories, public.retained_corpus_source_categories from public, anon, authenticated' in SQL
    assert "jsonb_typeof(category.value) <> 'string'" in SQL
    assert "category.value #>> '{}' !~ '^[a-z0-9][a-z0-9-]{0,79}$'" in SQL


def test_postgres_regression_is_transactional_and_covers_reclassification_and_partial_source():
    verifier=(Path(__file__).parents[1]/'scripts/verify_retained_corpus_postgres.py').read_text()
    assert '--category-regression-only' in verifier
    assert 'begin;' in verifier and 'rollback;' in verifier
    assert verifier.count("request.jwt.claims = '{\\\"role\\\":\\\"service_role\\\"}'") == 2
    assert 'request.jwt.claim.role' not in verifier
    for check in ('old_source_category_removed','stale_replay_ignored',
                  'independent_source_category_preserved','publisher_metadata_preserved',
                  'source_local_reclassification_accepted_below_global_watermark'):
        assert check in verifier
