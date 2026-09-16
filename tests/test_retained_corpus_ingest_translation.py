"""The hourly ingest must translate the language-exclusive stories, or say why not.

Nothing here touches the network: the provider and the cache store are stubs,
and the "no key configured" path must be a clean skip, never a broken ingest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from curator.models import Item
from curator.retained_corpus import retain
from curator.translation import InMemoryTranslationStore
from curator.translation.base import TranslationProviderResult, TranslationResultItem
from scripts.retained_corpus_ingest import translate_rows

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
KEY_ENV = "NEWS_CURATOR_MODEL_API_KEY"


@dataclass
class FakeConfig:
    translation: dict
    language: dict


def config(**overrides):
    translation = {"enabled": True, "provider": "openai", "model": "gpt-5-mini",
                   "api_key_env": KEY_ENV, "run_character_limit": 2000,
                   "day_character_limit": 15000, "month_character_limit": 450000,
                   "daily_cost_limit_usd": 0.5, "cost_per_1k_characters_usd": 0.002,
                   "cache_ttl_days": 30, "on_failure": "show_original_marked",
                   "max_items_per_language": 25, "normalization_version": "normalized-item-v1",
                   "glossary_policy_version": "none-v1", "candidate_policy_version": "ranked-non-newsletter-v1"}
    translation.update(overrides)
    return FakeConfig(translation=translation,
                      language={"default_display": "en", "other_lane_enabled": True,
                                "exclusive_category_id": "only-other-language-press"})


class StubProvider:
    provider_id = "openai"
    model_version = "gpt-5-mini:translation-json-v1"

    def __init__(self):
        self.calls = 0

    def translate(self, request):
        self.calls += 1
        item = request.items[0]
        return TranslationProviderResult(
            items=(TranslationResultItem(request_id=item.request_id, title="Exclusive: seven new rules",
                                         description="An English summary."),),
            source_language=request.source_language, target_language=request.target_language,
            provider=self.provider_id, model_version=self.model_version)


def fixture_rows():
    english = Item(title="Nvidia beats on earnings with 3 Blackwell chips", url="https://e.com/1",
                   canonical_url="https://e.com/1", source_id="fixture", source_name="Fixture",
                   published_at=NOW, language="en", description="")
    paired = Item(title="英伟达 Nvidia 发布 3 款 Blackwell 芯片 earnings", url="https://e.cn/1",
                  canonical_url="https://e.cn/1", source_id="fixture", source_name="Fixture",
                  published_at=NOW, language="zh", description="")
    exclusive = Item(title="独家：某部门发布七项新规", url="https://e.cn/2", canonical_url="https://e.cn/2",
                     source_id="fixture", source_name="Fixture", published_at=NOW, language="zh",
                     description="中文摘要内容。")
    return retain([english, paired, exclusive], categories=[], observed_at=NOW)


def test_only_the_language_exclusive_story_is_translated():
    provider = StubProvider()
    rows, message = translate_rows(config(), fixture_rows(), env={KEY_ENV: "test-key"}, now=NOW,
                                   store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider)
    by_url = {row.item.canonical_url: row for row in rows}
    assert provider.calls == 1
    assert by_url["https://e.cn/2"].title_translations == {"en": "Exclusive: seven new rules"}
    # The zh story an English outlet also covered is not exclusive, so it is
    # not translated, and the English story is never a candidate at all.
    assert by_url["https://e.cn/1"].title_translations == {}
    assert by_url["https://e.com/1"].title_translations == {}
    assert "translated=1" in message


def test_a_missing_key_skips_cleanly_and_never_breaks_the_ingest():
    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=StubProvider())
    assert message == "translation skipped: key not configured"
    assert result == rows


def test_the_feature_switch_off_is_reported_and_changes_nothing():
    rows = fixture_rows()
    result, message = translate_rows(config(enabled=False), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=StubProvider())
    assert message == "translation skipped: disabled in config"
    assert result == rows


def test_a_translation_failure_degrades_and_never_drops_a_row():
    class Broken:
        provider_id = "openai"
        model_version = "gpt-5-mini:translation-json-v1"

        def translate(self, request):
            raise RuntimeError("provider exploded")

    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=Broken())
    assert len(result) == len(rows)
    assert all(row.title_translations == {} for row in result)
    assert "untranslated_shown=1" in message


def test_an_unexpected_failure_inside_translation_leaves_the_ingest_intact():
    class Exploding:
        provider_id = "openai"
        model_version = "gpt-5-mini:translation-json-v1"

        def translate(self, request):
            raise RuntimeError("unreachable")

    class BrokenStore:
        def lookup(self, key):
            raise RuntimeError("store down")

        def recover_stale(self, key, **kwargs):
            raise RuntimeError("store down")

        def acquire(self, request):
            raise RuntimeError("store down")

    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=BrokenStore(), provider=Exploding())
    assert result == rows or len(result) == len(rows)
    assert "untranslated_shown" in message or message.startswith("translation unavailable")
