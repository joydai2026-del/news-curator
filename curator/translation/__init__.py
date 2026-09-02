"""Public contracts for the translation bridge."""

from .base import (
    DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
    DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
    TranslationErrorReason,
    TranslationInput,
    TranslationPrivacyError,
    TranslationProvider,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationProviderResult,
    TranslationOutputLimits,
    TranslationRequestItem,
    TranslationResultItem,
)
from .google import GoogleTranslationAdapter, GoogleTranslationConfig
from .registry import TranslationProviderRegistry
from .selector import TranslationCandidatePolicy, select_translation_candidates
from .memory import InMemoryTranslationStore
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
    TranslationStore,
    TranslationStoreError,
)
from .supabase import SupabaseTranslationConfig, SupabaseTranslationStore

__all__ = (
    "GoogleTranslationAdapter",
    "GoogleTranslationConfig",
    "DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS",
    "DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS",
    "InMemoryTranslationStore",
    "AcquireRequest",
    "AcquireResult",
    "AcquireStatus",
    "BudgetLimits",
    "ReconciliationOutcome",
    "Reservation",
    "ReservationState",
    "StoreErrorReason",
    "SupabaseTranslationConfig",
    "SupabaseTranslationStore",
    "TranslationCacheKey",
    "TranslationCacheRecord",
    "TranslationCandidatePolicy",
    "TranslationErrorReason",
    "TranslationInput",
    "TranslationPrivacyError",
    "TranslationProvider",
    "TranslationProviderError",
    "TranslationProviderRegistry",
    "TranslationProviderRequest",
    "TranslationProviderResult",
    "TranslationOutputLimits",
    "TranslationRequestItem",
    "TranslationResultItem",
    "TranslationStore",
    "TranslationStoreError",
    "select_translation_candidates",
)
