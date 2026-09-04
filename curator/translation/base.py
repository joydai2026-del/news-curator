"""Immutable translation contracts with a deliberately narrow privacy boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from curator.models import Item
from curator.normalize import clean_title


SUPPORTED_TRANSLATION_LANGUAGES = frozenset(("en", "zh"))
MAX_TRANSLATION_TITLE_CHARS = 500
MAX_TRANSLATION_DESCRIPTION_CHARS = 2_000
MAX_TRANSLATION_INPUT_CHARS = MAX_TRANSLATION_TITLE_CHARS + MAX_TRANSLATION_DESCRIPTION_CHARS
DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS = 2_000
DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS = 8_000
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FACTORY_TOKEN = object()


class TranslationPrivacyError(ValueError):
    """A record was rejected before it could cross the translation boundary."""


class TranslationErrorReason(str, Enum):
    INVALID_REQUEST = "invalid_request"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    TRANSPORT_FAILURE = "transport_failure"
    PROVIDER_REJECTED = "provider_rejected"
    MALFORMED_RESPONSE = "malformed_response"
    RESPONSE_TOO_LARGE = "response_too_large"


class TranslationProviderError(Exception):
    """A low-information provider error safe for logs and health receipts."""

    __slots__ = ("provider", "reason")

    def __init__(self, provider: str, reason: TranslationErrorReason) -> None:
        self.provider = _safe_provider_id(provider)
        self.reason = reason
        super().__init__(self.provider, self.reason.value)

    @property
    def reason_code(self) -> str:
        return self.reason.value

    def __str__(self) -> str:
        return f"{self.provider}: {self.reason.value}"


@dataclass(frozen=True, init=False)
class TranslationInput:
    """Approved publisher text copied from one normalized, non-newsletter Item.

    The constructor is intentionally private. Callers cannot pass arbitrary
    strings, URLs, preferences, senders, article bodies, or newsletter data.
    """

    title: str = field(repr=False)
    description: str = field(repr=False)
    source_language: str
    digest: str
    character_count: int

    def __init__(
        self,
        *,
        _token: object,
        title: str,
        description: str,
        source_language: str,
    ) -> None:
        if _token is not _FACTORY_TOKEN:
            raise TranslationPrivacyError("TranslationInput must be created from an Item")
        digest = hashlib.sha256(
            ("translation-input-v1\0" + source_language + "\0" + title + "\0" + description).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "source_language", source_language)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "character_count", len(title) + len(description))

    @classmethod
    def from_item(cls, item: Item) -> "TranslationInput":
        """Copy only approved normalized fields from an authoritative Item."""

        if not isinstance(item, Item):
            raise TranslationPrivacyError("only Item records can become translation input")
        if item.is_newsletter:
            raise TranslationPrivacyError("newsletter items cannot become translation input")
        title = str(item.title)
        description = str(item.description or "")
        language = str(item.language or "").lower()
        if language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise TranslationPrivacyError("item language is not supported for translation")
        if not title or title != clean_title(title):
            raise TranslationPrivacyError("item title must already be normalized")
        if description != clean_title(description):
            raise TranslationPrivacyError("item description must already be normalized")
        if len(title) > MAX_TRANSLATION_TITLE_CHARS:
            raise TranslationPrivacyError("item title exceeds the translation input bound")
        if len(description) > MAX_TRANSLATION_DESCRIPTION_CHARS:
            raise TranslationPrivacyError("item description exceeds the translation input bound")
        return cls(_token=_FACTORY_TOKEN, title=title, description=description, source_language=language)


@dataclass(frozen=True)
class TranslationRequestItem:
    request_id: str
    content: TranslationInput

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.request_id):
            raise ValueError("translation request id is invalid")
        if not isinstance(self.content, TranslationInput):
            raise TypeError("translation request content must be TranslationInput")


@dataclass(frozen=True)
class TranslationProviderRequest:
    items: tuple[TranslationRequestItem, ...]
    source_language: str
    target_language: str

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("translation request must contain at least one item")
        if self.source_language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise ValueError("translation source language is invalid")
        if self.target_language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise ValueError("translation target language is invalid")
        if self.source_language == self.target_language:
            raise ValueError("translation source and target languages must differ")
        ids = [item.request_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("translation request ids must be unique")
        if any(item.content.source_language != self.source_language for item in self.items):
            raise ValueError("translation request source languages do not match")


@dataclass(frozen=True)
class TranslationResultItem:
    request_id: str
    title: str
    description: str

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.request_id):
            raise ValueError("translation result id is invalid")
        if not self.title:
            raise ValueError("translated title must not be empty")


@dataclass(frozen=True)
class TranslationProviderResult:
    items: tuple[TranslationResultItem, ...]
    source_language: str
    target_language: str
    provider: str
    model_version: str

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("translation result must contain at least one item")
        if self.source_language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise ValueError("translation result source language is invalid")
        if self.target_language not in SUPPORTED_TRANSLATION_LANGUAGES:
            raise ValueError("translation result target language is invalid")
        if self.source_language == self.target_language:
            raise ValueError("translation result languages must differ")
        if _safe_provider_id(self.provider) != self.provider:
            raise ValueError("translation result provider is invalid")
        if not isinstance(self.model_version, str) or not self.model_version:
            raise ValueError("translation result model version is invalid")
        ids = [item.request_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("translation result ids must be unique")


@dataclass(frozen=True)
class TranslationOutputLimits:
    """Runtime policy applied before any translated result is settled."""

    title: int = DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS
    description: int = DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS

    def __post_init__(self) -> None:
        for value in (self.title, self.description):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("translation output limits must be positive integers")
        if self.title > DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS:
            raise ValueError("translation title limit exceeds the artifact hard bound")
        if self.description > DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS:
            raise ValueError("translation description limit exceeds the artifact hard bound")

    def validate(self, title: str, description: str) -> None:
        if not isinstance(title, str) or not title or len(title) > self.title:
            raise ValueError("translated title exceeds the configured output bound")
        if not isinstance(description, str) or len(description) > self.description:
            raise ValueError("translated description exceeds the configured output bound")


class TranslationProvider(Protocol):
    """Injected provider interface. Implementations must not retain source text."""

    provider_id: str
    model_version: str

    def translate(self, request: TranslationProviderRequest) -> TranslationProviderResult: ...


def _safe_provider_id(value: object) -> str:
    text = str(value or "unknown")
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "._-")[:40]
    return cleaned or "unknown"
