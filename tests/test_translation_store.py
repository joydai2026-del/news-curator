import hashlib
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.sources import SafeHttpResponse
from curator.translation import (
    AcquireRequest,
    AcquireStatus,
    BudgetLimits,
    InMemoryTranslationStore,
    ReconciliationOutcome,
    ReservationState,
    StoreErrorReason,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
    TranslationCacheKey,
    TranslationCacheRecord,
    TranslationStoreError,
)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def cache_key(story: str = "story:1") -> TranslationCacheKey:
    return TranslationCacheKey(
        story_id=story,
        input_digest=hashlib.sha256(("private:" + story).encode()).hexdigest(),
        field_selection=("title",),
        normalization_version="v1",
        source_locale="en",
        target_locale="zh",
        provider="google",
        model_version="nmt-v3",
        glossary_policy_version="none-v1",
        candidate_policy_version="bounded-v1",
    )


def request(
    key: TranslationCacheKey | None = None,
    *,
    idem: str = "idem:1",
    run: str = "run:1",
    reserved: int = 100,
    limits: BudgetLimits | None = None,
) -> AcquireRequest:
    return AcquireRequest(key or cache_key(), idem, run, reserved, limits or BudgetLimits(1000, 2000, 3000))


def record(key: TranslationCacheKey | None = None, *, actual: int = 80) -> TranslationCacheRecord:
    return TranslationCacheRecord(key or cache_key(), "translated", "", actual)


def store() -> tuple[InMemoryTranslationStore, Clock]:
    clock = Clock(datetime(2026, 8, 30, 3, 59, tzinfo=timezone.utc))
    return InMemoryTranslationStore(clock=clock), clock


def test_full_happy_path_reserves_sends_settles_and_caches_atomically() -> None:
    backend, _ = store()
    leased = backend.acquire(request())
    assert leased.status == AcquireStatus.LEASED
    assert backend.counter_snapshot(run_id="run:1") == {"run": 100, "day": 100, "month": 100}
    assert backend.mark_sent("idem:1").state == ReservationState.SENT

    settled = backend.settle("idem:1", actual_characters=80, record=record())
    assert settled.state == ReservationState.SETTLED
    assert backend.counter_snapshot(run_id="run:1") == {"run": 80, "day": 80, "month": 80}
    hit = backend.acquire(request(idem="idem:2"))
    assert hit.status == AcquireStatus.CACHE_HIT
    assert hit.cache.translated_title == "translated"


def test_sent_is_durable_and_ambiguous_outcome_stays_fully_counted_and_blocked() -> None:
    backend, _ = store()
    backend.acquire(request())
    backend.mark_sent("idem:1")
    unknown = backend.mark_charge_unknown("idem:1")
    assert unknown.state == ReservationState.CHARGE_UNKNOWN
    assert backend.counter_snapshot(run_id="run:1")["run"] == 100
    blocked = backend.acquire(request(idem="retry:automatic"))
    assert blocked.status == AcquireStatus.BLOCKED
    assert blocked.reservation.state == ReservationState.CHARGE_UNKNOWN
    assert backend.mark_charge_unknown("idem:1") == unknown


def test_only_proven_pre_send_failure_releases_and_can_be_reacquired() -> None:
    backend, _ = store()
    backend.acquire(request())
    failed = backend.mark_failed_before_send("idem:1")
    assert failed.state == ReservationState.FAILED_BEFORE_SEND
    assert backend.counter_snapshot(run_id="run:1")["run"] == 0
    assert backend.acquire(request(idem="idem:2")).status == AcquireStatus.LEASED
    with pytest.raises(TranslationStoreError):
        backend.mark_failed_before_send("idem:2") if backend.mark_sent("idem:2") else None


def test_settlement_is_idempotent_and_rejects_negative_oversized_or_conflicting_actual() -> None:
    backend, _ = store()
    backend.acquire(request())
    backend.mark_sent("idem:1")
    settled = backend.settle("idem:1", actual_characters=80, record=record())
    assert backend.settle("idem:1", actual_characters=80, record=record()) == settled
    with pytest.raises(TranslationStoreError):
        backend.settle("idem:1", actual_characters=79, record=record(actual=79))
    with pytest.raises(TranslationStoreError):
        backend.settle(
            "idem:1",
            actual_characters=80,
            record=TranslationCacheRecord(cache_key(), "different", "", 80),
        )

    other, _ = store()
    other.acquire(request())
    other.mark_sent("idem:1")
    with pytest.raises(TranslationStoreError):
        other.settle("idem:1", actual_characters=101, record=record(actual=101))


def test_budget_limits_are_atomic_across_run_day_and_month() -> None:
    backend, _ = store()
    limits = BudgetLimits(150, 150, 150)
    assert backend.acquire(request(cache_key("story:1"), idem="a", reserved=100, limits=limits)).status == AcquireStatus.LEASED
    assert backend.acquire(request(cache_key("story:2"), idem="b", reserved=51, limits=limits)).status == AcquireStatus.BUDGET_EXHAUSTED
    assert backend.counter_snapshot(run_id="run:1") == {"run": 100, "day": 100, "month": 100}


def test_utc_day_and_month_keys_do_not_follow_local_time() -> None:
    backend, clock = store()
    backend.acquire(request())
    assert backend.counter_snapshot(run_id="run:1") == {"run": 100, "day": 100, "month": 100}
    clock.value = datetime(2026, 9, 1, 0, 1, tzinfo=timezone.utc)
    assert backend.counter_snapshot(run_id="run:1") == {"run": 100, "day": 0, "month": 0}


def test_idempotency_conflict_fails_and_concurrent_acquire_has_one_lease() -> None:
    backend, _ = store()
    barrier = threading.Barrier(12)
    results = []

    def worker(index: int) -> None:
        barrier.wait()
        results.append(backend.acquire(request(idem=f"idem:{index}")))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item.status == AcquireStatus.LEASED for item in results) == 1
    assert sum(item.status == AcquireStatus.BLOCKED for item in results) == 11
    assert backend.counter_snapshot(run_id="run:1")["run"] == 100

    with pytest.raises(TranslationStoreError):
        backend.acquire(request(cache_key("story:other"), idem=results[0].reservation.request.idempotency_key))


def test_quarantine_blocks_corrupt_rows_without_rebuild_stampede() -> None:
    backend, _ = store()
    key = cache_key()
    backend._cache[key.digest] = object()  # deliberate corruption injection at the private test seam
    assert backend.lookup(key) is None
    assert backend.acquire(request(key)).status == AcquireStatus.QUARANTINED
    assert backend.acquire(request(key, idem="other")).status == AcquireStatus.QUARANTINED


def test_reconciliation_is_explicit_idempotent_and_cannot_reverse_decision() -> None:
    backend, _ = store()
    backend.acquire(request())
    backend.mark_sent("idem:1")
    backend.mark_charge_unknown("idem:1")
    evidence = "e" * 64
    first = backend.reconcile(
        "idem:1", outcome=ReconciliationOutcome.CHARGED, evidence_digest=evidence, actual_characters=70
    )
    second = backend.reconcile(
        "idem:1", outcome=ReconciliationOutcome.CHARGED, evidence_digest=evidence, actual_characters=70
    )
    assert first == second
    assert first.state == ReservationState.CHARGED_WITHOUT_CACHE
    assert backend.counter_snapshot(run_id="run:1")["run"] == 70
    retry = backend.acquire(request(idem="idem:2"))
    assert retry.status == AcquireStatus.BLOCKED
    assert retry.reservation.state == ReservationState.CHARGED_WITHOUT_CACHE
    with pytest.raises(TranslationStoreError):
        backend.reconcile(
            "idem:1",
            outcome=ReconciliationOutcome.CONFIRMED_NOT_SENT,
            evidence_digest="f" * 64,
        )


def test_confirmed_not_sent_reconciliation_releases_full_reservation() -> None:
    backend, _ = store()
    backend.acquire(request())
    backend.mark_sent("idem:1")
    backend.mark_charge_unknown("idem:1")
    result = backend.reconcile(
        "idem:1",
        outcome=ReconciliationOutcome.CONFIRMED_NOT_SENT,
        evidence_digest="f" * 64,
    )
    assert result.state == ReservationState.FAILED_BEFORE_SEND
    assert backend.counter_snapshot(run_id="run:1")["run"] == 0


def test_stale_never_sent_lease_releases_but_stale_sent_becomes_charge_unknown() -> None:
    backend, clock = store()
    key = cache_key()
    backend.acquire(request(key))
    clock.value += timedelta(seconds=31)
    recovered = backend.recover_stale(key, lease_timeout_seconds=30, sent_timeout_seconds=30)
    assert recovered.state == ReservationState.FAILED_BEFORE_SEND
    assert backend.counter_snapshot(run_id="run:1")["run"] == 0
    assert backend.acquire(request(key, idem="idem:2")).status == AcquireStatus.LEASED
    backend.mark_sent("idem:2")
    clock.value += timedelta(seconds=31)
    recovered = backend.recover_stale(key, lease_timeout_seconds=30, sent_timeout_seconds=30)
    assert recovered.state == ReservationState.CHARGE_UNKNOWN
    assert backend.counter_snapshot(run_id="run:1")["run"] == 100
    assert backend.acquire(request(key, idem="automatic-retry")).status == AcquireStatus.BLOCKED


def test_concurrent_identical_reconciliation_is_idempotent() -> None:
    backend, _ = store()
    backend.acquire(request())
    backend.mark_sent("idem:1")
    backend.mark_charge_unknown("idem:1")
    barrier = threading.Barrier(8)
    outcomes = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(
            backend.reconcile(
                "idem:1",
                outcome=ReconciliationOutcome.CHARGED,
                evidence_digest="a" * 64,
                actual_characters=70,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 8
    assert {outcome.state for outcome in outcomes} == {ReservationState.CHARGED_WITHOUT_CACHE}
    assert backend.counter_snapshot(run_id="run:1")["run"] == 70


class FakeSafeTransport:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    def request(self, source_id, method, url, **kwargs):
        self.calls.append((source_id, method, url, kwargs))
        return SafeHttpResponse(200, url, {"content-type": "application/json"}, json.dumps(self.payload).encode())


def cache_mapping(key: TranslationCacheKey) -> dict[str, object]:
    return {
        **key.as_dict(),
        "translated_title": "translated",
        "translated_description": "",
        "actual_characters": 80,
        "created_at": "2026-08-30T03:59:00Z",
    }


def reservation_mapping(
    acquire_request: AcquireRequest,
    *,
    state: ReservationState = ReservationState.LEASED,
) -> dict[str, object]:
    return {
        **acquire_request.key.as_dict(),
        "idempotency_key": acquire_request.idempotency_key,
        "run_id": acquire_request.run_id,
        "reserved_characters": acquire_request.reserved_characters,
        "run_limit": acquire_request.limits.run,
        "day_limit": acquire_request.limits.day,
        "month_limit": acquire_request.limits.month,
        "state": state.value,
        "counter_day": "2026-08-30",
        "counter_month": "2026-08",
        "actual_characters": None,
        "created_at": "2026-08-30T03:59:00Z",
        "sent_at": None,
        "finalized_at": None,
    }


def supabase_client(payload) -> SupabaseTranslationStore:
    return SupabaseTranslationStore(
        SupabaseTranslationConfig(
            "https://project.supabase.co",
            "sb_secret_service_role_sentinel",
        ),
        transport=FakeSafeTransport(payload),
    )


def assert_malformed_response(call) -> None:
    with pytest.raises(TranslationStoreError) as caught:
        call()
    assert caught.value.reason is StoreErrorReason.MALFORMED_RESPONSE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_supabase_lookup_rejects_schema_valid_cache_for_a_different_requested_key() -> None:
    requested = cache_key("story:one")
    returned = cache_key("story:two")
    assert_malformed_response(
        lambda: supabase_client(
            {"status": "cache_hit", "cache": cache_mapping(returned)}
        ).lookup(requested)
    )


def test_supabase_acquire_rejects_schema_valid_cache_for_a_different_request() -> None:
    expected = request(cache_key("story:one"))
    returned = cache_key("story:two")
    assert_malformed_response(
        lambda: supabase_client(
            {"status": "cache_hit", "cache": cache_mapping(returned)}
        ).acquire(expected)
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("story_id", "story:two"),
        ("input_digest", "b" * 64),
        ("normalization_version", "v2"),
        ("source_locale", "fr"),
        ("target_locale", "de"),
        ("provider", "other"),
        ("model_version", "nmt-v4"),
        ("glossary_policy_version", "glossary-v2"),
        ("candidate_policy_version", "bounded-v2"),
    ),
)
def test_supabase_acquire_rejects_cross_wired_full_cache_identity(
    field_name: str,
    replacement: str,
) -> None:
    expected = request(cache_key("story:one"))
    returned_key = replace(expected.key, **{field_name: replacement})
    returned = replace(expected, key=returned_key)
    assert returned.fingerprint != expected.fingerprint
    assert_malformed_response(
        lambda: supabase_client(
            {"status": "leased", "reservation": reservation_mapping(returned)}
        ).acquire(expected)
    )


@pytest.mark.parametrize(
    "returned",
    (
        request(idem="idem:other"),
        request(run="run:other"),
        request(reserved=99),
        request(limits=BudgetLimits(999, 2000, 3000)),
        request(limits=BudgetLimits(1000, 1999, 3000)),
        request(limits=BudgetLimits(1000, 2000, 2999)),
    ),
)
def test_supabase_acquire_rejects_cross_wired_idempotency_run_or_budget(
    returned: AcquireRequest,
) -> None:
    expected = request()
    assert returned.fingerprint != expected.fingerprint
    assert_malformed_response(
        lambda: supabase_client(
            {"status": "existing", "reservation": reservation_mapping(returned)}
        ).acquire(expected)
    )


def test_supabase_acquire_accepts_only_a_same_key_prior_reservation_as_blocker() -> None:
    expected = request(idem="idem:new", run="run:new")
    prior = request(
        expected.key,
        idem="idem:prior",
        run="run:prior",
        reserved=90,
        limits=BudgetLimits(900, 1900, 2900),
    )
    result = supabase_client(
        {
            "status": "blocked",
            "reservation": reservation_mapping(
                prior,
                state=ReservationState.CHARGE_UNKNOWN,
            ),
        }
    ).acquire(expected)
    assert result.status is AcquireStatus.BLOCKED
    assert result.reservation is not None
    assert result.reservation.request == prior

    wrong_key = replace(prior, key=cache_key("story:two"))
    assert_malformed_response(
        lambda: supabase_client(
            {
                "status": "blocked",
                "reservation": reservation_mapping(
                    wrong_key,
                    state=ReservationState.CHARGE_UNKNOWN,
                ),
            }
        ).acquire(expected)
    )


def test_supabase_acquire_rejects_status_state_and_response_shape_substitution() -> None:
    expected = request()
    malformed_payloads = (
        {
            "status": "leased",
            "reservation": reservation_mapping(expected, state=ReservationState.SENT),
        },
        {
            "status": "blocked",
            "reservation": reservation_mapping(expected, state=ReservationState.CHARGE_UNKNOWN),
        },
        {
            "status": "budget_exhausted",
            "reservation": reservation_mapping(expected),
        },
        {
            "status": "leased",
            "reservation": reservation_mapping(expected),
            "cache": "untrusted-cross-wired-cache",
        },
    )
    for payload in malformed_payloads:
        assert_malformed_response(lambda payload=payload: supabase_client(payload).acquire(expected))


def test_supabase_lifecycle_reply_must_match_the_requested_idempotency_key() -> None:
    expected = request()
    foreign = request(idem="idem:foreign")
    assert_malformed_response(
        lambda: supabase_client(
            {
                "status": "sent",
                "reservation": reservation_mapping(foreign, state=ReservationState.SENT),
            }
        ).mark_sent(expected.idempotency_key)
    )


def test_supabase_client_binds_broad_service_identity_to_one_https_origin_without_repr_leak(capsys) -> None:
    secret = "sb_secret_service_role_sentinel"
    config = SupabaseTranslationConfig("https://project.supabase.co", secret)
    transport = FakeSafeTransport({"status": "missing"})
    client = SupabaseTranslationStore(config, transport=transport)
    assert client.lookup(cache_key()) is None
    _, method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url == "https://project.supabase.co/rest/v1/rpc/translation_cache_lookup"
    credentials = kwargs["credentials"]
    assert {credential.header_name for credential in credentials} == {"Authorization", "apikey"}
    assert {credential.origin for credential in credentials} == {"https://project.supabase.co"}
    assert "apikey" not in kwargs["headers"]
    assert secret not in repr(config)
    assert secret not in repr(client)
    assert secret not in kwargs["body"].decode()
    output = capsys.readouterr()
    assert secret not in output.out + output.err


@pytest.mark.parametrize("origin", ("http://project.supabase.co", "https://project.supabase.co/path", "https://*.supabase.co"))
def test_supabase_client_rejects_non_exact_https_origins(origin) -> None:
    with pytest.raises(ValueError):
        SupabaseTranslationConfig(origin, "sb_secret_service_role_sentinel")


def test_supabase_client_allows_explicit_insecure_loopback_for_local_harness_only() -> None:
    local = SupabaseTranslationConfig(
        "http://127.0.0.1:54321",
        "sb_secret_service_role_sentinel",
        allow_insecure_loopback=True,
    )
    assert local.origin == "http://127.0.0.1:54321"
    with pytest.raises(ValueError):
        SupabaseTranslationConfig(
            "http://project.supabase.co",
            "sb_secret_service_role_sentinel",
            allow_insecure_loopback=True,
        )


def test_supabase_response_errors_do_not_retain_private_response_text() -> None:
    private = "PRIVATE_TRANSLATED_SENTINEL"
    config = SupabaseTranslationConfig("https://project.supabase.co", "sb_secret_service_role_sentinel")
    transport = FakeSafeTransport({"status": private})
    with pytest.raises(TranslationStoreError) as caught:
        SupabaseTranslationStore(config, transport=transport).acquire(request())
    assert private not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_migration_is_private_service_only_atomic_and_search_path_pinned() -> None:
    sql = (Path(__file__).parents[1] / "supabase/migrations/202608290002_translation_store.sql").read_text()
    lower = sql.lower()
    assert "create schema if not exists translation_private" in lower
    assert "force row level security" in lower
    assert "translation_cache_is_immutable" in lower
    assert "translation_cache_complete_key_unique" in lower
    assert "coalesce(auth.jwt() ->> 'role', '') <> 'service_role'" in lower
    assert "revoke all on all tables in schema translation_private from public, anon, authenticated, service_role" in lower
    assert "grant execute on function public.translation_acquire" in lower
    assert "to service_role" in lower
    assert "to authenticated" not in "\n".join(line for line in lower.splitlines() if line.startswith("grant execute"))
    assert "where state in ('leased', 'sent', 'charge_unknown', 'charged_without_cache')" in lower
    assert "set state = 'charged_without_cache'" in lower
    assert "for update" in lower
    assert "scope_type = 'run'" in lower
    assert lower.index("scope_type = 'run'") < lower.index("scope_type = 'day'") < lower.index("scope_type = 'month'")
    assert lower.count("security definer") == 9
    assert lower.count("set search_path =") >= 14
    assert "actual_characters < 0 or actual_characters > row_value.reserved_characters" in lower
    assert "state = 'charge_unknown'" in lower
    assert "create or replace function public.translation_recover_stale" in lower
    assert lower.index("for update;\n  if not found then") < lower.index("select x.* into previous")
