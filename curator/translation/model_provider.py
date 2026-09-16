"""Chat-model translation adapter behind the existing provider interface.

The provider, the model id, the endpoint origin and the API-key environment
variable name all come from configuration. Nothing here decides which vendor
runs: the registry does, from ``translation.provider``. This exists because the
Google path was never provisioned, so Phase 1 reuses the model the ranker
already uses rather than blocking on a human cloud-provisioning task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from curator.sources import OriginBoundCredential, SafeHttpTransport, SafeTransportError

from .base import (
    DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS,
    DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS,
    TranslationErrorReason,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationProviderResult,
    TranslationResultItem,
)


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ORIGIN = re.compile(r"^https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_LANGUAGE_NAMES = {"en": "English", "zh": "Simplified Chinese"}

# Title and summary only. Names and numbers are preserved because a changed
# number is the exact signal cross-language grouping relies on. The reply must
# be one JSON object with exactly these two fields and nothing else.
PROMPT_VERSION = "translation-json-v1"
_SYSTEM_PROMPT = (
    "You translate one news headline and its one-paragraph summary. "
    "Translate from {source_name} into {target_name}. "
    "Translate the title and the summary only. Preserve every proper name, "
    "organization, product name, and every number, date and unit exactly as "
    "written. Do not add, remove, explain, or comment. "
    'Reply with one JSON object with exactly the fields "title" and "summary", '
    "both strings. The summary is an empty string when the input summary is empty."
)


@dataclass(frozen=True)
class ModelTranslationConfig:
    """Validated adapter policy. Every value is a configuration key."""

    provider_id: str
    model: str
    api_origin: str = "https://api.openai.com"
    api_path: str = "/v1/chat/completions"
    max_request_bytes: int = 32 * 1024
    max_response_bytes: int = 512 * 1024
    max_output_title_chars: int = DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS
    max_output_description_chars: int = DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^[a-z][a-z0-9_-]{0,39}$", self.provider_id or ""):
            raise ValueError("translation provider id is invalid")
        if not _MODEL_ID.fullmatch(self.model or ""):
            raise ValueError("translation model id is invalid")
        if not _ORIGIN.fullmatch(self.api_origin or ""):
            raise ValueError("translation api origin must be an https origin")
        if not _PATH.fullmatch(self.api_path or ""):
            raise ValueError("translation api path is invalid")
        bounds = (self.max_request_bytes, self.max_response_bytes,
                  self.max_output_title_chars, self.max_output_description_chars)
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in bounds):
            raise ValueError("translation model bounds must be positive integers")
        if self.max_output_title_chars > DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS:
            raise ValueError("translation title output limit exceeds the artifact hard bound")
        if self.max_output_description_chars > DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS:
            raise ValueError("translation description output limit exceeds the artifact hard bound")


class ModelTranslationAdapter:
    """One item per call: the cache and budget ledger are per story anyway."""

    def __init__(self, *, config: ModelTranslationConfig, transport: SafeHttpTransport,
                 api_key: Callable[[], str]) -> None:
        self._config = config
        self._transport = transport
        self._api_key = api_key

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    @property
    def model_version(self) -> str:
        """Output-affecting identity recorded in the cache key."""

        return f"{self._config.model}:{PROMPT_VERSION}"

    def translate(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        try:
            return self._translate(request)
        except TranslationProviderError as exc:
            raise TranslationProviderError(self.provider_id, exc.reason) from None
        except Exception:
            raise TranslationProviderError(self.provider_id, TranslationErrorReason.MALFORMED_RESPONSE) from None

    def _translate(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        if len(request.items) != 1:
            self._fail(TranslationErrorReason.INVALID_REQUEST)
        item = request.items[0]
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT.format(
                    source_name=_LANGUAGE_NAMES[request.source_language],
                    target_name=_LANGUAGE_NAMES[request.target_language])},
                {"role": "user", "content": json.dumps(
                    {"title": item.content.title, "summary": item.content.description},
                    ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self._config.max_request_bytes:
            self._fail(TranslationErrorReason.INVALID_REQUEST)
        key = self._load_key()
        credential = OriginBoundCredential(
            origin=self._config.api_origin, header_name="Authorization", value="Bearer " + key)
        try:
            response = self._transport.request(
                "model-translation", "POST", self._config.api_origin + self._config.api_path,
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                body=body, credential=credential, allowed_mime_types=("application/json",))
        except SafeTransportError:
            self._fail(TranslationErrorReason.TRANSPORT_FAILURE)
        if response.status_code != 200:
            self._fail(TranslationErrorReason.PROVIDER_REJECTED)
        if len(response.body) > self._config.max_response_bytes:
            self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
        title, summary, usage = self._parse(response.body)
        if item.content.description and not summary:
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if not item.content.description and summary:
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        return TranslationProviderResult(
            items=(TranslationResultItem(request_id=item.request_id, title=title, description=summary),),
            source_language=request.source_language, target_language=request.target_language,
            provider=self.provider_id, model_version=self.model_version,
            input_tokens=usage[0], output_tokens=usage[1])

    def _parse(self, raw: bytes) -> tuple[str, str, tuple[int, int]]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if not isinstance(envelope, Mapping):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        content = message.get("content")
        if not isinstance(content, str) or not content or len(content) > self._config.max_response_bytes:
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        # Any reply that is not valid JSON with exactly these fields is a
        # translation failure. The story is still shown, marked untranslated.
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if not isinstance(parsed, Mapping) or set(parsed) != {"title", "summary"}:
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        title, summary = parsed["title"], parsed["summary"]
        if not isinstance(title, str) or not title or not isinstance(summary, str):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if any(ord(char) < 32 and char not in "\t\n\r" for char in title + summary):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if len(title) > self._config.max_output_title_chars:
            self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
        if len(summary) > self._config.max_output_description_chars:
            self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
        return title, summary, self._usage(envelope)

    @staticmethod
    def _usage(envelope: Mapping[str, object]) -> tuple[int, int]:
        """Usage is advisory: a missing or malformed block settles as zero and
        the caller then falls back to its own estimate, never to free."""

        usage = envelope.get("usage")
        if not isinstance(usage, Mapping):
            return (0, 0)
        values = []
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            values.append(value if isinstance(value, int) and not isinstance(value, bool)
                          and 0 <= value <= 10_000_000 else 0)
        return (values[0], values[1])

    def _load_key(self) -> str:
        try:
            key = self._api_key()
        except Exception:
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        if not isinstance(key, str) or not key or len(key) > 8_192:
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        if any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in key):
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        return key

    def _fail(self, reason: TranslationErrorReason) -> None:
        raise TranslationProviderError(self._config.provider_id, reason) from None
