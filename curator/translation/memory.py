"""Deterministic, thread-safe translation store for tests and local dry runs."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from .store import (
    AcquireRequest,
    AcquireResult,
    AcquireStatus,
    ReconciliationOutcome,
    Reservation,
    ReservationState,
    StoreErrorReason,
    TranslationCacheKey,
    TranslationCacheRecord,
    TranslationStoreError,
)


UtcClock = Callable[[], datetime]


def _utc_now(clock: UtcClock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)
    return value.astimezone(timezone.utc)


class InMemoryTranslationStore:
    """Reference implementation with the same conservative transitions as SQL."""

    def __init__(self, *, clock: UtcClock) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: dict[str, object] = {}
        self._reservations: dict[str, Reservation] = {}
        self._counters: dict[tuple[str, str], int] = {}
        self._quarantined: set[str] = set()
        self._reconciliations: dict[str, tuple[ReconciliationOutcome, str, int | None]] = {}

    def lookup(self, key: TranslationCacheKey) -> TranslationCacheRecord | None:
        with self._lock:
            return self._lookup_locked(key)

    def _lookup_locked(self, key: TranslationCacheKey) -> TranslationCacheRecord | None:
        digest = key.digest
        if digest in self._quarantined:
            return None
        value = self._cache.get(digest)
        if value is None:
            return None
        if not isinstance(value, TranslationCacheRecord) or value.key != key:
            self._quarantined.add(digest)
            return None
        return value

    def acquire(self, request: AcquireRequest) -> AcquireResult:
        now = _utc_now(self._clock)
        day = now.date().isoformat()
        month = day[:7]
        with self._lock:
            cached = self._lookup_locked(request.key)
            if cached is not None:
                return AcquireResult(AcquireStatus.CACHE_HIT, cache=cached)
            if request.key.digest in self._quarantined:
                return AcquireResult(AcquireStatus.QUARANTINED)

            existing = self._reservations.get(request.idempotency_key)
            if existing is not None:
                if existing.request.fingerprint != request.fingerprint:
                    raise TranslationStoreError(StoreErrorReason.CONFLICT)
                return AcquireResult(AcquireStatus.EXISTING, reservation=existing)

            blocker = next(
                (
                    reservation
                    for reservation in self._reservations.values()
                    if reservation.request.key.digest == request.key.digest
                    and reservation.state
                    in (
                        ReservationState.LEASED,
                        ReservationState.SENT,
                        ReservationState.CHARGE_UNKNOWN,
                        ReservationState.CHARGED_WITHOUT_CACHE,
                    )
                ),
                None,
            )
            if blocker is not None:
                return AcquireResult(AcquireStatus.BLOCKED, reservation=blocker)

            keys = (("run", request.run_id), ("day", day), ("month", month))
            limits = (request.limits.run, request.limits.day, request.limits.month)
            if any(self._counters.get(key, 0) + request.reserved_characters > limit for key, limit in zip(keys, limits)):
                return AcquireResult(AcquireStatus.BUDGET_EXHAUSTED)

            for key in keys:  # fixed run, day, month order mirrors the SQL locks
                self._counters[key] = self._counters.get(key, 0) + request.reserved_characters
            reservation = Reservation(
                request=request,
                state=ReservationState.LEASED,
                counter_day=day,
                counter_month=month,
                created_at=now,
            )
            self._reservations[request.idempotency_key] = reservation
            return AcquireResult(AcquireStatus.LEASED, reservation=reservation)

    def recover_stale(
        self,
        key: TranslationCacheKey,
        *,
        lease_timeout_seconds: int,
        sent_timeout_seconds: int,
    ) -> Reservation | None:
        """Recover only provably never-sent leases; quarantine stale sent work."""

        if (
            isinstance(lease_timeout_seconds, bool)
            or not isinstance(lease_timeout_seconds, int)
            or lease_timeout_seconds <= 0
            or isinstance(sent_timeout_seconds, bool)
            or not isinstance(sent_timeout_seconds, int)
            or sent_timeout_seconds <= 0
        ):
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        now = _utc_now(self._clock)
        with self._lock:
            current = next(
                (
                    reservation
                    for reservation in self._reservations.values()
                    if reservation.request.key.digest == key.digest
                    and reservation.state in (ReservationState.LEASED, ReservationState.SENT)
                ),
                None,
            )
            if current is None:
                return None
            if current.state == ReservationState.LEASED:
                if current.created_at is None or now < current.created_at + timedelta(seconds=lease_timeout_seconds):
                    return current
                self._release(current, current.request.reserved_characters)
                current = replace(
                    current,
                    state=ReservationState.FAILED_BEFORE_SEND,
                    finalized_at=now,
                )
            else:
                stale_from = current.sent_at or current.created_at
                if stale_from is None or now < stale_from + timedelta(seconds=sent_timeout_seconds):
                    return current
                current = replace(
                    current,
                    state=ReservationState.CHARGE_UNKNOWN,
                    finalized_at=now,
                )
            self._reservations[current.request.idempotency_key] = current
            return current

    def mark_sent(self, idempotency_key: str) -> Reservation:
        now = _utc_now(self._clock)
        with self._lock:
            current = self._reservation(idempotency_key)
            if current.state == ReservationState.LEASED:
                current = replace(current, state=ReservationState.SENT, sent_at=now)
                self._reservations[idempotency_key] = current
                return current
            if current.state in (
                ReservationState.SENT,
                ReservationState.SETTLED,
                ReservationState.CHARGE_UNKNOWN,
                ReservationState.CHARGED_WITHOUT_CACHE,
            ):
                return current
            raise TranslationStoreError(StoreErrorReason.INVALID_TRANSITION)

    def settle(
        self,
        idempotency_key: str,
        *,
        actual_characters: int,
        record: TranslationCacheRecord,
    ) -> Reservation:
        now = _utc_now(self._clock)
        if isinstance(actual_characters, bool) or not isinstance(actual_characters, int) or actual_characters < 0:
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        with self._lock:
            current = self._reservation(idempotency_key)
            if record.key != current.request.key or record.actual_characters != actual_characters:
                raise TranslationStoreError(StoreErrorReason.CONFLICT)
            if actual_characters > current.request.reserved_characters:
                raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
            if current.state == ReservationState.SETTLED:
                if current.actual_characters != actual_characters:
                    raise TranslationStoreError(StoreErrorReason.CONFLICT)
                cached = self._cache.get(record.key.digest)
                if not isinstance(cached, TranslationCacheRecord) or (
                    cached.translated_title != record.translated_title
                    or cached.translated_description != record.translated_description
                    or cached.actual_characters != record.actual_characters
                ):
                    raise TranslationStoreError(StoreErrorReason.CONFLICT)
                return current
            if current.state != ReservationState.SENT:
                raise TranslationStoreError(StoreErrorReason.INVALID_TRANSITION)

            digest = record.key.digest
            existing = self._cache.get(digest)
            if existing is not None and existing != record:
                self._quarantined.add(digest)
                current = replace(current, state=ReservationState.CHARGE_UNKNOWN, finalized_at=now)
                self._reservations[idempotency_key] = current
                return current
            if digest in self._quarantined:
                current = replace(current, state=ReservationState.CHARGE_UNKNOWN, finalized_at=now)
                self._reservations[idempotency_key] = current
                return current

            if existing is None:
                self._cache[digest] = replace(record, created_at=record.created_at or now)
            self._release(current, current.request.reserved_characters - actual_characters)
            current = replace(
                current,
                state=ReservationState.SETTLED,
                actual_characters=actual_characters,
                finalized_at=now,
            )
            self._reservations[idempotency_key] = current
            return current

    def mark_failed_before_send(self, idempotency_key: str) -> Reservation:
        now = _utc_now(self._clock)
        with self._lock:
            current = self._reservation(idempotency_key)
            if current.state == ReservationState.FAILED_BEFORE_SEND:
                return current
            if current.state != ReservationState.LEASED:
                raise TranslationStoreError(StoreErrorReason.INVALID_TRANSITION)
            self._release(current, current.request.reserved_characters)
            current = replace(current, state=ReservationState.FAILED_BEFORE_SEND, finalized_at=now)
            self._reservations[idempotency_key] = current
            return current

    def mark_charge_unknown(self, idempotency_key: str) -> Reservation:
        now = _utc_now(self._clock)
        with self._lock:
            current = self._reservation(idempotency_key)
            if current.state in (
                ReservationState.CHARGE_UNKNOWN,
                ReservationState.SETTLED,
                ReservationState.CHARGED_WITHOUT_CACHE,
            ):
                return current
            if current.state != ReservationState.SENT:
                raise TranslationStoreError(StoreErrorReason.INVALID_TRANSITION)
            current = replace(current, state=ReservationState.CHARGE_UNKNOWN, finalized_at=now)
            self._reservations[idempotency_key] = current
            return current

    def quarantine(self, key: TranslationCacheKey, *, reason_code: str) -> None:
        if not _safe_reason(reason_code):
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        with self._lock:
            self._quarantined.add(key.digest)

    def reconcile(
        self,
        idempotency_key: str,
        *,
        outcome: ReconciliationOutcome,
        evidence_digest: str,
        actual_characters: int | None = None,
    ) -> Reservation:
        now = _utc_now(self._clock)
        if not _sha256(evidence_digest):
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        if outcome == ReconciliationOutcome.CHARGED:
            if isinstance(actual_characters, bool) or not isinstance(actual_characters, int) or actual_characters < 0:
                raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        elif outcome == ReconciliationOutcome.CONFIRMED_NOT_SENT:
            if actual_characters not in (None, 0):
                raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        else:
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)

        decision = (outcome, evidence_digest, actual_characters)
        with self._lock:
            previous = self._reconciliations.get(idempotency_key)
            if previous is not None:
                if previous != decision:
                    raise TranslationStoreError(StoreErrorReason.CONFLICT)
                return self._reservation(idempotency_key)
            current = self._reservation(idempotency_key)
            if current.state != ReservationState.CHARGE_UNKNOWN:
                raise TranslationStoreError(StoreErrorReason.INVALID_TRANSITION)
            if outcome == ReconciliationOutcome.CHARGED:
                assert actual_characters is not None
                if actual_characters > current.request.reserved_characters:
                    raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
                self._release(current, current.request.reserved_characters - actual_characters)
                current = replace(
                    current,
                    state=ReservationState.CHARGED_WITHOUT_CACHE,
                    actual_characters=actual_characters,
                    finalized_at=now,
                )
            else:
                self._release(current, current.request.reserved_characters)
                current = replace(current, state=ReservationState.FAILED_BEFORE_SEND, finalized_at=now)
            self._reservations[idempotency_key] = current
            self._reconciliations[idempotency_key] = decision
            return current

    def counter_snapshot(self, *, run_id: str, at: datetime | None = None) -> dict[str, int]:
        now = (at or _utc_now(self._clock)).astimezone(timezone.utc)
        day = now.date().isoformat()
        month = day[:7]
        with self._lock:
            return {
                "run": self._counters.get(("run", run_id), 0),
                "day": self._counters.get(("day", day), 0),
                "month": self._counters.get(("month", month), 0),
            }

    def reservation(self, idempotency_key: str) -> Reservation:
        with self._lock:
            return self._reservation(idempotency_key)

    def _reservation(self, idempotency_key: str) -> Reservation:
        current = self._reservations.get(idempotency_key)
        if current is None:
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        return current

    def _release(self, reservation: Reservation, amount: int) -> None:
        if amount < 0:
            raise TranslationStoreError(StoreErrorReason.INVALID_REQUEST)
        keys = (
            ("run", reservation.request.run_id),
            ("day", reservation.counter_day),
            ("month", reservation.counter_month),
        )
        for key in keys:
            remaining = self._counters.get(key, 0) - amount
            if remaining < 0:
                raise TranslationStoreError(StoreErrorReason.CONFLICT)
            self._counters[key] = remaining


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _safe_reason(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(char.isalnum() or char in "_-" for char in value)
