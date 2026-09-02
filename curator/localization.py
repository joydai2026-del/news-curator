"""Validated translation artifacts and language-specific backend projections.

Translations are presentation overlays. This module never feeds translated text
back into identity, deduplication, category matching, or ranking.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .config import Category
from .models import Item, LocalizedItem, TranslationRecord
from .normalize import clean_title


MAX_ARTIFACT_BYTES = 2_000_000
MAX_TRANSLATION_RECORDS = 500
ARTIFACT_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_VERSION = 1
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_TRANSLATION_KEYS = {
    "story_id",
    "input_digest",
    "source_language",
    "target_language",
    "title",
    "description",
    "provider",
    "model_version",
}


class TranslationArtifactError(ValueError):
    """A low-information artifact validation failure."""


def story_id_for_item(item: Item) -> str:
    """Stable public identity derived only from authoritative item fields."""

    anchor = item.canonical_url or item.url
    if not anchor:
        anchor = "\0".join(
            (item.source_id, item.title, item.published_at.astimezone(timezone.utc).isoformat())
        )
    return "story:" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()


def load_translation_artifact(path: Path) -> tuple[TranslationRecord, ...]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise TranslationArtifactError("artifact_too_large")
        payload = json.loads(raw.decode("utf-8"))
    except TranslationArtifactError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise TranslationArtifactError("artifact_unreadable") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "generated_at", "translations"
    }:
        raise TranslationArtifactError("artifact_schema")
    if payload["schema_version"] != ARTIFACT_SCHEMA_VERSION or not isinstance(
        payload["generated_at"], str
    ) or not _TIMESTAMP.fullmatch(payload["generated_at"]):
        raise TranslationArtifactError("artifact_metadata")
    rows = payload["translations"]
    if not isinstance(rows, list) or len(rows) > MAX_TRANSLATION_RECORDS:
        raise TranslationArtifactError("artifact_record_count")
    records: list[TranslationRecord] = []
    seen: set[tuple[str, str]] = set()
    for raw_record in rows:
        if not isinstance(raw_record, dict) or set(raw_record) != _TRANSLATION_KEYS:
            raise TranslationArtifactError("artifact_record_schema")
        if not all(isinstance(raw_record[key], str) for key in _TRANSLATION_KEYS):
            raise TranslationArtifactError("artifact_record_schema")
        try:
            record = TranslationRecord(
                story_id=raw_record["story_id"],
                input_digest=raw_record["input_digest"],
                source_language=raw_record["source_language"],
                target_language=raw_record["target_language"],
                title=clean_title(raw_record["title"]),
                description=clean_title(raw_record["description"]),
                provider=raw_record["provider"],
                model_version=raw_record["model_version"],
            )
        except (KeyError, TypeError, ValueError):
            raise TranslationArtifactError("artifact_record_invalid") from None
        identity = (record.story_id, record.target_language)
        if identity in seen:
            raise TranslationArtifactError("artifact_duplicate_record")
        seen.add(identity)
        records.append(record)
    return tuple(records)


def write_translation_artifact(
    records: Iterable[TranslationRecord], path: Path, *, generated_at: datetime
) -> Path:
    stable = tuple(records)
    if len(stable) > MAX_TRANSLATION_RECORDS:
        raise TranslationArtifactError("artifact_record_count")
    identities = {(record.story_id, record.target_language) for record in stable}
    if len(identities) != len(stable):
        raise TranslationArtifactError("artifact_duplicate_record")
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generated_at": _timestamp(generated_at),
        "translations": [_translation_dict(record) for record in stable],
    }
    encoded = _safe_json(payload)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise TranslationArtifactError("artifact_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return path


def build_localized_view(
    *,
    target_language: str,
    native_ranked: Mapping[str, list[Item]],
    source_ranked: Mapping[str, list[Item]],
    translations: Iterable[TranslationRecord],
) -> dict[str, list[LocalizedItem]]:
    """Layer validated translations after original-language ranking.

    Native target-language rows are emitted first and claim the story identity.
    A translated projection can fill only an identity the native view lacks.
    """

    if target_language not in {"en", "zh"}:
        raise ValueError("target language must be en or zh")
    records = {
        (record.story_id, record.source_language, record.target_language): record
        for record in translations
    }
    output: dict[str, list[LocalizedItem]] = {}
    for category_name in dict.fromkeys((*native_ranked, *source_ranked)):
        rows: list[LocalizedItem] = []
        seen: set[str] = set()
        validated_provenance: dict[str, TranslationRecord] = {}
        for source_item in source_ranked.get(category_name, []):
            if source_item.is_newsletter:
                continue
            source_story_id = story_id_for_item(source_item)
            try:
                from .translation import TranslationInput

                source_content = TranslationInput.from_item(source_item)
            except (TypeError, ValueError):
                continue
            source_record = records.get(
                (source_story_id, source_item.language, target_language)
            )
            if source_record is not None and source_record.input_digest == source_content.digest:
                validated_provenance[source_story_id] = source_record
        for item in native_ranked.get(category_name, []):
            story_id = story_id_for_item(item)
            if story_id in seen:
                continue
            seen.add(story_id)
            # A newsletter is an original-only private lane. Even a coincident
            # public URL must not attach provider/cache provenance to it.
            provenance = None if item.is_newsletter else validated_provenance.get(story_id)
            rows.append(
                LocalizedItem(
                    story_id=story_id,
                    original=item,
                    display_language=target_language,
                    title=item.title,
                    description=item.description,
                    translation_available=provenance is not None,
                    translation_provider=provenance.provider if provenance else "",
                    translation_model_version=provenance.model_version if provenance else "",
                    translation_source_language=provenance.source_language if provenance else "",
                )
            )
        for item in source_ranked.get(category_name, []):
            if item.is_newsletter:
                continue
            story_id = story_id_for_item(item)
            if story_id in seen:
                continue
            try:
                from .translation import TranslationInput

                content = TranslationInput.from_item(item)
            except (TypeError, ValueError):
                continue
            record = records.get((story_id, item.language, target_language))
            if record is None or record.input_digest != content.digest:
                continue
            seen.add(story_id)
            rows.append(
                LocalizedItem(
                    story_id=story_id,
                    original=item,
                    display_language=target_language,
                    title=record.title,
                    description=record.description,
                    translated=True,
                    translation_available=True,
                    translation_provider=record.provider,
                    translation_model_version=record.model_version,
                    translation_source_language=record.source_language,
                )
            )
        output[category_name] = rows
    return output


def write_localized_projection(
    *,
    language: str,
    categories: Iterable[Category],
    ranked: Mapping[str, list[LocalizedItem]],
    path: Path,
    generated_at: datetime,
) -> Path:
    category_rows = []
    for category in categories:
        category_rows.append(
            {
                "id": category.id,
                "name": category.name,
                "items": [
                    _localized_dict(item) for item in ranked.get(category.name, [])
                ],
            }
        )
    payload = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "generated_at": _timestamp(generated_at),
        "language": language,
        "categories": category_rows,
    }
    encoded = _safe_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return path


def _localized_dict(item: LocalizedItem) -> dict[str, object]:
    original = item.original
    return {
        "story_id": item.story_id,
        "title": item.title,
        "description": item.description,
        "url": original.url,
        "canonical_url": original.canonical_url,
        "source_id": original.source_id,
        "source_name": original.source_name,
        "published_at": original.published_at.astimezone(timezone.utc).isoformat(),
        "original_language": original.language,
        "display_language": item.display_language,
        "translated": item.translated,
        "translation_available": item.translation_available,
        "translation_source_language": item.translation_source_language,
        "translation_provider": item.translation_provider,
        "translation_model_version": item.translation_model_version,
        "image_url": "" if original.is_newsletter else original.image_url,
        "is_newsletter": original.is_newsletter,
    }


def _translation_dict(record: TranslationRecord) -> dict[str, str]:
    return {
        "story_id": record.story_id,
        "input_digest": record.input_digest,
        "source_language": record.source_language,
        "target_language": record.target_language,
        "title": record.title,
        "description": record.description,
        "provider": record.provider,
        "model_version": record.model_version,
    }


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_json(payload: object) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return text.encode("utf-8")
