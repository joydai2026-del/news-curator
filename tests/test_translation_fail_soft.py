"""Translation faults must leave the authoritative nonempty originals usable."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from curator.config import Category, Config
from curator.localization import (
    TranslationArtifactError,
    build_localized_view,
    load_translation_artifact,
    story_id_for_item,
)
from curator.models import Item
from curator.translation import (
    InMemoryTranslationStore,
    TranslationErrorReason,
    TranslationProviderError,
)
from scripts.run_translation_job import produce_translation_records


NOW = datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)


def _item(*, source_language: str = "en", newsletter: bool = False) -> Item:
    title = "AI publisher original" if source_language == "en" else "人工智能来源原文"
    description = "Original summary" if source_language == "en" else "来源摘要"
    suffix = "newsletter" if newsletter else "original"
    return Item(
        title=title + (" private newsletter" if newsletter else ""),
        description=description,
        url=f"https://example.com/{source_language}/{suffix}",
        canonical_url=f"https://example.com/{source_language}/{suffix}",
        source_id="publisher",
        source_name="Publisher",
        published_at=NOW,
        language=source_language,
        is_newsletter=newsletter,
    )


def _cfg(*, enabled: bool = True, target_language: str = "zh") -> Config:
    return Config(
        categories=[Category(name="AI", id="ai", keywords=["AI"])],
        rss=[],
        settings={"max_age_hours": 48},
        ranking={},
        dedup={},
        hackernews={"enabled": False},
        reddit={},
        translation={
            "enabled": enabled,
            "provider": "google",
            "targets": [target_language],
            "max_items_per_language": 5,
            "max_characters_per_language": 1000,
            "run_character_limit": 1000,
            "day_character_limit": 2000,
            "month_character_limit": 3000,
            "normalization_version": "normalized-item-v1",
            "glossary_policy_version": "none-v1",
            "candidate_policy_version": "ranked-v1",
            "model_version": "google-nmt-v3",
        },
    )


def _original_projection(original: Item):
    return build_localized_view(
        target_language=original.language,
        native_ranked={"AI": [original]},
        source_ranked={"AI": []},
        translations=(),
    )["AI"]


class FailingProvider:
    provider_id = "google"
    model_version = "projects/valid-project-123/locations/global/models/general/nmt"

    def __init__(self) -> None:
        self.calls = []

    def translate(self, request):
        self.calls.append(request)
        raise RuntimeError("private provider detail")


class FailingAcquireStore(InMemoryTranslationStore):
    def acquire(self, request):
        raise RuntimeError("private store detail")


class _TimeoutProvider(FailingProvider):
    def translate(self, request):
        self.calls.append(request)
        raise TimeoutError("provider timeout detail")


class _MissingCredentialProvider(FailingProvider):
    def translate(self, request):
        self.calls.append(request)
        raise TranslationProviderError("google", TranslationErrorReason.CREDENTIAL_UNAVAILABLE)


class _PartialResponseProvider(FailingProvider):
    def translate(self, request):
        self.calls.append(request)
        return SimpleNamespace(
            provider=self.provider_id,
            model_version=self.model_version,
            source_language=request.source_language,
            target_language=request.target_language,
            items=(),
        )


@pytest.mark.parametrize(("source_language", "target_language"), (("en", "zh"), ("zh", "en")))
def test_provider_and_store_failure_preserve_original_projection(
    source_language, target_language
) -> None:
    original = _item(source_language=source_language)
    before = _original_projection(original)
    ranked = {source_language: {"AI": [original]}, target_language: {"AI": []}}
    provider_result = produce_translation_records(
        cfg=_cfg(target_language=target_language),
        ranked_by_language=ranked,
        store=InMemoryTranslationStore(clock=lambda: NOW),
        provider=FailingProvider(),
        now=NOW,
        run_id="run:provider-failure",
    )
    store_result = produce_translation_records(
        cfg=_cfg(target_language=target_language),
        ranked_by_language=ranked,
        store=FailingAcquireStore(clock=lambda: NOW),
        provider=FailingProvider(),
        now=NOW,
        run_id="run:store-failure",
    )

    assert provider_result.records == store_result.records == ()
    assert provider_result.counters["provider_failed"] == 1
    assert store_result.counters["acquire_failed"] == 1
    after = _original_projection(original)
    assert [(row.story_id, row.title, row.description) for row in after] == [
        (story_id_for_item(original), before[0].title, before[0].description)
    ]


@pytest.mark.parametrize(
    ("fault", "expected_counter"),
    (
        ("timeout", "provider_failed"),
        ("partial_response", "provider_contract_failed"),
        ("missing_credential", "provider_failed"),
        ("budget_exhaustion", "budget_exhausted"),
        ("database_outage", "acquire_failed"),
    ),
)
@pytest.mark.parametrize(("source_language", "target_language"), (("en", "zh"), ("zh", "en")))
def test_fault_matrix_keeps_authoritative_originals_unchanged(
    fault, expected_counter, source_language, target_language
) -> None:
    original = _item(source_language=source_language)
    newsletter = _item(source_language=source_language, newsletter=True)
    baseline = _original_projection(original)
    cfg = _cfg(target_language=target_language)
    provider = {
        "timeout": _TimeoutProvider(),
        "partial_response": _PartialResponseProvider(),
        "missing_credential": _MissingCredentialProvider(),
        "budget_exhaustion": FailingProvider(),
        "database_outage": FailingProvider(),
    }[fault]
    store = (
        FailingAcquireStore(clock=lambda: NOW)
        if fault == "database_outage"
        else InMemoryTranslationStore(clock=lambda: NOW)
    )
    if fault == "budget_exhaustion":
        cfg.translation["run_character_limit"] = 1
        cfg.translation["day_character_limit"] = 1
        cfg.translation["month_character_limit"] = 1

    result = produce_translation_records(
        cfg=cfg,
        ranked_by_language={
            source_language: {"AI": [original, newsletter]},
            target_language: {"AI": []},
        },
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:" + fault.replace("_", "-"),
    )

    assert result.records == ()
    assert result.counters[expected_counter] == 1
    if provider.calls:
        assert len(provider.calls) == 1
        assert provider.calls[0].items[0].content.title == original.title
        assert "newsletter" not in provider.calls[0].items[0].content.title
    assert len(getattr(store, "_reservations", {})) <= 1
    after = _original_projection(original)
    assert [(row.story_id, row.title, row.description) for row in after] == [
        (baseline[0].story_id, baseline[0].title, baseline[0].description)
    ]


@pytest.mark.parametrize("source_language", ("en", "zh"))
def test_malformed_artifact_falls_back_to_same_nonempty_original_projection(
    tmp_path, source_language
) -> None:
    original = _item(source_language=source_language)
    baseline = _original_projection(original)
    artifact = tmp_path / "translations.json"
    artifact.write_text(json.dumps({"schema_version": 1, "translations": "not-a-list"}))

    try:
        translations = load_translation_artifact(artifact)
    except TranslationArtifactError:
        translations = ()
    after = build_localized_view(
        target_language=source_language,
        native_ranked={"AI": [original]},
        source_ranked={"AI": []},
        translations=translations,
    )["AI"]

    assert [(row.story_id, row.title, row.description) for row in after] == [
        (baseline[0].story_id, baseline[0].title, baseline[0].description)
    ]


@pytest.mark.parametrize("source_language", ("en", "zh"))
def test_missing_artifact_falls_back_to_same_nonempty_original_projection(
    tmp_path, source_language
) -> None:
    original = _item(source_language=source_language)
    baseline = _original_projection(original)
    artifact = tmp_path / "missing-translations.json"

    try:
        translations = load_translation_artifact(artifact)
    except TranslationArtifactError:
        translations = ()
    after = build_localized_view(
        target_language=source_language,
        native_ranked={"AI": [original]},
        source_ranked={"AI": []},
        translations=translations,
    )["AI"]

    assert [(row.story_id, row.title, row.description) for row in after] == [
        (baseline[0].story_id, baseline[0].title, baseline[0].description)
    ]
