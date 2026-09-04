"""An explicit injected translation-provider registry with no global state."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .base import TranslationProvider


_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


class TranslationProviderRegistry:
    def __init__(self, providers: Mapping[str, TranslationProvider] | None = None) -> None:
        self._providers: dict[str, TranslationProvider] = {}
        for provider_id, provider in (providers or {}).items():
            self.register(provider_id, provider)

    def register(self, provider_id: str, provider: TranslationProvider) -> None:
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("translation provider id is invalid")
        if provider_id in self._providers:
            raise ValueError("translation provider id is already registered")
        if getattr(provider, "provider_id", None) != provider_id:
            raise ValueError("translation provider id does not match implementation")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> TranslationProvider:
        try:
            return self._providers[provider_id]
        except KeyError:
            allowed = ", ".join(sorted(self._providers)) or "none"
            raise ValueError(f"unknown translation provider; allowed: {allowed}") from None

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
