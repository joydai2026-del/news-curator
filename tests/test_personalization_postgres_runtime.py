from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_reading_history_postgres_runtime import _wait_for_postgres


ROOT = Path(__file__).resolve().parents[1]
BASE_MIGRATION = ROOT / "supabase/migrations/202608290001_user_preferences.sql"
GRANTS_MIGRATION = ROOT / "supabase/migrations/202609040002_user_preferences_validator_grants.sql"
POSTGRES_IMAGE = "postgres:17.11"


def _run(
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=60,
    )


def _psql(container: str, sql: str) -> str:
    return _run(
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-At",
        input_text=sql,
    ).stdout


@pytest.mark.parametrize(
    "moved_validators",
    [(), ("valid_interests(text[])",), ("valid_saved_searches(jsonb)",),
     ("valid_interests(text[])", "valid_saved_searches(jsonb)")],
    ids=["fresh", "interests_private", "searches_private", "already_private"],
)
def test_validator_grants_migration_accepts_supported_starting_states(
    moved_validators: tuple[str, ...],
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable.")
    if _run("docker", "image", "inspect", POSTGRES_IMAGE, check=False).returncode:
        pytest.skip(f"Local {POSTGRES_IMAGE} image is unavailable.")

    container = f"news-curator-personalization-{uuid.uuid4().hex[:10]}"
    _run(
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        container,
        "-e",
        "POSTGRES_PASSWORD=review-only",
        POSTGRES_IMAGE,
    )
    try:
        _wait_for_postgres(container)
        _psql(
            container,
            """
            create role anon nologin;
            create role authenticated nologin;
            create role service_role nologin bypassrls;
            create schema auth;
            create table auth.users(id uuid primary key);
            create function auth.uid() returns uuid language sql stable
              as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
            grant usage on schema auth to authenticated, service_role;
            grant execute on function auth.uid() to authenticated, service_role;
            """,
        )
        _psql(container, BASE_MIGRATION.read_text(encoding="utf-8"))
        original_dependencies = _psql(container, """
            select d.refobjid from pg_depend d join pg_constraint c on c.oid = d.objid
            where d.classid = 'pg_constraint'::regclass
              and d.refclassid = 'pg_proc'::regclass
              and c.conrelid = 'public.user_preferences'::regclass
            order by d.refobjid;
        """)
        # Reproduce live drift independently of the migration being tested.
        _psql(container, "create schema personalization_private;")
        for signature in moved_validators:
            _psql(container, f"alter function public.{signature} set schema personalization_private;")

        _psql(container, GRANTS_MIGRATION.read_text(encoding="utf-8"))
        _psql(container, GRANTS_MIGRATION.read_text(encoding="utf-8"))

        assert _psql(container, """
            select d.refobjid from pg_depend d join pg_constraint c on c.oid = d.objid
            where d.classid = 'pg_constraint'::regclass
              and d.refclassid = 'pg_proc'::regclass
              and c.conrelid = 'public.user_preferences'::regclass
            order by d.refobjid;
        """) == original_dependencies

        result = _psql(
            container,
            """
            select count(*) from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public'
              and p.proname in ('valid_interests', 'valid_saved_searches');
            select count(*) from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'personalization_private'
              and p.proname in ('valid_interests', 'valid_saved_searches');
            select count(*) from information_schema.routine_privileges
            where specific_schema = 'personalization_private'
              and routine_name in ('valid_interests', 'valid_saved_searches')
              and grantee = 'authenticated'
              and privilege_type = 'EXECUTE';
            """,
        ).splitlines()
        assert result == ["0", "2", "2"]
        assert _psql(container, """
            select has_schema_privilege('anon', 'personalization_private', 'USAGE');
            select has_function_privilege('anon',
              'personalization_private.valid_interests(text[])', 'EXECUTE');
            select has_function_privilege('anon',
              'personalization_private.valid_saved_searches(jsonb)', 'EXECUTE');
            select has_function_privilege('anon',
              'public.compare_and_swap_user_preferences(bigint,text,text[],jsonb)', 'EXECUTE');
        """).splitlines() == ["f", "f", "f", "f"]
        user_id = str(uuid.uuid4())
        _psql(container, f"""
            insert into auth.users values ('{user_id}');
            set role authenticated;
            select set_config('request.jwt.claim.sub', '{user_id}', false);
            insert into public.user_preferences(user_id) values ('{user_id}');
        """)
        assert _psql(container, f"""
            set role authenticated;
            set request.jwt.claim.sub = '{user_id}';
            select public.compare_and_swap_user_preferences(0, 'zh', '{{}}', '[]')->>'status';
            select public.compare_and_swap_user_preferences(0, 'en', '{{}}', '[]')->>'status';
            select personalization_private.valid_interests(array['']);
            select personalization_private.valid_saved_searches('{{}}');
        """).splitlines()[-4:] == ["updated", "conflict", "f", "f"]
    finally:
        _run("docker", "stop", container, check=False)
