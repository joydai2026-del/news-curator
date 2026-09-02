from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/202608290001_user_preferences.sql"


def migration_text() -> str:
    return MIGRATION.read_text()


def test_schema_has_bounded_private_owner_contract() -> None:
    sql = migration_text()
    required = (
        "user_id uuid primary key references auth.users(id) on delete cascade",
        "revision bigint not null default 0 check (revision >= 0)",
        "locale text not null default 'en' check (locale in ('en', 'zh'))",
        "cardinality(value) <= 20",
        "jsonb_array_length(value) > 20",
        "octet_length(value::text) > 8192",
        "item ?& array['id', 'query', 'enabled']",
        "item_id = any(seen_ids)",
        "item ~ '^[[:space:]]|[[:space:]]$'",
        "item_id ~ '^[[:space:]]|[[:space:]]$'",
        "item_query ~ '^[[:space:]]|[[:space:]]$'",
        "alter table public.user_preferences enable row level security",
        "alter table public.user_preferences force row level security",
    )
    for fragment in required:
        assert fragment in sql


def test_rls_predicates_cover_each_operation_exactly() -> None:
    sql = migration_text()
    owner = "auth.uid() is not null and auth.uid() = user_id"
    assert sql.count(owner) == 5
    assert "for select\nto authenticated\nusing (" + owner + ")" in sql
    assert "for insert\nto authenticated\nwith check (" + owner + ")" in sql
    assert "for update\nto authenticated\nusing (" + owner + ")\nwith check (" + owner + ")" in sql
    assert "for delete\nto authenticated\nusing (" + owner + ")" in sql


def test_grants_forbid_direct_revision_or_update_control() -> None:
    sql = migration_text().lower()
    assert "revoke all on table public.user_preferences from public, anon, authenticated" in sql
    assert "grant select, delete on table public.user_preferences to authenticated" in sql
    assert "grant insert (user_id, locale, interests, saved_searches)" in sql
    assert "grant update" not in sql
    assert "grant insert on table public.user_preferences" not in sql
    assert "grant execute on function public.compare_and_swap_user_preferences" in sql
    assert "revoke execute on function public.compare_and_swap_user_preferences" in sql


def test_migration_changes_privileges_only_on_exact_news_curator_objects() -> None:
    sql = migration_text().lower()
    assert "revoke all on all " not in sql
    assert "alter default privileges" not in sql
    assert "revoke all on table public.user_preferences" in sql
    for signature in (
        "public.valid_interests(text[])",
        "public.valid_saved_searches(jsonb)",
        "public.set_user_preferences_updated_at()",
        "public.compare_and_swap_user_preferences(bigint, text, text[], jsonb)",
    ):
        assert f"revoke execute on function {signature}" in sql


def test_cas_is_owner_checked_atomic_and_server_increments_revision() -> None:
    sql = migration_text()
    assert "security definer\nset search_path = pg_catalog, public" in sql
    assert "caller_id uuid := auth.uid()" in sql
    assert "where user_id = caller_id and revision = expected_revision" in sql
    assert "revision = revision + 1" in sql
    assert "'status', 'updated'" in sql
    assert "'status', 'conflict'" in sql
    assert "'status', 'not_found'" in sql
    assert "expected_revision bigint" in sql
    assert "new_revision" not in sql


def test_all_functions_pin_search_path_and_limit_execute() -> None:
    sql = migration_text()
    assert sql.count("set search_path =") == 4
    for signature in (
        "public.valid_interests(text[])",
        "public.valid_saved_searches(jsonb)",
        "public.set_user_preferences_updated_at()",
        "public.compare_and_swap_user_preferences(bigint, text, text[], jsonb)",
    ):
        assert f"revoke execute on function {signature}" in sql
    assert sql.count("security definer") == 1


def test_local_config_has_only_local_redirects_and_rotation() -> None:
    config = (ROOT / "supabase/config.toml").read_text()
    assert 'site_url = "http://127.0.0.1:8000"' in config
    assert '"http://127.0.0.1:8000/auth/callback/"' in config
    assert '"http://127.0.0.1:*/callback"' in config
    assert "enable_refresh_token_rotation = true" in config
    assert "service_role" not in config.lower()
