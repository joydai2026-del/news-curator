"""Bounded Google Cloud Translation v3 REST adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
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


GOOGLE_TRANSLATION_ORIGIN = "https://translation.googleapis.com"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,62}[a-z0-9]$")
_LOCATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MODEL_RESOURCE = re.compile(
    r"^projects/(?P<project>[a-z][a-z0-9-]{4,62}[a-z0-9])"
    r"/locations/(?P<location>[a-z0-9][a-z0-9-]{0,62})"
    r"/models/(?P<model>[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,3})$"
)


@dataclass(frozen=True)
class GoogleTranslationConfig:
    project_id: str
    location: str = "global"
    model_version: str = ""
    max_batch_items: int = 32
    max_fields: int = 64
    max_characters: int = 30_000
    max_request_bytes: int = 128 * 1024
    max_response_bytes: int = 512 * 1024
    max_output_title_chars: int = DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS
    max_output_description_chars: int = DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS

    def __post_init__(self) -> None:
        if not _PROJECT_ID.fullmatch(self.project_id):
            raise ValueError("Google translation project id is invalid")
        if not _LOCATION.fullmatch(self.location):
            raise ValueError("Google translation location is invalid")
        model_version = self.model_version or (
            f"projects/{self.project_id}/locations/{self.location}/models/general/nmt"
        )
        match = _MODEL_RESOURCE.fullmatch(model_version)
        if (
            match is None
            or match.group("project") != self.project_id
            or match.group("location") != self.location
        ):
            raise ValueError("Google translation model resource is invalid")
        object.__setattr__(self, "model_version", model_version)
        bounds = (
            self.max_batch_items,
            self.max_fields,
            self.max_characters,
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_output_title_chars,
            self.max_output_description_chars,
        )
        if any(value <= 0 for value in bounds):
            raise ValueError("Google translation bounds must be positive")
        if self.max_output_title_chars > DEFAULT_MAX_TRANSLATION_OUTPUT_TITLE_CHARS:
            raise ValueError("Google title output limit exceeds the artifact hard bound")
        if self.max_output_description_chars > DEFAULT_MAX_TRANSLATION_OUTPUT_DESCRIPTION_CHARS:
            raise ValueError("Google description output limit exceeds the artifact hard bound")


class GoogleTranslationAdapter:
    provider_id = "google"

    def __init__(
        self,
        *,
        config: GoogleTranslationConfig,
        transport: SafeHttpTransport,
        access_token: Callable[[], str],
    ) -> None:
        self._config = config
        self._transport = transport
        self._access_token = access_token

    @property
    def model_version(self) -> str:
        """Exact output-affecting model resource sent to Google."""

        return self._config.model_version

    def translate(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        sanitized: TranslationProviderError | None = None
        try:
            return self._translate(request)
        except TranslationProviderError as exc:
            sanitized = TranslationProviderError(self.provider_id, exc.reason)
        except Exception:
            sanitized = TranslationProviderError(self.provider_id, TranslationErrorReason.MALFORMED_RESPONSE)
        raise sanitized from None

    def _translate(self, request: TranslationProviderRequest) -> TranslationProviderResult:
        fields: list[tuple[str, str, str]] = []
        if len(request.items) > self._config.max_batch_items:
            self._fail(TranslationErrorReason.INVALID_REQUEST)
        for item in request.items:
            fields.append((item.request_id, "title", item.content.title))
            if item.content.description:
                fields.append((item.request_id, "description", item.content.description))
        if len(fields) > self._config.max_fields:
            self._fail(TranslationErrorReason.INVALID_REQUEST)
        if sum(len(text) for _, _, text in fields) > self._config.max_characters:
            self._fail(TranslationErrorReason.INVALID_REQUEST)

        payload = {
            "contents": [text for _, _, text in fields],
            "mimeType": "text/plain",
            "sourceLanguageCode": request.source_language,
            "targetLanguageCode": request.target_language,
            "model": self._config.model_version,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self._config.max_request_bytes:
            self._fail(TranslationErrorReason.INVALID_REQUEST)

        token = self._load_token()
        credential = OriginBoundCredential(
            origin=GOOGLE_TRANSLATION_ORIGIN,
            header_name="Authorization",
            value="Bearer " + token,
        )
        url = (
            f"{GOOGLE_TRANSLATION_ORIGIN}/v3/projects/{self._config.project_id}"
            f"/locations/{self._config.location}:translateText"
        )
        try:
            response = self._transport.request(
                "google-translation",
                "POST",
                url,
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                body=body,
                credential=credential,
                allowed_mime_types=("application/json",),
            )
        except SafeTransportError:
            self._fail(TranslationErrorReason.TRANSPORT_FAILURE)
        if response.status_code != 200:
            self._fail(TranslationErrorReason.PROVIDER_REJECTED)
        if len(response.body) > self._config.max_response_bytes:
            self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
        parsed = self._parse_json(response.body)
        raw_translations = parsed.get("translations")
        if not isinstance(raw_translations, list) or len(raw_translations) != len(fields):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)

        translated_fields: dict[str, dict[str, str]] = {}
        total_output = 0
        for (request_id, field_name, _), raw in zip(fields, raw_translations, strict=True):
            if not isinstance(raw, Mapping):
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            translated = raw.get("translatedText")
            if not isinstance(translated, str) or not translated:
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            output_limit = (
                self._config.max_output_title_chars
                if field_name == "title"
                else self._config.max_output_description_chars
            )
            if len(translated) > output_limit:
                self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
            detected = raw.get("detectedLanguageCode")
            if detected is not None and _base_language(detected) != request.source_language:
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            if any(ord(char) < 32 and char not in "\t\n\r" for char in translated):
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            translated_fields.setdefault(request_id, {})[field_name] = translated
            total_output += len(translated)
        if total_output > self._config.max_characters * 4:
            self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)

        results: list[TranslationResultItem] = []
        for request_item in request.items:
            values = translated_fields.get(request_item.request_id, {})
            title = values.get("title", "")
            description = values.get("description", "")
            if not title:
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            if request_item.content.description and not description:
                self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
            if len(title) > self._config.max_output_title_chars:
                self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
            if len(description) > self._config.max_output_description_chars:
                self._fail(TranslationErrorReason.RESPONSE_TOO_LARGE)
            results.append(
                TranslationResultItem(
                    request_id=request_item.request_id,
                    title=title,
                    description=description,
                )
            )
        return TranslationProviderResult(
            items=tuple(results),
            source_language=request.source_language,
            target_language=request.target_language,
            provider=self.provider_id,
            model_version=self._config.model_version,
        )

    def _load_token(self) -> str:
        try:
            token = self._access_token()
        except Exception:
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        if not isinstance(token, str) or not token or len(token) > 8_192:
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        if any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token):
            self._fail(TranslationErrorReason.CREDENTIAL_UNAVAILABLE)
        return token

    def _parse_json(self, body: bytes) -> Mapping[str, object]:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if not isinstance(parsed, Mapping):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        if not _bounded_json(parsed, max_depth=8, max_nodes=1_000, max_string_chars=64_000):
            self._fail(TranslationErrorReason.MALFORMED_RESPONSE)
        return parsed

    @staticmethod
    def _fail(reason: TranslationErrorReason) -> None:
        raise TranslationProviderError("google", reason) from None


def _base_language(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().split("-", 1)[0]


def _bounded_json(value: object, *, max_depth: int, max_nodes: int, max_string_chars: int) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        if isinstance(current, str):
            if len(current) > max_string_chars:
                return False
        elif isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > 100:
                    return False
                stack.append((child, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(current, (bytes, bytearray)):
            stack.extend((child, depth + 1) for child in current)
        elif current is not None and not isinstance(current, (bool, int, float)):
            return False
    return True
