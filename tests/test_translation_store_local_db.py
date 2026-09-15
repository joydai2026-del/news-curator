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
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

import pytest

from curator.sources import SafeHttpResponse
from curator.translation import (
    AcquireRequest,
    AcquireStatus,
    BudgetLimits,
    MoneyLimits,
    MoneyReservation,
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


def _money_counters() -> dict[str, int]:
    raw = _psql(
        "select coalesce(jsonb_object_agg(scope_type, counted_microusd), '{}'::jsonb) "
        "from translation_private.translation_usage_counters where scope_key like 'money:%';"
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


class _LocalPostgresRpcTransport:
    """Test-only adapter for SupabaseTranslationStore against an isolated local DB.

    The local PostgreSQL template has no PostgREST listener. This deliberately
    narrow transport invokes only the RPCs used in this proof and returns their
    JSON exactly as the Supabase client expects. It never opens a non-local
    connection and does not carry a credential into a command or test output.
    """

    def request(self, source_id: str, method: str, url: str, **kwargs: object) -> SafeHttpResponse:
        parsed = urlsplit(url)
        assert source_id == "translation-store" and method == "POST"
        assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port == 5432
        assert parsed.path.startswith("/rest/v1/rpc/")
        credentials = kwargs.get("credentials")
        assert isinstance(credentials, tuple) and len(credentials) == 2
        body = json.loads(bytes(kwargs["body"]).decode("utf-8"))
        assert isinstance(body, dict)
        name = parsed.path.rsplit("/", 1)[-1]
        response = _psql(_claims() + " select public." + name + "(" + self._args(name, body) + ");")
        return SafeHttpResponse(200, url, MappingProxyType({"content-type": "application/json"}), response.encode())

    @staticmethod
    def _text(value: object) -> str:
        assert isinstance(value, str)
        return "'" + value.replace("'", "''") + "'"

    def _args(self, name: str, body: dict[str, object]) -> str:
        text = self._text
        integer = lambda key: str(_require_int(body[key]))
        if name == "translation_acquire":
            fields = body["field_selection"]
            assert isinstance(fields, list) and all(isinstance(value, str) for value in fields)
            array = "array[" + ",".join(text(value) for value in fields) + "]::text[]"
            return ",".join((
                text(body["cache_key_digest"]), text(body["story_id"]), text(body["input_digest"]), array,
                text(body["normalization_version"]), text(body["source_locale"]), text(body["target_locale"]),
                text(body["provider"]), text(body["model_version"]), text(body["glossary_policy_version"]),
                text(body["candidate_policy_version"]), text(body["idempotency_key"]), text(body["run_id"]),
                integer("reserved_characters"), integer("run_limit"), integer("day_limit"), integer("month_limit"),
            ))
        if name == "translation_reserve_money":
            return ",".join((text(body["idempotency_key"]), text(body["charge_scope"]), integer("reserved_microusd"), integer("run_limit_microusd"), integer("day_limit_microusd"), integer("month_limit_microusd")))
        if name == "translation_settle_money":
            return ",".join((text(body["idempotency_key"]), integer("actual_microusd")))
        if name in {"translation_mark_sent", "translation_mark_failed_before_send", "translation_mark_charge_unknown"}:
            return text(body["idempotency_key"])
        if name == "translation_settle":
            return ",".join((text(body["idempotency_key"]), integer("actual_characters"), text(body["translated_title"]), text(body["translated_description"])))
        raise AssertionError("unexpected local translation RPC")


def _require_int(value: object) -> int:
    assert not isinstance(value, bool) and isinstance(value, int)
    return value


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


def test_money_rpc_caps_idempotence_nulls_and_never_sent_release() -> None:
    suffix = uuid.uuid4().hex[:12]
    idem = f"idem:money-{suffix}"
    run = f"run:money-{suffix}"
    assert json.loads(_psql(_acquire_sql(_key("money-" + suffix), idem, run)))["status"] == "leased"
    args = f"'{idem}','public_translation_openai_v1',100,150,150,150"
    first = _rpc("translation_reserve_money", args)
    second = _rpc("translation_reserve_money", args)
    assert first == second
    assert first["reservation"]["reserved_microusd"] == 100

    null_call = _raw_psql(
        _claims() + f" select public.translation_reserve_money('{idem}',null,100,150,150,150);"
    )
    assert null_call.returncode != 0

    other = f"idem:money-other-{suffix}"
    assert json.loads(
        _psql(_acquire_sql(_key("money-other-" + suffix), other, run))
    )["status"] == "leased"
    exhausted_call = _raw_psql(
        _claims()
        + f" select public.translation_reserve_money('{other}','public_translation_openai_v1',51,150,150,150);"
    )
    assert exhausted_call.returncode == 0, exhausted_call.stderr
    exhausted = json.loads(
        [line for line in exhausted_call.stdout.splitlines() if line.strip()][-1]
    )
    assert exhausted["status"] == "budget_exhausted"

    assert _rpc("translation_mark_failed_before_send", f"'{idem}'")["status"] == "failed_before_send"
    remaining = int(
        _psql(
            "select coalesce(sum(counted_microusd),0) "
            "from translation_private.translation_usage_counters"
        )
    )
    assert remaining == 0


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


@pytest.mark.allow_socket
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


def _captured_openai_keys() -> tuple[TranslationCacheKey, TranslationCacheKey, TranslationCacheKey]:
    """Build test-only keys from three public retained rows without emitting content."""

    fixture = Path(__file__).parent / "fixtures" / "m2-retained-public.json"
    rows = json.loads(fixture.read_text(encoding="utf-8"))["rows"]
    keys: list[TranslationCacheKey] = []
    for row in rows:
        language = row.get("language")
        title = row.get("title")
        summary = row.get("summary")
        story_id = row.get("story_id")
        canonical_url = row.get("canonical_url")
        if language not in {"en", "zh"} or not all(isinstance(value, str) and value for value in (title, story_id, canonical_url)):
            continue
        original = (canonical_url + "\\n" + title + "\\n" + (summary if isinstance(summary, str) else "")).encode("utf-8")
        keys.append(
            TranslationCacheKey(
                story_id=story_id,
                input_digest=hashlib.sha256(original).hexdigest(),
                field_selection=("title", "description") if summary else ("title",),
                normalization_version="normalized-item-v1",
                source_locale=language,
                target_locale="zh" if language == "en" else "en",
                provider="openai",
                model_version="gpt-5-mini",
                glossary_policy_version="none-v1",
                candidate_policy_version="retained-round-robin-v1",
            )
        )
        if len(keys) == 3:
            return tuple(keys)  # type: ignore[return-value]
    raise AssertionError("captured public fixture has fewer than three usable rows")


class _ControlledPublicProvider:
    """A no-network protocol double that records only the number of sends."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self) -> tuple[int, int]:
        self.calls += 1
        # 64 input and 32 output tokens at configured prices cost 16 + 64 micro-USD.
        return (64, 32)


@pytest.mark.allow_socket
def test_supabase_store_money_path_uses_three_public_rows_one_shared_cap_and_no_third_send() -> None:
    """Controlled local protocol case. It never calls a provider or production service."""

    first_key, second_key, blocked_key = _captured_openai_keys()
    store = SupabaseTranslationStore(
        SupabaseTranslationConfig(
            "http://127.0.0.1:5432",
            "sb_secret_local_test",
            allow_insecure_loopback=True,
        ),
        transport=_LocalPostgresRpcTransport(),
    )
    limits = MoneyLimits(run=250, day=250, month=250)
    money = MoneyReservation("public_translation_openai_v1", 100, limits)
    run_id = "run:money-store-" + uuid.uuid4().hex[:12]
    provider = _ControlledPublicProvider()

    def request_for(key: TranslationCacheKey, suffix: str) -> AcquireRequest:
        return AcquireRequest(
            key=key,
            idempotency_key="idem:money-store-" + suffix + "-" + uuid.uuid4().hex[:12],
            run_id=run_id,
            reserved_characters=50,
            limits=BudgetLimits(500, 500, 500),
            money=money,
        )

    # A proven pre-send failure releases both character and money holds.
    abandoned = request_for(first_key, "abandoned")
    assert store.acquire(abandoned).status is AcquireStatus.LEASED
    assert store.mark_failed_before_send(abandoned.idempotency_key).state is ReservationState.FAILED_BEFORE_SEND
    assert _money_counters() == {"run": 0, "day": 0, "month": 0}

    # The same captured story may subsequently be sent. Its unknown outcome keeps the full hold.
    unknown = request_for(first_key, "unknown")
    assert store.acquire(unknown).status is AcquireStatus.LEASED
    assert store.mark_sent(unknown.idempotency_key).state is ReservationState.SENT
    provider.send()
    assert store.mark_charge_unknown(unknown.idempotency_key).state is ReservationState.CHARGE_UNKNOWN
    assert _money_counters() == {"run": 100, "day": 100, "month": 100}

    # A second public story settles exact known usage: ceil(64*.25) + ceil(32*2) = 80 micro-USD.
    settled_request = request_for(second_key, "settled")
    assert store.acquire(settled_request).status is AcquireStatus.LEASED
    assert store.mark_sent(settled_request.idempotency_key).state is ReservationState.SENT
    input_tokens, output_tokens = provider.send()
    actual_microusd = (input_tokens * 250_000 + 999_999) // 1_000_000 + (output_tokens * 2_000_000 + 999_999) // 1_000_000
    record = TranslationCacheRecord(settled_request.key, "translated", "summary", 50)
    assert store.settle(settled_request.idempotency_key, actual_characters=50, record=record, actual_microusd=actual_microusd).state is ReservationState.SETTLED
    assert store.settle(settled_request.idempotency_key, actual_characters=50, record=record, actual_microusd=actual_microusd).state is ReservationState.SETTLED
    assert _money_counters() == {"run": 180, "day": 180, "month": 180}

    # The third distinct captured row cannot reserve the remaining shared cap, so no third send occurs.
    blocked = request_for(blocked_key, "blocked")
    assert store.acquire(blocked).status is AcquireStatus.BUDGET_EXHAUSTED
    assert provider.calls == 2
    assert _money_counters() == {"run": 180, "day": 180, "month": 180}
    assert _psql(
        "select state from translation_private.translation_reservations where idempotency_key = "
        + _LocalPostgresRpcTransport._text(blocked.idempotency_key)
    ) == "failed_before_send"
    assert _psql(
        "select count(distinct run_id)::text || ':' || count(distinct counter_day)::text "
        "from translation_private.translation_reservations where run_id = "
        + _LocalPostgresRpcTransport._text(run_id)
    ) == "1:1"


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
