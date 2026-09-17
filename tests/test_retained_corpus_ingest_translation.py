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
from curator.translation import (
    InMemoryTranslationStore,
    StoreErrorReason,
    TranslationErrorReason,
    TranslationProviderError,
    TranslationStoreError,
)
from curator.translation.base import TranslationProviderResult, TranslationResultItem
from curator.grouping import GroupingCandidate, event_group_id_for
from curator.translation.pairing import ExclusivityDecision
from scripts.retained_corpus_ingest import translate_rows

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
KEY_ENV = "NEWS_CURATOR_MODEL_API_KEY"


@dataclass
class FakeConfig:
    translation: dict
    language: dict
    grouping: dict = None


class StubPairing:
    """Stands in for the model. `answers` maps story_id -> match story id or None."""

    provider_id = "openai"
    model_version = "gpt-5-mini:pairing-json-v1"

    def __init__(self, answers=None, default=None):
        self.answers, self.default, self.asked = dict(answers or {}), default, []

    def decide(self, *, story, context):
        self.asked.append(story.story_id)
        target = self.answers.get(story.story_id, self.default)
        if target is None:
            return None, 500, 6
        for index, row in enumerate(context):
            if row.story_id == target:
                return index, 500, 6
        return None, 500, 6


def config(**overrides):
    translation = {"enabled": True, "provider": "openai", "model": "gpt-5-mini",
                   "pairing_window_hours": 48, "pairing_max_context_titles": 60,
                   "pairing_daily_call_limit": 600,
                   "api_key_env": KEY_ENV, "run_character_limit": 2000,
                   "day_character_limit": 15000, "month_character_limit": 450000,
                   "daily_cost_limit_usd": 0.5, "cost_per_1k_characters_usd": 0.002,
                   "cache_ttl_days": 30, "on_failure": "show_original_marked",
                   "max_items_per_language": 25, "normalization_version": "normalized-item-v1",
                   "glossary_policy_version": "none-v1", "candidate_policy_version": "ranked-non-newsletter-v1"}
    translation.update(overrides)
    return FakeConfig(translation=translation,
                      language={"default_display": "en", "other_lane_enabled": True,
                                "exclusive_category_id": "only-other-language-press"},
                      grouping={"cross_language_enabled": True})


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


def _ids(rows):
    """story_id by canonical url, so tests can name the fixture stories."""
    return {row.item.canonical_url: row.story_id for row in rows}


def pairing_for(rows):
    """The model's answers: the paired story matches the English one, the
    exclusive story matches nothing."""
    ids = _ids(rows)
    return StubPairing(answers={ids["https://e.cn/1"]: ids["https://e.com/1"],
                                ids["https://e.cn/2"]: None})


def test_only_the_language_exclusive_story_is_translated():
    batch = fixture_rows()
    provider = StubProvider()
    rows, message = translate_rows(config(), batch, env={KEY_ENV: "test-key"}, now=NOW,
                                   store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider,
                                   pairing_provider=pairing_for(batch))
    by_url = {row.item.canonical_url: row for row in rows}
    assert provider.calls == 1
    assert by_url["https://e.cn/2"].title_translations == {"en": "Exclusive: seven new rules"}
    # The zh story the model matched to an English one is not exclusive, so it
    # is not translated, and it joins that story's group.
    assert by_url["https://e.cn/1"].title_translations == {}
    assert by_url["https://e.cn/1"].event_group_id == event_group_id_for(_ids(batch)["https://e.com/1"])
    assert by_url["https://e.com/1"].title_translations == {}
    assert "translated=1" in message


def test_a_missing_key_skips_cleanly_and_never_breaks_the_ingest():
    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=StubProvider(), pairing_provider=StubPairing())
    assert message == "translation skipped: NEWS_CURATOR_MODEL_API_KEY is not set"
    assert result == rows


def test_the_feature_switch_off_is_reported_and_changes_nothing():
    rows = fixture_rows()
    result, message = translate_rows(config(enabled=False), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=StubProvider(), pairing_provider=StubPairing())
    assert message == "translation skipped: sources.yaml translation.enabled is false"
    assert result == rows


def test_a_translation_failure_degrades_and_never_drops_a_row():
    class Broken:
        provider_id = "openai"
        model_version = "gpt-5-mini:translation-json-v1"

        def translate(self, request):
            raise TranslationProviderError(self.provider_id, TranslationErrorReason.PROVIDER_REJECTED)

    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=Broken(),
                                     pairing_provider=pairing_for(rows))
    assert len(result) == len(rows)
    assert all(row.title_translations == {} for row in result)
    assert "untranslated_shown=1" in message


def test_a_store_outage_degrades_with_a_named_reason_and_keeps_every_row():
    """A store that is DOWN (its own error type) degrades. A store that is
    BROKEN (a TypeError) surfaces, and that case is asserted separately."""
    rows = fixture_rows()

    class DownStore:
        def lookup(self, key):
            raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)

        def recover_stale(self, key, **kwargs):
            raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)

        def acquire(self, request):
            raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)

    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=DownStore(), provider=StubProvider(),
                                     pairing_provider=pairing_for(rows))
    assert len(result) == len(rows), "no story is dropped for a store outage"
    assert all(row.title_translations == {} for row in result)
    assert "untranslated_shown=1" in message


def test_the_shipped_config_translates_language_exclusive_stories_with_a_key():
    """Phase 1 acceptance, against sources.yaml as shipped, with a stub provider."""
    from pathlib import Path
    from curator.config import load_config
    cfg = load_config(Path(__file__).resolve().parents[1])
    assert cfg.translation["enabled"] is True, "Phase 1 ships enabled"
    batch = fixture_rows()
    provider = StubProvider()
    rows, message = translate_rows(cfg, batch, env={cfg.translation["api_key_env"]: "test-key"},
                                   now=NOW, store=InMemoryTranslationStore(clock=lambda: NOW),
                                   provider=provider, pairing_provider=pairing_for(batch))
    translated = [row for row in rows if row.title_translations]
    assert provider.calls == 1 and len(translated) == 1
    assert translated[0].item.language == "zh"
    assert translated[0].title_translations == {"en": "Exclusive: seven new rules"}
    assert "translated=1" in message


def _corpus_row(row, *, group=None):
    """A row as the corpus read-back returns it, group id included."""
    return GroupingCandidate(story_id=row.story_id, language=row.item.language, title=row.item.title,
                             summary=row.item.description or "", published_at=row.item.published_at,
                             canonical_url=row.item.canonical_url,
                             category_ids=tuple(sorted(row.category_ids)), event_group_id=group)


def test_run_n_then_run_n_plus_1_the_chinese_story_is_not_exclusive():
    """The real sequence: the English story arrived in an EARLIER run, and the
    database stored it with a NULL group id. The Chinese story shows up alone in
    the next run and must still not be sold as 'Only in Chinese press'."""
    english = Item(title="Nvidia beats on earnings with 3 Blackwell chips", url="https://e.com/1",
                   canonical_url="https://e.com/1", source_id="fixture", source_name="Fixture",
                   published_at=NOW, language="en", description="Revenue rose in 2026.")
    chinese = Item(title="英伟达 Nvidia 发布 3 款 Blackwell 芯片", url="https://e.cn/1",
                   canonical_url="https://e.cn/1", source_id="fixture", source_name="Fixture",
                   published_at=NOW, language="zh", description="2026 年营收增长。")
    run_n = retain([english], categories=[], observed_at=NOW)
    assert run_n[0].event_group_id is None, "a group of one has no id in the database"
    run_n1 = retain([chinese], categories=[], observed_at=NOW)
    # Exactly what the read-back yields: the English row, group id NULL.
    corpus = (_corpus_row(run_n[0], group=None),)
    provider = StubProvider()
    rows, message = translate_rows(config(), run_n1, env={KEY_ENV: "test-key"}, now=NOW,
                                   store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider,
                                   pairing_provider=StubPairing(answers={run_n1[0].story_id: run_n[0].story_id}),
                                   corpus=corpus)
    assert provider.calls == 0, "a covered story is never paid for"
    assert rows[0].event_group_id == event_group_id_for(run_n[0].story_id)
    assert "no language-exclusive stories" in message


def test_a_decision_made_in_an_earlier_run_is_reused_and_never_re_asked():
    rows = fixture_rows()
    zh_alone = next(row for row in rows if row.item.canonical_url == "https://e.cn/2")
    zh_paired = next(row for row in rows if row.item.canonical_url == "https://e.cn/1")
    english = next(row for row in rows if row.item.canonical_url == "https://e.com/1")
    decided = {
        zh_alone.story_id: ExclusivityDecision(story_id=zh_alone.story_id, decided_at=NOW,
                                               model="gpt-5-mini", policy_id="pairing-json-v1",
                                               match_story_id=None, outcome="exclusive"),
        zh_paired.story_id: ExclusivityDecision(story_id=zh_paired.story_id, decided_at=NOW,
                                                model="gpt-5-mini", policy_id="pairing-json-v1",
                                                match_story_id=english.story_id, outcome="matched"),
    }
    pairing = StubPairing()
    provider = StubProvider()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider,
                                     pairing_provider=pairing, decisions=decided)
    assert pairing.asked == [], "a decided story is never re-asked"
    assert provider.calls == 1 and "pairing_calls=0" in message
    by_url = {row.item.canonical_url: row for row in result}
    assert by_url["https://e.cn/2"].title_translations == {"en": "Exclusive: seven new rules"}
    assert by_url["https://e.cn/1"].event_group_id == event_group_id_for(english.story_id)


def test_a_truncated_read_back_skips_pairing_and_translation_entirely():
    """Exclusivity is unproven when the window was not fully read, and a wrong
    claim spends money on a story an English outlet already ran."""
    rows = fixture_rows()
    provider, pairing = StubProvider(), StubPairing()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider,
                                     pairing_provider=pairing, truncated=True)
    assert message == "translation skipped: corpus read-back truncated"
    assert provider.calls == 0 and pairing.asked == []
    assert result == rows


def test_an_undecided_story_is_neither_translated_nor_claimed_exclusive():
    rows = fixture_rows()

    class Refusing:
        provider_id = "openai"
        model_version = "gpt-5-mini:pairing-json-v1"

        def decide(self, *, story, context):
            raise TranslationProviderError(self.provider_id, TranslationErrorReason.TRANSPORT_FAILURE)

    provider = StubProvider()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW), provider=provider,
                                     pairing_provider=Refusing())
    assert provider.calls == 0
    assert "undecided=2" in message
    assert all(row.title_translations == {} for row in result)


def test_a_missing_key_emits_an_actions_warning_and_still_exits_zero(capsys):
    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW),
                                     provider=StubProvider(), pairing_provider=StubPairing())
    captured = capsys.readouterr()
    assert "::warning::translation skipped: NEWS_CURATOR_MODEL_API_KEY not configured" in captured.err
    assert message == "translation skipped: NEWS_CURATOR_MODEL_API_KEY is not set"
    assert result == rows


def test_the_decision_callback_receives_every_new_decision_for_persistence():
    rows = fixture_rows()
    recorded = []
    translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                   store=InMemoryTranslationStore(clock=lambda: NOW), provider=StubProvider(),
                   pairing_provider=pairing_for(rows), on_decision=recorded.append)
    assert {decision.story_id for decision in recorded} == {
        row.story_id for row in rows if row.item.language == "zh"}
    assert {decision.policy_id for decision in recorded} == {"pairing-json-v1"}


def test_the_two_supabase_key_env_names_are_deliberate_and_documented():
    """The ingest RPCs and the translation store use DIFFERENT key names.

    Both are service-role credentials, but they are provisioned separately, so
    the split is recorded here rather than left as a trap for the next reader.
    """
    from pathlib import Path
    from curator.config import load_config
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root)
    script = (root / "scripts/retained_corpus_ingest.py").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/retained-corpus-ingest.yml").read_text(encoding="utf-8")
    # The corpus read and ingest RPCs use the ingest key.
    assert "NEWS_CURATOR_SUPABASE_SECRET_KEY" in script
    # The translation cache/budget store uses the service-role key from config.
    assert cfg.translation["supabase_service_role_key_env"] == "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY"
    # Both must be present in the job, or one half of the feature is dark.
    for name in ("NEWS_CURATOR_SUPABASE_SECRET_KEY", "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY"):
        assert name in workflow, name


def test_a_programmer_error_is_not_swallowed_as_a_provider_outage():
    """Round 2 hid code defects behind 'original text retained'. It must not."""
    rows = fixture_rows()

    class BrokenStore:
        def lookup(self, key):
            raise TypeError("lookup() got an unexpected keyword argument")

        def recover_stale(self, key, **kwargs):
            raise TypeError("programmer error")

        def acquire(self, request):
            raise TypeError("programmer error")

    with pytest.raises(TypeError):
        translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW, store=BrokenStore(),
                       provider=StubProvider(), pairing_provider=pairing_for(rows))


def test_a_503_from_the_spend_ledger_degrades_instead_of_killing_the_ingest():
    """The reviewer reproduced this escaping translate_rows and exiting 2, which
    loses every retained row for the run, not just the translations."""
    import urllib.error

    class FailingLedger:
        def reserve(self, amount_usd):
            raise urllib.error.HTTPError("https://db.test/rpc", 503, "Service Unavailable", {}, None)

        def settle(self, reserved_usd, settled_usd):
            raise AssertionError("never reached")

        def retain(self, reserved_usd):
            return None

        def release(self, reserved_usd):
            return None

    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW),
                                     provider=StubProvider(), pairing_provider=pairing_for(rows),
                                     spend_ledger=FailingLedger())
    assert "translation unavailable" in message and "HTTPError" in message
    assert len(result) == len(rows), "every retained row survives the outage"


def test_a_503_from_the_pairing_ledger_degrades_too():
    import urllib.error

    class FailingPairingLedger:
        def reserve_call(self, amount_usd):
            raise urllib.error.HTTPError("https://db.test/rpc", 503, "Service Unavailable", {}, None)

        def settle_call(self, reserved_usd, settled_usd):
            return None

    rows = fixture_rows()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW),
                                     provider=StubProvider(), pairing_provider=pairing_for(rows),
                                     pairing_ledger=FailingPairingLedger())
    assert "pairing unavailable" in message
    assert len(result) == len(rows)


def test_a_decision_that_cannot_be_persisted_does_not_drive_translation():
    rows = fixture_rows()

    def failing_persist(decision):
        raise ConnectionError("supabase unreachable")

    provider = StubProvider()
    result, message = translate_rows(config(), rows, env={KEY_ENV: "test-key"}, now=NOW,
                                     store=InMemoryTranslationStore(clock=lambda: NOW),
                                     provider=provider, pairing_provider=pairing_for(rows),
                                     persist_decision=failing_persist)
    assert provider.calls == 0, "an unpersisted decision buys nothing"
    assert "no language-exclusive stories" in message
    assert all(row.title_translations == {} for row in result)


def test_a_matched_pair_puts_the_group_id_on_both_rows_before_ingest():
    """The English peer kept NULL before, which is what made the matched story
    come back from the exclusive lane."""
    batch = fixture_rows()
    ids = _ids(batch)
    rows, _ = translate_rows(config(), batch, env={KEY_ENV: "test-key"}, now=NOW,
                             store=InMemoryTranslationStore(clock=lambda: NOW),
                             provider=StubProvider(), pairing_provider=pairing_for(batch))
    by_url = {row.item.canonical_url: row for row in rows}
    group = event_group_id_for(ids["https://e.com/1"])
    assert by_url["https://e.cn/1"].event_group_id == group
    assert by_url["https://e.com/1"].event_group_id == group, "the English peer carries it too"
