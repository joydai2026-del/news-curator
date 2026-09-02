"""Executable local PostgreSQL proof for the paid translation state machine.

The harness never connects to a non-loopback database. Run ``supabase db
reset`` first so both migrations are applied. When the local development stack
is not available, collection skips with one precise reason instead of turning a
missing Docker daemon into a false product failure.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import uuid
from types import MappingProxyType
from urllib.parse import urlsplit

import pytest

from curator.sources import SafeHttpResponse
from curator.translation import (
    AcquireRequest,
    AcquireStatus,
    BudgetLimits,
    ReservationState,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
    TranslationCacheKey,
    TranslationCacheRecord,
)


DEFAULT_LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def _dsn() -> str:
    value = os.environ.get("NEWS_CURATOR_TEST_DATABASE_URL", DEFAULT_LOCAL_DSN)
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        pytest.skip("translation local-db harness refuses every non-loopback database")
    return value


def _raw_psql(sql: str, *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("psql")
    if executable is None:
        pytest.skip("psql is unavailable; install PostgreSQL client tools for the local-db harness")
    env = dict(os.environ)
    env["PGCONNECT_TIMEOUT"] = "2"
    return subprocess.run(
        [executable, _dsn(), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _psql(sql: str) -> str:
    result = _raw_psql(sql)
    if result.returncode != 0:
        pytest.fail("local PostgreSQL command failed without exposing database output")
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    return rows[-1] if rows else ""


@pytest.fixture(scope="module", autouse=True)
def _require_local_translation_schema() -> None:
    try:
        connected = _raw_psql("select 1", timeout=5)
    except subprocess.TimeoutExpired:
        pytest.skip("local Supabase PostgreSQL did not answer on 127.0.0.1:54322")
    if connected.returncode != 0:
        pytest.skip("local Supabase PostgreSQL is unavailable; run supabase start and supabase db reset")
    present = _raw_psql(
        "select to_regclass('translation_private.translation_reservations') is not null"
    )
    if present.returncode != 0 or "t" not in present.stdout.split():
        pytest.skip("translation migration is not applied locally; run supabase db reset")


@pytest.fixture(autouse=True)
def _reset_translation_state() -> None:
    _psql(
        "truncate translation_private.translation_reconciliations, "
        "translation_private.translation_reservations, "
        "translation_private.translation_cache_quarantine, "
        "translation_private.translation_cache, "
        "translation_private.translation_usage_counters; select 'reset';"
    )


def _key(suffix: str) -> TranslationCacheKey:
    return TranslationCacheKey(
        story_id=f"story:localdb-{suffix}",
        input_digest=hashlib.sha256(f"input:{suffix}".encode()).hexdigest(),
        field_selection=("title",),
        normalization_version="v1",
        source_locale="en",
        target_locale="zh",
        provider="google",
        model_version="projects/valid-project-123/locations/global/models/general/nmt",
        glossary_policy_version="none-v1",
        candidate_policy_version="bounded-v1",
    )


def _claims() -> str:
    return "select set_config('request.jwt.claims', '{\"role\":\"service_role\"}', false);"


def _acquire_sql(
    key: TranslationCacheKey,
    idem: str,
    run: str,
    *,
    reserved: int = 100,
    run_limit: int = 2000,
    day_limit: int = 15000,
    month_limit: int = 450000,
) -> str:
    fields = "array['title']::text[]"
    return _claims() + " select public.translation_acquire(" + ",".join(
        (
            f"'{key.digest}'",
            f"'{key.story_id}'",
            f"'{key.input_digest}'",
            fields,
            "'v1'",
            "'en'",
            "'zh'",
            "'google'",
            f"'{key.model_version}'",
            "'none-v1'",
            "'bounded-v1'",
            f"'{idem}'",
            f"'{run}'",
            str(reserved),
            str(run_limit),
            str(day_limit),
            str(month_limit),
        )
    ) + ");"


def _rpc(name: str, args: str) -> dict[str, object]:
    return json.loads(_psql(_claims() + f" select public.{name}({args});"))


def _parallel_psql(sql_commands: list[str], *, timeout: int = 15) -> list[dict[str, object]]:
    executable = shutil.which("psql")
    assert executable is not None
    env = {**os.environ, "PGCONNECT_TIMEOUT": "2"}
    processes = [
        subprocess.Popen(
            [executable, _dsn(), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for sql in sql_commands
    ]
    payloads: list[dict[str, object]] = []
    for process in processes:
        stdout, _stderr = process.communicate(timeout=timeout)
        assert process.returncode == 0, "concurrent local PostgreSQL RPC failed"
        rows = [line for line in stdout.splitlines() if line.strip()]
        payloads.append(json.loads(rows[-1]))
    return payloads


def _counters() -> dict[str, int]:
    raw = _psql(
        "select coalesce(jsonb_object_agg(scope_type, counted_characters), '{}'::jsonb) "
        "from translation_private.translation_usage_counters;"
    )
    return {str(key): int(value) for key, value in json.loads(raw).items()}


class _LoopbackRestTransport:
    """Minimal test transport that can reach only the local Supabase REST port."""

    def request(self, source_id: str, method: str, url: str, **kwargs: object) -> SafeHttpResponse:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("local RPC harness attempted a non-loopback request")
        headers = dict(kwargs.get("headers") or {})
        for credential in kwargs.get("credentials") or ():
            if credential.origin != f"{parsed.scheme}://{parsed.netloc}":
                raise AssertionError("local RPC credential origin mismatch")
            headers[credential.header_name] = credential.value
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request(
            method,
            parsed.path,
            body=kwargs.get("body"),
            headers=headers,
        )
        response = connection.getresponse()
        body = response.read(256 * 1024 + 1)
        response_headers = MappingProxyType({key.lower(): value for key, value in response.getheaders()})
        connection.close()
        return SafeHttpResponse(response.status, url, response_headers, body)


def _local_rest_identity() -> tuple[str, str]:
    origin = os.environ.get("NEWS_CURATOR_TEST_SUPABASE_URL", "http://127.0.0.1:54321")
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("translation local-RPC harness refuses every non-loopback HTTP origin")
    key = os.environ.get("NEWS_CURATOR_TEST_SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        pytest.skip(
            "local Supabase service-role test identity is unavailable; set "
            "NEWS_CURATOR_TEST_SUPABASE_SERVICE_ROLE_KEY"
        )
    try:
        probe = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        probe.connect()
        probe.close()
    except OSError:
        pytest.skip("local Supabase REST is unavailable on the configured loopback origin")
    return origin, key


def test_atomic_competing_acquire_has_one_lease_and_one_counter_increment() -> None:
    suffix = uuid.uuid4().hex[:12]
    key = _key(suffix)
    run = f"run:compete-{suffix}"
    payloads = _parallel_psql(
        [_acquire_sql(key, f"idem:compete-{suffix}-{index}", run) for index in range(10)]
    )

    assert [payload["status"] for payload in payloads].count("leased") == 1
    assert [payload["status"] for payload in payloads].count("blocked") == 9
    assert _counters() == {"run": 100, "day": 100, "month": 100}


def test_duplicate_idempotency_budget_counters_and_utc_keys_are_stable() -> None:
    suffix = uuid.uuid4().hex[:12]
    first_key = _key("first-" + suffix)
    idem = f"idem:duplicate-{suffix}"
    run = f"run:duplicate-{suffix}"
    first = json.loads(_psql(_acquire_sql(first_key, idem, run, run_limit=200, day_limit=200, month_limit=200)))
    duplicate = json.loads(_psql(_acquire_sql(first_key, idem, run, run_limit=200, day_limit=200, month_limit=200)))
    exhausted = json.loads(
        _psql(
            _acquire_sql(
                _key("second-" + suffix),
                f"idem:second-{suffix}",
                run,
                reserved=101,
                run_limit=200,
                day_limit=200,
                month_limit=200,
            )
        )
    )

    assert first["status"] == "leased"
    assert duplicate["status"] == "existing"
    assert exhausted["status"] == "budget_exhausted"
    assert _counters() == {"run": 100, "day": 100, "month": 100}
    utc_keys = json.loads(
        _psql(
            "select jsonb_build_object("
            "'stored_day', counter_day::text, "
            "'expected_day', (clock_timestamp() at time zone 'UTC')::date::text, "
            "'stored_month', counter_month::text, "
            "'expected_month', date_trunc('month', clock_timestamp() at time zone 'UTC')::date::text) "
            "from translation_private.translation_reservations limit 1;"
        )
    )
    assert utc_keys["stored_day"] == utc_keys["expected_day"]
    assert utc_keys["stored_month"] == utc_keys["expected_month"]


def test_distinct_concurrent_acquires_finish_without_deadlock_pressure() -> None:
    suffix = uuid.uuid4().hex[:12]
    commands = [
        _acquire_sql(
            _key(f"deadlock-{suffix}-{index}"),
            f"idem:deadlock-{suffix}-{index}",
            f"run:deadlock-{suffix}-{index}",
        )
        for index in range(12)
    ]
    payloads = _parallel_psql(commands)
    assert {payload["status"] for payload in payloads} == {"leased"}
    assert _counters()["day"] == 1200
    assert _counters()["month"] == 1200


def test_settle_retry_is_idempotent_and_cache_lookup_matches() -> None:
    suffix = uuid.uuid4().hex[:12]
    key = _key(suffix)
    idem = f"idem:settle-{suffix}"
    run = f"run:settle-{suffix}"
    assert json.loads(_psql(_acquire_sql(key, idem, run)))["status"] == "leased"
    assert _rpc("translation_mark_sent", f"'{idem}'")["status"] == "sent"
    args = f"'{idem}',70,'translated title','translated description'"
    first = _rpc("translation_settle", args)
    second = _rpc("translation_settle", args)
    lookup = _rpc(
        "translation_cache_lookup",
        ",".join(
            (
                f"'{key.digest}'",
                f"'{key.story_id}'",
                f"'{key.input_digest}'",
                "array['title']::text[]",
                "'v1'",
                "'en'",
                "'zh'",
                "'google'",
                f"'{key.model_version}'",
                "'none-v1'",
                "'bounded-v1'",
            )
        ),
    )
    assert first == second
    assert first["status"] == "settled"
    assert lookup["status"] == "cache_hit"
    assert lookup["cache"]["model_version"] == key.model_version
    assert _counters() == {"run": 70, "day": 70, "month": 70}


def test_quarantine_and_pre_send_post_send_failures_keep_retry_rules_distinct() -> None:
    suffix = uuid.uuid4().hex[:12]
    quarantined = _key("quarantine-" + suffix)
    assert _rpc("translation_quarantine", f"'{quarantined.digest}','output_contract'")["status"] == "quarantined"
    assert json.loads(
        _psql(_acquire_sql(quarantined, f"idem:quarantine-{suffix}", f"run:quarantine-{suffix}"))
    )["status"] == "quarantined"

    pre_key = _key("pre-" + suffix)
    pre_idem = f"idem:pre-{suffix}"
    pre_run = f"run:pre-{suffix}"
    assert json.loads(_psql(_acquire_sql(pre_key, pre_idem, pre_run)))["status"] == "leased"
    assert _rpc("translation_mark_failed_before_send", f"'{pre_idem}'")["status"] == "failed_before_send"
    assert json.loads(
        _psql(_acquire_sql(pre_key, f"idem:pre-retry-{suffix}", pre_run))
    )["status"] == "leased"

    post_key = _key("post-" + suffix)
    post_idem = f"idem:post-{suffix}"
    post_run = f"run:post-{suffix}"
    assert json.loads(_psql(_acquire_sql(post_key, post_idem, post_run)))["status"] == "leased"
    assert _rpc("translation_mark_sent", f"'{post_idem}'")["status"] == "sent"
    assert _rpc("translation_mark_charge_unknown", f"'{post_idem}'")["status"] == "charge_unknown"
    blocked = json.loads(_psql(_acquire_sql(post_key, f"idem:post-retry-{suffix}", post_run)))
    assert blocked["status"] == "blocked"
    assert blocked["reservation"]["state"] == "charge_unknown"


def test_python_supabase_client_matches_local_rpc_happy_path() -> None:
    origin, service_role_key = _local_rest_identity()
    client = SupabaseTranslationStore(
        SupabaseTranslationConfig(
            origin,
            service_role_key,
            allow_insecure_loopback=True,
        ),
        transport=_LoopbackRestTransport(),
    )
    suffix = uuid.uuid4().hex[:12]
    key = _key("python-rpc-" + suffix)
    request = AcquireRequest(
        key=key,
        idempotency_key=f"idem:python-rpc-{suffix}",
        run_id=f"run:python-rpc-{suffix}",
        reserved_characters=100,
        limits=BudgetLimits(2000, 15000, 450000),
    )

    acquired = client.acquire(request)
    assert acquired.status is AcquireStatus.LEASED
    assert acquired.reservation is not None
    assert acquired.reservation.request.key == key
    assert client.mark_sent(request.idempotency_key).state is ReservationState.SENT
    record = TranslationCacheRecord(
        key=key,
        translated_title="translated title",
        translated_description="translated description",
        actual_characters=70,
    )
    settled = client.settle(request.idempotency_key, actual_characters=70, record=record)
    assert settled.state is ReservationState.SETTLED
    cached = client.lookup(key)
    assert cached is not None
    assert cached.key == key
    assert cached.translated_title == "translated title"
    assert cached.actual_characters == 70


def test_local_postgres_stale_recovery_preserves_no_paid_retry_rule() -> None:
    suffix = uuid.uuid4().hex[:12]
    key = _key(suffix)
    idem = f"idem:localdb-{suffix}"
    run = f"run:localdb-{suffix}"
    assert json.loads(_psql(_acquire_sql(key, idem, run)))["status"] == "leased"
    _psql(
        "update translation_private.translation_reservations "
        f"set created_at = clock_timestamp() - interval '2 minutes' where idempotency_key = '{idem}'; "
        "select 'updated';"
    )
    recovered = _rpc("translation_recover_stale", f"'{key.digest}',60,60")
    assert recovered["status"] == "failed_before_send"

    second = f"idem:localdb-sent-{suffix}"
    assert json.loads(_psql(_acquire_sql(key, second, run)))["status"] == "leased"
    assert _rpc("translation_mark_sent", f"'{second}'")["status"] == "sent"
    _psql(
        "update translation_private.translation_reservations "
        f"set sent_at = clock_timestamp() - interval '2 minutes' where idempotency_key = '{second}'; "
        "select 'updated';"
    )
    recovered = _rpc("translation_recover_stale", f"'{key.digest}',60,60")
    assert recovered["status"] == "charge_unknown"
    blocked = json.loads(_psql(_acquire_sql(key, f"idem:localdb-retry-{suffix}", run)))
    assert blocked["status"] == "blocked"
    assert blocked["reservation"]["state"] == "charge_unknown"


def test_concurrent_identical_local_postgres_reconciliation_is_idempotent() -> None:
    suffix = uuid.uuid4().hex[:12]
    key = _key(suffix)
    idem = f"idem:localdb-reconcile-{suffix}"
    run = f"run:localdb-reconcile-{suffix}"
    assert json.loads(_psql(_acquire_sql(key, idem, run)))["status"] == "leased"
    _rpc("translation_mark_sent", f"'{idem}'")
    _rpc("translation_mark_charge_unknown", f"'{idem}'")
    evidence = hashlib.sha256(f"evidence:{suffix}".encode()).hexdigest()
    sql = _claims() + (
        " select public.translation_reconcile("
        f"'{idem}','charged','{evidence}',70);"
    )
    executable = shutil.which("psql")
    assert executable is not None
    command = [executable, _dsn(), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "2"}
    first = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    second = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    first_out, _ = first.communicate(timeout=10)
    second_out, _ = second.communicate(timeout=10)

    assert first.returncode == second.returncode == 0
    first_payload = json.loads([line for line in first_out.splitlines() if line.strip()][-1])
    second_payload = json.loads([line for line in second_out.splitlines() if line.strip()][-1])
    assert first_payload == second_payload
    assert first_payload["status"] == "charged_without_cache"
