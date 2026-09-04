"""Pure candidate selection that enforces privacy before logging or storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from curator.models import Item

from .base import (
    SUPPORTED_TRANSLATION_LANGUAGES,
    TranslationInput,
    TranslationPrivacyError,
    TranslationRequestItem,
)


@dataclass(frozen=True)
class TranslationCandidatePolicy:
    max_items: int = 25
    max_characters: int = 2_000

    def __post_init__(self) -> None:
        if self.max_items <= 0 or self.max_characters <= 0:
            raise ValueError("translation candidate bounds must be positive")


def select_translation_candidates(
    items: Iterable[Item],
    *,
    target_language: str,
    policy: TranslationCandidatePolicy | None = None,
) -> tuple[TranslationRequestItem, ...]:
    """Return a bounded, stable batch containing only approved text snapshots.

    This function is intentionally dependency-free. Newsletter and non-Item
    inputs fail before any caller can perform logging, cache lookup, budget
    accounting, or provider invocation.
    """

    if target_language not in SUPPORTED_TRANSLATION_LANGUAGES:
        raise ValueError("translation target language is invalid")
    selected_policy = policy or TranslationCandidatePolicy()
    approved: list[TranslationRequestItem] = []
    seen: set[str] = set()
    used_characters = 0

    for item in items:
        if not isinstance(item, Item):
            raise TranslationPrivacyError("only Item records may be translation candidates")
        if item.is_newsletter:
            raise TranslationPrivacyError("newsletter items are rejected before candidate selection")
        content = TranslationInput.from_item(item)
        if content.source_language == target_language or content.digest in seen:
            continue
        if len(approved) >= selected_policy.max_items:
            break
        if used_characters + content.character_count > selected_policy.max_characters:
            continue
        request_id = "t-" + content.digest[:32]
        approved.append(TranslationRequestItem(request_id=request_id, content=content))
        seen.add(content.digest)
        used_characters += content.character_count
    return tuple(approved)
