"""Contracts for private translation cache and conservative cost accounting.

The caller contract is intentionally strict: ``mark_sent`` must return before
the provider transport is allowed to open a socket. Any outcome after that
durable transition is either settled or charge-unknown, never silently retried.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from .base import TranslationInput
from .base import (
    DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
    DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
    TranslationOutputLimits,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_FIELDS = (("title",), ("title", "description"))


class ReservationState(str, Enum):
    LEASED = "leased"
    SENT = "sent"
    SETTLED = "settled"
    FAILED_BEFORE_SEND = "failed_before_send"
    CHARGE_UNKNOWN = "charge_unknown"
    CHARGED_WITHOUT_CACHE = "charged_without_cache"


class AcquireStatus(str, Enum):
    CACHE_HIT = "cache_hit"
    LEASED = "leased"
    EXISTING = "existing"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    QUARANTINED = "quarantined"


class ReconciliationOutcome(str, Enum):
    CHARGED = "charged"
    CONFIRMED_NOT_SENT = "confirmed_not_sent"


class StoreErrorReason(str, Enum):
    INVALID_REQUEST = "invalid_request"
    INVALID_TRANSITION = "invalid_transition"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"


class TranslationStoreError(RuntimeError):
    """A low-information error that never includes source text or credentials."""

    __slots__ = ("reason",)

    def __init__(self, reason: StoreErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)

    def __str__(self) -> str:
        return f"translation_store: {self.reason.value}"


def _bounded(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _canonical_part(value: str) -> str:
    raw = value.encode("utf-8")
    return f"{len(raw)}:{value}"


@dataclass(frozen=True)
class TranslationCacheKey:
    """The complete versioned cache identity. It contains no source text."""

    story_id: str
    input_digest: str
    field_selection: tuple[str, ...]
    normalization_version: str
    source_locale: str
    target_locale: str
    provider: str
    model_version: str
    glossary_policy_version: str
    candidate_policy_version: str

    def __post_init__(self) -> None:
        _bounded(self.story_id, _IDENTIFIER, "story id")
        _bounded(self.input_digest, _SHA256, "input digest")
        if self.field_selection not in _FIELDS:
            raise ValueError("invalid translation field selection")
        for label, value in (
            ("normalization version", self.normalization_version),
            ("provider", self.provider),
            ("glossary policy version", self.glossary_policy_version),
            ("candidate policy version", self.candidate_policy_version),
        ):
            _bounded(value, _VERSION, label)
        _bounded(self.model_version, _MODEL_IDENTITY, "model version")
        _bounded(self.source_locale, _LOCALE, "source locale")
        _bounded(self.target_locale, _LOCALE, "target locale")
        if self.source_locale == self.target_locale:
            raise ValueError("translation cache locales must differ")

    @classmethod
    def from_input(
        cls,
        *,
        story_id: str,
        content: TranslationInput,
        target_locale: str,
        normalization_version: str,
        provider: str,
        model_version: str,
        glossary_policy_version: str,
        candidate_policy_version: str,
    ) -> "TranslationCacheKey":
        if not isinstance(content, TranslationInput):
            raise TypeError("content must be TranslationInput")
        fields = ("title", "description") if content.description else ("title",)
        return cls(
            story_id=story_id,
            input_digest=content.digest,
            field_selection=fields,
            normalization_version=normalization_version,
            source_locale=content.source_language,
            target_locale=target_locale,
            provider=provider,
            model_version=model_version,
            glossary_policy_version=glossary_policy_version,
            candidate_policy_version=candidate_policy_version,
        )

    @property
    def digest(self) -> str:
        values = (
            "translation-cache-key-v1",
            self.story_id,
            self.input_digest,
            ",".join(self.field_selection),
            self.normalization_version,
            self.source_locale,
            self.target_locale,
            self.provider,
            self.model_version,
            self.glossary_policy_version,
            self.candidate_policy_version,
        )
        canonical = "|".join(_canonical_part(value) for value in values)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "cache_key_digest": self.digest,
            "story_id": self.story_id,
            "input_digest": self.input_digest,
            "field_selection": list(self.field_selection),
            "normalization_version": self.normalization_version,
            "source_locale": self.source_locale,
            "target_locale": self.target_locale,
            "provider": self.provider,
            "model_version": self.model_version,
            "glossary_policy_version": self.glossary_policy_version,
            "candidate_policy_version": self.candidate_policy_version,
        }


@dataclass(frozen=True)
class TranslationCacheRecord:
    key: TranslationCacheKey
    translated_title: str = field(repr=False)
    translated_description: str = field(default="", repr=False)
    actual_characters: int = 0
    created_at: datetime | None = None
    max_title_characters: int = DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS
    max_description_characters: int = DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS

    def __post_init__(self) -> None:
        if not isinstance(self.translated_title, str) or not self.translated_title:
            raise ValueError("translated title must not be empty")
        if not isinstance(self.translated_description, str):
            raise ValueError("translated description must be text")
        limits = TranslationOutputLimits(
            title=self.max_title_characters,
            description=self.max_description_characters,
        )
        limits.validate(self.translated_title, self.translated_description)
        if isinstance(self.actual_characters, bool) or not isinstance(self.actual_characters, int):
            raise ValueError("actual characters must be an integer")
        if self.actual_characters < 0:
            raise ValueError("actual characters must be non-negative")


@dataclass(frozen=True)
class BudgetLimits:
    run: int
    day: int
    month: int

    def __post_init__(self) -> None:
        for value in (self.run, self.day, self.month):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("budget limits must be non-negative integers")


@dataclass(frozen=True)
class AcquireRequest:
    key: TranslationCacheKey
    idempotency_key: str
    run_id: str
    reserved_characters: int
    limits: BudgetLimits

    def __post_init__(self) -> None:
        _bounded(self.idempotency_key, _IDENTIFIER, "idempotency key")
        _bounded(self.run_id, _IDENTIFIER, "run id")
        if (
            isinstance(self.reserved_characters, bool)
            or not isinstance(self.reserved_characters, int)
            or self.reserved_characters <= 0
        ):
            raise ValueError("reserved characters must be a positive integer")

    @property
    def fingerprint(self) -> str:
        value = {
            "cache": self.key.digest,
            "idempotency": self.idempotency_key,
            "run": self.run_id,
            "reserved": self.reserved_characters,
            "limits": [self.limits.run, self.limits.day, self.limits.month],
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Reservation:
    request: AcquireRequest
    state: ReservationState
    counter_day: str
    counter_month: str
    actual_characters: int | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None
    finalized_at: datetime | None = None


@dataclass(frozen=True)
class AcquireResult:
    status: AcquireStatus
    cache: TranslationCacheRecord | None = None
    reservation: Reservation | None = None


class TranslationStore(Protocol):
    def lookup(self, key: TranslationCacheKey) -> TranslationCacheRecord | None: ...
    def acquire(self, request: AcquireRequest) -> AcquireResult: ...
    def recover_stale(
        self,
        key: TranslationCacheKey,
        *,
        lease_timeout_seconds: int,
        sent_timeout_seconds: int,
    ) -> Reservation | None: ...
    def mark_sent(self, idempotency_key: str) -> Reservation: ...
    def settle(
        self,
        idempotency_key: str,
        *,
        actual_characters: int,
        record: TranslationCacheRecord,
    ) -> Reservation: ...
    def mark_failed_before_send(self, idempotency_key: str) -> Reservation: ...
    def mark_charge_unknown(self, idempotency_key: str) -> Reservation: ...
    def quarantine(self, key: TranslationCacheKey, *, reason_code: str) -> None: ...
    def reconcile(
        self,
        idempotency_key: str,
        *,
        outcome: ReconciliationOutcome,
        evidence_digest: str,
        actual_characters: int | None = None,
    ) -> Reservation: ...
