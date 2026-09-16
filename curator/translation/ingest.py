"""Translate language-exclusive stories at retained-corpus ingest.

Ingest is where ``Item`` records exist, so the existing privacy boundary in
``TranslationInput.from_item`` is preserved with no new bypass, and the result
lands on the retained row the M2 reader actually serves. A story whose
translation fails, is over budget, or is still pending is NEVER dropped: it is
returned with an untranslated status so the reader can show the original and
say so.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from curator.models import Item
from curator.normalize import clean_title

from .base import (
    SUPPORTED_TRANSLATION_LANGUAGES,
    TranslationInput,
    TranslationPrivacyError,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationRequestItem,
    TranslationOutputLimits,
)
from .store import (
    AcquireRequest,
    AcquireStatus,
    BudgetLimits,
    ReservationState,
    TranslationCacheKey,
    TranslationCacheRecord,
)


TRANSLATED = "translated"
UNTRANSLATED = "untranslated"
ON_FAILURE_VALUES = ("show_original_marked", "show_original_silent")


@dataclass(frozen=True)
class IngestTranslationPolicy:
    """Every operational value is a validated config key with a safe default."""

    enabled: bool = False
    provider: str = "google"
    display_language: str = "en"
    run_character_limit: int = 2_000
    day_character_limit: int = 15_000
    month_character_limit: int = 450_000
    daily_cost_limit_usd: float = 0.50
    cost_per_1k_characters_usd: float = 0.002
    cache_ttl_days: int = 30
    on_failure: str = "show_original_marked"
    max_items: int = 25
    normalization_version: str = "normalized-item-v1"
    glossary_policy_version: str = "none-v1"
    candidate_policy_version: str = "ranked-non-newsletter-v1"
    lease_timeout_seconds: int = 900
    sent_timeout_seconds: int = 900
    output_limits: TranslationOutputLimits = field(default_factory=TranslationOutputLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("translation.enabled must be a boolean")
        if self.display_language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise ValueError("language.default_display must be a supported language")
        if self.on_failure not in ON_FAILURE_VALUES:
            raise ValueError("translation.on_failure must be show_original_marked or show_original_silent")
        for label, value, low, high in (
            ("translation.run_character_limit", self.run_character_limit, 100, 100_000),
            ("translation.day_character_limit", self.day_character_limit, 100, 1_000_000),
            ("translation.month_character_limit", self.month_character_limit, 1_000, 20_000_000),
            ("translation.cache_ttl_days", self.cache_ttl_days, 1, 365),
            ("translation.max_items", self.max_items, 1, 1_000),
            ("translation.lease_timeout_seconds", self.lease_timeout_seconds, 1, 86_400),
            ("translation.sent_timeout_seconds", self.sent_timeout_seconds, 1, 86_400),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{label} must be an integer in [{low}, {high}]")
        for label, value, low, high in (
            ("translation.daily_cost_limit_usd", self.daily_cost_limit_usd, 0.0, 25.0),
            ("translation.cost_per_1k_characters_usd", self.cost_per_1k_characters_usd, 0.0, 1.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
                raise ValueError(f"{label} must be a number in [{low}, {high}]")

    @property
    def character_allowance(self) -> int:
        """Characters bound volume, dollars bound spend. Both must be checked."""

        if self.cost_per_1k_characters_usd <= 0:
            return self.day_character_limit
        by_cost = math.floor(self.daily_cost_limit_usd * 1_000 / self.cost_per_1k_characters_usd)
        return min(self.day_character_limit, self.run_character_limit, by_cost)


@dataclass(frozen=True)
class TranslationOverlay:
    title_translations: Mapping[str, str]
    summary_translations: Mapping[str, str]
    status: str
    reason: str = ""


@dataclass(frozen=True)
class IngestTranslationResult:
    overlays: Mapping[str, TranslationOverlay]
    counters: Mapping[str, int]

    @property
    def untranslated_shown(self) -> int:
        """The operator's signal that the budget or the provider is degraded."""

        return sum(1 for overlay in self.overlays.values() if overlay.status == UNTRANSLATED)


def _untranslated(reason: str) -> TranslationOverlay:
    return TranslationOverlay({}, {}, UNTRANSLATED, reason)


def translate_exclusive_stories(
    stories: Sequence[tuple[str, Item]],
    *,
    policy: IngestTranslationPolicy,
    store,
    provider,
    run_id: str,
    now: datetime,
) -> IngestTranslationResult:
    """Translate into ``policy.display_language`` only. One story, one call."""

    counters: Counter[str] = Counter()
    overlays: dict[str, TranslationOverlay] = {}
    if not policy.enabled:
        return IngestTranslationResult({}, {"disabled": 1})
    target = policy.display_language
    limits = BudgetLimits(policy.run_character_limit, policy.day_character_limit, policy.month_character_limit)
    allowance = policy.character_allowance
    spent_characters = 0
    ttl = timedelta(days=policy.cache_ttl_days)

    for story_id, item in stories[: policy.max_items]:
        try:
            content = TranslationInput.from_item(item)
        except TranslationPrivacyError:
            counters["rejected_by_privacy_boundary"] += 1
            overlays[story_id] = _untranslated("invalid_request")
            continue
        if content.source_language == target:
            continue
        key = TranslationCacheKey.from_input(
            story_id=story_id, content=content, target_locale=target,
            normalization_version=policy.normalization_version,
            provider=provider.provider_id, model_version=provider.model_version,
            glossary_policy_version=policy.glossary_policy_version,
            candidate_policy_version=policy.candidate_policy_version)
        cached = _fresh_cache(store, key, now=now, ttl=ttl, counters=counters)
        if cached is not None:
            # Content-hash cache. The same story is never translated twice.
            counters["cache_hit"] += 1
            overlays[story_id] = _translated(cached.translated_title, cached.translated_description, target)
            continue
        if spent_characters + content.character_count > allowance:
            counters["budget_exhausted"] += 1
            overlays[story_id] = _untranslated("budget_exhausted")
            continue
        candidate = TranslationRequestItem(request_id="t-" + content.digest[:32], content=content)
        overlay = _paid_translation(
            store=store, provider=provider, policy=policy, key=key, candidate=candidate,
            target=target, run_id=run_id, limits=limits, counters=counters)
        overlays[story_id] = overlay
        if overlay.status == TRANSLATED:
            spent_characters += content.character_count
    return IngestTranslationResult(overlays, dict(counters))


def _translated(title: str, summary: str, target: str) -> TranslationOverlay:
    return TranslationOverlay({target: title}, ({target: summary} if summary else {}), TRANSLATED)


def _fresh_cache(store, key, *, now: datetime, ttl: timedelta, counters: Counter) -> TranslationCacheRecord | None:
    try:
        cached = store.lookup(key)
    except Exception:
        counters["cache_unavailable"] += 1
        return None
    if cached is None:
        return None
    created = cached.created_at
    if created is not None and now - created > ttl:
        # Stale but present still beats an empty card; it is refreshed here at
        # ingest, never evicted on the read path.
        counters["cache_expired"] += 1
        return None
    return cached


def _paid_translation(*, store, provider, policy, key, candidate, target, run_id, limits, counters) -> TranslationOverlay:
    idempotency_key = f"{run_id}:{key.digest[:40]}"
    request = AcquireRequest(key=key, idempotency_key=idempotency_key, run_id=run_id,
                             reserved_characters=candidate.content.character_count, limits=limits)
    try:
        store.recover_stale(key, lease_timeout_seconds=policy.lease_timeout_seconds,
                            sent_timeout_seconds=policy.sent_timeout_seconds)
    except Exception:
        counters["recover_failed"] += 1
    try:
        acquired = store.acquire(request)
    except Exception:
        counters["acquire_failed"] += 1
        return _untranslated("acquire_failed")
    if acquired.status == AcquireStatus.CACHE_HIT and acquired.cache is not None and acquired.cache.key == key:
        counters["cache_hit"] += 1
        return _translated(acquired.cache.translated_title, acquired.cache.translated_description, target)
    if acquired.status != AcquireStatus.LEASED:
        counters[acquired.status.value] += 1
        return _untranslated(acquired.status.value)
    try:
        sent = store.mark_sent(idempotency_key)
    except Exception:
        counters["mark_sent_failed"] += 1
        return _untranslated("mark_sent_failed")
    if sent.state != ReservationState.SENT:
        counters["charge_unknown"] += 1
        return _untranslated("charge_unknown")
    try:
        response = provider.translate(TranslationProviderRequest(
            items=(candidate,), source_language=candidate.content.source_language, target_language=target))
        if (response.provider != provider.provider_id or response.model_version != provider.model_version
                or len(response.items) != 1 or response.items[0].request_id != candidate.request_id):
            raise ValueError("provider contract mismatch")
        title = clean_title(response.items[0].title)
        summary = clean_title(response.items[0].description)
        policy.output_limits.validate(title, summary)
        record = TranslationCacheRecord(
            key=key, translated_title=title, translated_description=summary,
            actual_characters=candidate.content.character_count,
            max_title_characters=policy.output_limits.title,
            max_description_characters=policy.output_limits.description)
    except TranslationProviderError as error:
        counters["provider_failed"] += 1
        _mark_unknown(store, idempotency_key, counters)
        return _untranslated(error.reason_code)
    except Exception:
        counters["provider_contract_failed"] += 1
        _mark_unknown(store, idempotency_key, counters)
        return _untranslated("malformed_response")
    try:
        settled = store.settle(idempotency_key, actual_characters=candidate.content.character_count, record=record)
    except Exception:
        counters["settlement_failed"] += 1
        _mark_unknown(store, idempotency_key, counters)
        return _untranslated("settlement_failed")
    if settled.state != ReservationState.SETTLED:
        counters["charge_unknown"] += 1
        return _untranslated("charge_unknown")
    counters["translated"] += 1
    return _translated(title, summary, target)


def _mark_unknown(store, idempotency_key: str, counters: Counter) -> None:
    try:
        store.mark_charge_unknown(idempotency_key)
    except Exception:
        counters["persistence_unknown"] += 1
