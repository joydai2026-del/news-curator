"""Supabase RPC client for the private translation store.

The service-role credential is an explicitly broad server identity. This
client binds it to one exact HTTPS origin and never places it in a URL, body,
return value, exception, representation, cache record, or log message.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping
from urllib.parse import urlsplit

from curator.sources import OriginBoundCredential, SafeHttpTransport, SafeTransportError

from .store import (
    AcquireRequest,
    AcquireResult,
    AcquireStatus,
    BudgetLimits,
    ReconciliationOutcome,
    Reservation,
    ReservationState,
    StoreErrorReason,
    TranslationCacheKey,
    TranslationCacheRecord,
    TranslationStoreError,
)


MAX_RPC_RESPONSE_BYTES = 256 * 1024


def _jwt_role(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    return decoded.get("role") if isinstance(decoded, dict) and isinstance(decoded.get("role"), str) else None


@dataclass(frozen=True)
class SupabaseTranslationConfig:
    origin: str
    service_role_key: str = field(repr=False)
    allow_insecure_loopback: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        scheme_allowed = parsed.scheme == "https" or (
            parsed.scheme == "http" and self.allow_insecure_loopback and loopback
        )
        if (
            not scheme_allowed
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or "*" in parsed.hostname
        ):
            raise ValueError("Supabase translation origin must be one exact HTTPS origin")
        key = self.service_role_key
        if not isinstance(key, str) or not key or len(key) > 8192:
            raise ValueError("Supabase service-role server identity is invalid")
        if not key.startswith("sb_secret_") and _jwt_role(key) != "service_role":
            raise ValueError("Supabase translation client requires the broad service_role identity")
        object.__setattr__(self, "origin", self.origin.rstrip("/"))


class SupabaseTranslationStore:
    def __init__(self, config: SupabaseTranslationConfig, *, transport: SafeHttpTransport) -> None:
        self._config = config
        self._transport = transport

    def lookup(self, key: TranslationCacheKey) -> TranslationCacheRecord | None:
        payload = self._rpc("translation_cache_lookup", key.as_dict())
        status = payload.get("status")
        if status in ("missing", "quarantined"):
            return None
        if status != "cache_hit" or not isinstance(payload.get("cache"), Mapping):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        cache = _cache_from_mapping(payload["cache"])
        _require_cache_key(cache, key)
        return cache

    def acquire(self, request: AcquireRequest) -> AcquireResult:
        body = {
            **request.key.as_dict(),
            "idempotency_key": request.idempotency_key,
            "run_id": request.run_id,
            "reserved_characters": request.reserved_characters,
            "run_limit": request.limits.run,
            "day_limit": request.limits.day,
            "month_limit": request.limits.month,
        }
        payload = self._rpc("translation_acquire", body)
        raw_status = payload.get("status")
        status = {item.value: item for item in AcquireStatus}.get(raw_status) if isinstance(raw_status, str) else None
        if status is None:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        raw_cache = payload.get("cache")
        raw_reservation = payload.get("reservation")
        if (raw_cache is not None and not isinstance(raw_cache, Mapping)) or (
            raw_reservation is not None and not isinstance(raw_reservation, Mapping)
        ):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        cache = _cache_from_mapping(raw_cache) if isinstance(raw_cache, Mapping) else None
        reservation = (
            _reservation_from_mapping(raw_reservation)
            if isinstance(raw_reservation, Mapping)
            else None
        )
        if status == AcquireStatus.CACHE_HIT:
            if cache is None or reservation is not None:
                raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
            _require_cache_key(cache, request.key)
        elif status in (AcquireStatus.LEASED, AcquireStatus.EXISTING):
            if reservation is None or cache is not None:
                raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
            _require_exact_request(reservation, request)
            if status == AcquireStatus.LEASED and reservation.state is not ReservationState.LEASED:
                raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        elif status == AcquireStatus.BLOCKED:
            if reservation is None or cache is not None:
                raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
            _require_blocker(reservation, request)
        elif cache is not None or reservation is not None:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        return AcquireResult(status, cache=cache, reservation=reservation)

    def recover_stale(
        self,
        key: TranslationCacheKey,
        *,
        lease_timeout_seconds: int,
        sent_timeout_seconds: int,
    ) -> Reservation | None:
        payload = self._rpc(
            "translation_recover_stale",
            {
                "cache_key_digest": key.digest,
                "lease_timeout_seconds": lease_timeout_seconds,
                "sent_timeout_seconds": sent_timeout_seconds,
            },
        )
        if payload.get("status") == "none":
            return None
        mapping = payload.get("reservation")
        if not isinstance(mapping, Mapping):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        reservation = _reservation_from_mapping(mapping)
        if reservation.request.key != key or payload.get("status") != reservation.state.value:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        return reservation

    def mark_sent(self, idempotency_key: str) -> Reservation:
        return self._reservation_rpc(
            "translation_mark_sent",
            {"idempotency_key": idempotency_key},
            expected_idempotency_key=idempotency_key,
        )

    def settle(
        self,
        idempotency_key: str,
        *,
        actual_characters: int,
        record: TranslationCacheRecord,
    ) -> Reservation:
        payload = self._rpc(
            "translation_settle",
            {
                "idempotency_key": idempotency_key,
                "actual_characters": actual_characters,
                "translated_title": record.translated_title,
                "translated_description": record.translated_description,
            },
        )
        if payload.get("status") not in ("settled", "charge_unknown"):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        mapping = payload.get("reservation")
        if not isinstance(mapping, Mapping):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        reservation = _reservation_from_mapping(mapping)
        _require_idempotency(reservation, idempotency_key)
        if reservation.request.key != record.key:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        if payload.get("status") != reservation.state.value:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        if reservation.state is ReservationState.SETTLED and reservation.actual_characters != actual_characters:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        return reservation

    def mark_failed_before_send(self, idempotency_key: str) -> Reservation:
        return self._reservation_rpc(
            "translation_mark_failed_before_send",
            {"idempotency_key": idempotency_key},
            expected_idempotency_key=idempotency_key,
        )

    def mark_charge_unknown(self, idempotency_key: str) -> Reservation:
        return self._reservation_rpc(
            "translation_mark_charge_unknown",
            {"idempotency_key": idempotency_key},
            expected_idempotency_key=idempotency_key,
        )

    def quarantine(self, key: TranslationCacheKey, *, reason_code: str) -> None:
        payload = self._rpc(
            "translation_quarantine",
            {"cache_key_digest": key.digest, "reason_code": reason_code},
        )
        if payload.get("status") != "quarantined":
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)

    def reconcile(
        self,
        idempotency_key: str,
        *,
        outcome: ReconciliationOutcome,
        evidence_digest: str,
        actual_characters: int | None = None,
    ) -> Reservation:
        return self._reservation_rpc(
            "translation_reconcile",
            {
                "idempotency_key": idempotency_key,
                "outcome": outcome.value,
                "evidence_digest": evidence_digest,
                "actual_characters": actual_characters,
            },
            expected_idempotency_key=idempotency_key,
        )

    def _reservation_rpc(
        self,
        name: str,
        body: Mapping[str, object],
        *,
        expected_idempotency_key: str,
    ) -> Reservation:
        payload = self._rpc(name, body)
        mapping = payload.get("reservation")
        if not isinstance(mapping, Mapping):
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        reservation = _reservation_from_mapping(mapping)
        _require_idempotency(reservation, expected_idempotency_key)
        if payload.get("status") != reservation.state.value:
            raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
        return reservation

    def _rpc(self, name: str, body: Mapping[str, object]) -> Mapping[str, object]:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        credentials = (
            OriginBoundCredential(
                origin=self._config.origin,
                header_name="Authorization",
                value="Bearer " + self._config.service_role_key,
            ),
            OriginBoundCredential(
                origin=self._config.origin,
                header_name="apikey",
                value=self._config.service_role_key,
            ),
        )
        sanitized: TranslationStoreError | None = None
        try:
            response = self._transport.request(
                "translation-store",
                "POST",
                f"{self._config.origin}/rest/v1/rpc/{name}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=raw,
                credentials=credentials,
                allowed_mime_types=("application/json",),
            )
            if response.status_code != 200 or len(response.body) > MAX_RPC_RESPONSE_BYTES:
                sanitized = TranslationStoreError(StoreErrorReason.UNAVAILABLE)
            else:
                decoded = json.loads(response.body.decode("utf-8"))
                if not isinstance(decoded, Mapping):
                    sanitized = TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
                else:
                    return decoded
        except (SafeTransportError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            sanitized = TranslationStoreError(StoreErrorReason.UNAVAILABLE)
        raise sanitized or TranslationStoreError(StoreErrorReason.UNAVAILABLE) from None


def _key_from_mapping(value: Mapping[str, object]) -> TranslationCacheKey:
    fields = value.get("field_selection")
    if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
    malformed = False
    key: TranslationCacheKey | None = None
    try:
        key = TranslationCacheKey(
            story_id=_required_string(value, "story_id"),
            input_digest=_required_string(value, "input_digest"),
            field_selection=tuple(fields),
            normalization_version=_required_string(value, "normalization_version"),
            source_locale=_required_string(value, "source_locale"),
            target_locale=_required_string(value, "target_locale"),
            provider=_required_string(value, "provider"),
            model_version=_required_string(value, "model_version"),
            glossary_policy_version=_required_string(value, "glossary_policy_version"),
            candidate_policy_version=_required_string(value, "candidate_policy_version"),
        )
    except (KeyError, TypeError, ValueError):
        malformed = True
    if malformed or key is None:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE) from None
    if value.get("cache_key_digest") != key.digest:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
    return key


def _require_cache_key(record: TranslationCacheRecord, expected: TranslationCacheKey) -> None:
    if record.key != expected or record.key.digest != expected.digest:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)


def _require_exact_request(reservation: Reservation, expected: AcquireRequest) -> None:
    returned = reservation.request
    if returned != expected or returned.fingerprint != expected.fingerprint:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)


def _require_idempotency(reservation: Reservation, expected_idempotency_key: str) -> None:
    if reservation.request.idempotency_key != expected_idempotency_key:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)


def _require_blocker(reservation: Reservation, request: AcquireRequest) -> None:
    if (
        reservation.request.key != request.key
        or reservation.request.key.digest != request.key.digest
        or reservation.request.idempotency_key == request.idempotency_key
        or reservation.state
        not in (
            ReservationState.LEASED,
            ReservationState.SENT,
            ReservationState.CHARGE_UNKNOWN,
            ReservationState.CHARGED_WITHOUT_CACHE,
        )
    ):
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)


def _cache_from_mapping(value: Mapping[str, object]) -> TranslationCacheRecord:
    key = _key_from_mapping(value)
    actual = value.get("actual_characters")
    title = value.get("translated_title")
    description = value.get("translated_description", "")
    if isinstance(actual, bool) or not isinstance(actual, int) or not isinstance(title, str) or not isinstance(description, str):
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE)
    created = value.get("created_at")
    malformed = False
    result: TranslationCacheRecord | None = None
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else None
        result = TranslationCacheRecord(key, title, description, actual, created_at)
    except ValueError:
        malformed = True
    if malformed or result is None:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE) from None
    return result


def _reservation_from_mapping(value: Mapping[str, object]) -> Reservation:
    malformed = False
    result: Reservation | None = None
    try:
        key = _key_from_mapping(value)
        limits = BudgetLimits(
            _required_integer(value, "run_limit"),
            _required_integer(value, "day_limit"),
            _required_integer(value, "month_limit"),
        )
        request = AcquireRequest(
            key=key,
            idempotency_key=_required_string(value, "idempotency_key"),
            run_id=_required_string(value, "run_id"),
            reserved_characters=_required_integer(value, "reserved_characters"),
            limits=limits,
        )
        state = ReservationState(_required_string(value, "state"))
        actual = value.get("actual_characters")
        if actual is not None and (isinstance(actual, bool) or not isinstance(actual, int)):
            raise ValueError
        result = Reservation(
            request=request,
            state=state,
            counter_day=_required_string(value, "counter_day"),
            counter_month=_required_string(value, "counter_month"),
            actual_characters=actual,
            created_at=_optional_datetime(value.get("created_at")),
            sent_at=_optional_datetime(value.get("sent_at")),
            finalized_at=_optional_datetime(value.get("finalized_at")),
        )
    except (KeyError, TypeError, ValueError, TranslationStoreError):
        malformed = True
    if malformed or result is None:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE) from None
    return result


def _required_string(value: Mapping[str, object], field_name: str) -> str:
    field_value = value[field_name]
    if not isinstance(field_value, str):
        raise ValueError
    return field_value


def _required_integer(value: Mapping[str, object], field_name: str) -> int:
    field_value = value[field_name]
    if isinstance(field_value, bool) or not isinstance(field_value, int):
        raise ValueError
    return field_value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    malformed = False
    result: datetime | None = None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        malformed = True
    if malformed or result is None:
        raise TranslationStoreError(StoreErrorReason.MALFORMED_RESPONSE) from None
    return result
