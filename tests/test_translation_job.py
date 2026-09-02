"""Translation orchestration state, privacy, and fail-soft tests."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from curator.config import Category, Config
from curator.models import Item
from curator.translation import (
    AcquireResult,
    AcquireStatus,
    InMemoryTranslationStore,
    Reservation,
    ReservationState,
    TranslationCacheRecord,
    TranslationCandidatePolicy,
    TranslationInput,
    TranslationProviderResult,
    TranslationResultItem,
)
import scripts.run_translation_job as translation_job
from scripts.run_translation_job import main, produce_translation_records


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]
MODEL_RESOURCE = "projects/valid-project-123/locations/global/models/general/nmt"


def config(*, enabled=True):
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
            "targets": ["zh"],
            "max_items_per_language": 5,
            "max_characters_per_language": 1000,
            "run_character_limit": 1000,
            "day_character_limit": 2000,
            "month_character_limit": 3000,
            "normalization_version": "normalized-item-v1",
            "glossary_policy_version": "none-v1",
            "candidate_policy_version": "ranked-v1",
            "model_version": "projects/{project_id}/locations/{location}/models/general/nmt",
        },
    )


def item(*, newsletter=False, language="en"):
    title = "AI story" if language == "en" else "人工智能新闻"
    description = "Publisher summary" if language == "en" else "来源摘要"
    return Item(
        title=title,
        description=description,
        url=f"https://example.com/{language}/story",
        canonical_url=f"https://example.com/{language}/story",
        source_id="publisher",
        source_name="Publisher",
        published_at=NOW,
        language=language,
        is_newsletter=newsletter,
    )


class Provider:
    provider_id = "google"
    model_version = MODEL_RESOURCE

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def translate(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("provider detail must not escape")
        return TranslationProviderResult(
            items=(
                TranslationResultItem(
                    request_id=request.items[0].request_id,
                    title="人工智能报道",
                    description="出版商摘要",
                ),
            ),
            source_language="en",
            target_language="zh",
            provider="google",
            model_version=self.model_version,
        )


def test_success_marks_sent_before_provider_settles_and_reuses_cache():
    store = InMemoryTranslationStore(clock=lambda: NOW)
    provider = Provider()
    ranked = {"en": {"AI": [item()]}, "zh": {"AI": []}}

    first = produce_translation_records(
        cfg=config(), ranked_by_language=ranked, store=store, provider=provider,
        now=NOW, run_id="run:1",
    )
    second = produce_translation_records(
        cfg=config(), ranked_by_language=ranked, store=store, provider=provider,
        now=NOW, run_id="run:2",
    )

    assert first.records == second.records
    assert first.records[0].title == "人工智能报道"
    assert first.counters["translated"] == 1
    assert second.counters["cache_hit"] == 1
    assert len(provider.calls) == 1
    assert list(store._reservations.values())[0].state == ReservationState.SETTLED


def test_cross_wired_cache_reply_is_quarantined_without_emitting_or_calling_provider():
    class CrossWiredCacheStore(InMemoryTranslationStore):
        def __init__(self):
            super().__init__(clock=lambda: NOW)
            self.quarantined = []

        def acquire(self, request):
            foreign_key = replace(request.key, story_id="story:foreign")
            return AcquireResult(
                AcquireStatus.CACHE_HIT,
                cache=TranslationCacheRecord(
                    key=foreign_key,
                    translated_title="wrong story",
                    actual_characters=10,
                ),
            )

        def quarantine(self, key, *, reason_code):
            self.quarantined.append((key, reason_code))

    store = CrossWiredCacheStore()
    provider = Provider()
    result = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:1",
    )

    assert result.records == ()
    assert result.counters["cache_invalid"] == 1
    assert provider.calls == []
    assert len(store.quarantined) == 1
    assert store.quarantined[0][1] == "response_correlation"


def test_cross_wired_lease_reply_is_rejected_before_mark_sent_or_provider():
    class CrossWiredLeaseStore:
        def __init__(self):
            self.mark_sent_calls = []

        def recover_stale(self, *_args, **_kwargs):
            return None

        def acquire(self, request):
            foreign = replace(request, run_id="run:foreign")
            return AcquireResult(
                AcquireStatus.LEASED,
                reservation=Reservation(
                    request=foreign,
                    state=ReservationState.LEASED,
                    counter_day="2026-08-30",
                    counter_month="2026-08",
                ),
            )

        def mark_sent(self, idempotency_key):
            self.mark_sent_calls.append(idempotency_key)
            raise AssertionError("mark_sent must not run for a cross-wired lease")

    store = CrossWiredLeaseStore()
    provider = Provider()
    result = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:1",
    )

    assert result.records == ()
    assert result.counters["acquire_failed"] == 1
    assert store.mark_sent_calls == []
    assert provider.calls == []


def test_shared_run_budget_round_robins_both_translation_directions():
    class BidirectionalProvider(Provider):
        def translate(self, request):
            self.calls.append(request)
            title = "Translated story" if request.target_language == "en" else "翻译新闻"
            description = "Translated summary" if request.target_language == "en" else "翻译摘要"
            return TranslationProviderResult(
                items=(TranslationResultItem(request.items[0].request_id, title, description),),
                source_language=request.source_language,
                target_language=request.target_language,
                provider="google",
                model_version=self.model_version,
            )

    def directional_item(language: str, index: int) -> Item:
        title = f"AI story {index}" if language == "en" else f"中文新闻{index}"
        description = "Summary" if language == "en" else "中文摘要"
        return Item(
            title=title,
            description=description,
            url=f"https://example.com/{language}/{index}",
            canonical_url=f"https://example.com/{language}/{index}",
            source_id=f"publisher-{language}",
            source_name=f"Publisher {language}",
            published_at=NOW,
            language=language,
        )

    cfg = config()
    cfg.translation["targets"] = ["en", "zh"]
    first_en = directional_item("en", 1)
    first_zh = directional_item("zh", 1)
    per_direction_pair = (
        len(first_en.title)
        + len(first_en.description)
        + len(first_zh.title)
        + len(first_zh.description)
    )
    cfg.translation["run_character_limit"] = per_direction_pair
    cfg.translation["day_character_limit"] = per_direction_pair
    cfg.translation["month_character_limit"] = per_direction_pair
    ranked = {
        "en": {"AI": [first_en, directional_item("en", 2), directional_item("en", 3)]},
        "zh": {"AI": [first_zh, directional_item("zh", 2), directional_item("zh", 3)]},
    }
    store = InMemoryTranslationStore(clock=lambda: NOW)
    provider = BidirectionalProvider()

    result = produce_translation_records(
        cfg=cfg,
        ranked_by_language=ranked,
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:fair-directions",
    )

    assert [(call.source_language, call.target_language) for call in provider.calls] == [
        ("zh", "en"),
        ("en", "zh"),
    ]
    assert {record.target_language for record in result.records} == {"en", "zh"}
    assert result.counters["translated"] == 2
    assert result.counters["budget_exhausted"] == 4
    assert store.counter_snapshot(run_id="run:fair-directions")["run"] == per_direction_pair


def test_translation_candidates_interleave_category_rank_before_the_cap() -> None:
    categories = {
        "AI": [
            replace(item(), title="AI first", canonical_url="https://example.com/ai/1"),
            replace(item(), title="AI second", canonical_url="https://example.com/ai/2"),
        ],
        "Crypto": [
            replace(item(), title="Crypto first", canonical_url="https://example.com/crypto/1"),
            replace(item(), title="Crypto second", canonical_url="https://example.com/crypto/2"),
        ],
    }

    assert [row.title for row in translation_job._unique_ranked(categories)] == [
        "AI first",
        "Crypto first",
        "AI second",
        "Crypto second",
    ]


def test_translation_candidate_digest_keeps_the_first_ranked_story() -> None:
    first = replace(item(), canonical_url="https://example.com/first", url="https://example.com/first")
    second = replace(item(), canonical_url="https://example.com/second", url="https://example.com/second")

    streams = translation_job._translation_candidate_streams(
        targets=("zh",),
        ranked_by_language={"en": {"AI": [first, second]}, "zh": {"AI": []}},
        candidate_policy=TranslationCandidatePolicy(),
    )

    _, _, by_digest, candidates = streams[0]
    assert len(candidates) == 1
    assert by_digest[TranslationInput.from_item(first).digest] is first


def test_any_provider_failure_after_mark_sent_becomes_charge_unknown():
    store = InMemoryTranslationStore(clock=lambda: NOW)
    provider = Provider(fail=True)

    records = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:1",
    )

    assert records.records == ()
    assert records.counters["provider_failed"] == 1
    assert records.counters["charge_unknown"] == 1
    assert list(store._reservations.values())[0].state == ReservationState.CHARGE_UNKNOWN


@pytest.mark.parametrize(("source_language", "target_language"), (("en", "zh"), ("zh", "en")))
def test_newsletter_is_filtered_before_candidate_selection_or_provider(
    source_language, target_language
):
    store = InMemoryTranslationStore(clock=lambda: NOW)
    provider = Provider()
    cfg = config()
    cfg.translation["targets"] = [target_language]

    records = produce_translation_records(
        cfg=cfg,
        ranked_by_language={
            source_language: {"AI": [item(newsletter=True, language=source_language)]},
            target_language: {"AI": []},
        },
        store=store,
        provider=provider,
        now=NOW,
        run_id="run:1",
    )

    assert records.records == ()
    assert provider.calls == []
    assert store._reservations == {}


def test_wrong_provider_language_is_rejected_before_settlement():
    class WrongLanguageProvider(Provider):
        def translate(self, request):
            result = super().translate(request)
            return TranslationProviderResult(
                items=result.items,
                source_language="zh",
                target_language="en",
                provider=result.provider,
                model_version=result.model_version,
            )

    store = InMemoryTranslationStore(clock=lambda: NOW)
    result = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=WrongLanguageProvider(),
        now=NOW,
        run_id="run:1",
    )

    assert result.records == ()
    assert result.counters["provider_contract_failed"] == 1
    assert list(store._reservations.values())[0].state == ReservationState.CHARGE_UNKNOWN


def test_oversized_provider_output_never_settles():
    class OversizedProvider(Provider):
        def translate(self, request):
            return TranslationProviderResult(
                items=(TranslationResultItem(request.items[0].request_id, "字" * 41, ""),),
                source_language="en",
                target_language="zh",
                provider="google",
                model_version=self.model_version,
            )

    cfg = config()
    cfg.translation["max_output_title_characters"] = 40
    cfg.translation["max_output_description_characters"] = 80
    store = InMemoryTranslationStore(clock=lambda: NOW)
    result = produce_translation_records(
        cfg=cfg,
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=OversizedProvider(),
        now=NOW,
        run_id="run:1",
    )

    assert result.records == ()
    assert result.counters["provider_contract_failed"] == 1
    assert store.lookup(next(iter(store._reservations.values())).request.key) is None


def test_lost_mark_sent_response_is_recovered_to_charge_unknown():
    class LostResponseStore(InMemoryTranslationStore):
        def mark_sent(self, idempotency_key):
            super().mark_sent(idempotency_key)
            raise RuntimeError("lost response")

    store = LostResponseStore(clock=lambda: NOW)
    result = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=store,
        provider=Provider(),
        now=NOW,
        run_id="run:1",
    )

    assert result.fatal_persistence_failure is False
    assert result.counters["charge_unknown"] == 1
    assert list(store._reservations.values())[0].state == ReservationState.CHARGE_UNKNOWN


def test_unrecordable_ambiguous_paid_state_is_fatal_to_translation_job_only():
    class BrokenPersistenceStore(InMemoryTranslationStore):
        def mark_sent(self, idempotency_key):
            super().mark_sent(idempotency_key)
            raise RuntimeError("lost response")

        def mark_charge_unknown(self, idempotency_key):
            raise RuntimeError("unavailable")

        def mark_failed_before_send(self, idempotency_key):
            raise RuntimeError("unavailable")

    result = produce_translation_records(
        cfg=config(),
        ranked_by_language={"en": {"AI": [item()]}, "zh": {"AI": []}},
        store=BrokenPersistenceStore(clock=lambda: NOW),
        provider=Provider(),
        now=NOW,
        run_id="run:1",
    )

    assert result.records == ()
    assert result.fatal_persistence_failure is True
    assert result.counters["persistence_unknown"] == 1


def test_disabled_cli_writes_valid_empty_artifact_without_credentials(tmp_path):
    (tmp_path / "topics.yaml").write_text(
        "categories:\n  - {id: ai, name: AI, keywords: [AI]}\n", encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text(
        "hackernews: {enabled: false}\ntranslation: {enabled: false}\nrss: []\n",
        encoding="utf-8",
    )
    output = tmp_path / "translation.json"

    code = main(
        [
            "--root", str(tmp_path),
            "--output", str(output),
            "--google-access-token-file", str(tmp_path / "missing"),
        ]
    )

    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["translations"] == []


def test_workflow_direct_script_entrypoint_imports_repo_package():
    result = subprocess.run(
        [sys.executable, "scripts/run_translation_job.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "--google-access-token-file" in result.stdout
    assert "--source-snapshot" in result.stdout


def test_default_run_id_distinguishes_github_rerun_attempts():
    first = translation_job._default_run_id(
        NOW,
        {"GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1"},
    )
    rerun = translation_job._default_run_id(
        NOW,
        {"GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2"},
    )

    assert first == "github:12345:1"
    assert rerun == "github:12345:2"
    assert first != rerun


def test_explicit_source_snapshot_is_used_without_collecting_again(monkeypatch, tmp_path):
    cfg = config()
    loaded = []
    collected = []
    monkeypatch.setattr(translation_job, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        translation_job,
        "load_source_snapshot",
        lambda path, **kwargs: loaded.append((path, kwargs))
        or SimpleNamespace(generated_at=NOW, results=()),
    )
    monkeypatch.setattr(
        translation_job,
        "collect",
        lambda _cfg: collected.append(True) or [],
    )
    output = tmp_path / "translation.json"
    snapshot_path = tmp_path / "source-snapshot.json"

    assert main(
        [
            "--root", str(tmp_path),
            "--output", str(output),
            "--google-access-token-file", str(tmp_path / "missing-token"),
            "--source-snapshot", str(snapshot_path),
        ]
    ) == 0

    assert loaded and loaded[0][0] == snapshot_path
    assert loaded[0][1]["max_age_seconds"] == 7_200
    assert loaded[0][1]["current_time"].tzinfo is not None
    assert collected == []
    assert json.loads(output.read_text(encoding="utf-8"))["translations"] == []


def test_missing_explicit_source_snapshot_fails_soft_without_collecting(monkeypatch, tmp_path):
    cfg = config()
    collected = []
    monkeypatch.setattr(translation_job, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        translation_job,
        "collect",
        lambda _cfg: collected.append(True) or [],
    )
    output = tmp_path / "translation.json"

    assert main(
        [
            "--root", str(tmp_path),
            "--output", str(output),
            "--google-access-token-file", str(tmp_path / "missing-token"),
            "--source-snapshot", str(tmp_path / "missing-snapshot.json"),
        ]
    ) == 0
    assert collected == []
    assert json.loads(output.read_text(encoding="utf-8"))["translations"] == []


def test_google_model_resource_template_is_configurable_and_bounded():
    policy = config().translation
    policy["location"] = "global"
    policy["model_version"] = (
        "projects/{project_id}/locations/{location}/models/custom-model-42"
    )
    assert translation_job._google_model_resource(policy, "valid-project-123") == (
        "projects/valid-project-123/locations/global/models/custom-model-42"
    )

    for invalid in (
        "google-nmt-v3",
        "projects/{project_id}/locations/{location}/models/../nmt",
        "projects/{project_id}/locations/{other}/models/general/nmt",
    ):
        policy["model_version"] = invalid
        with pytest.raises(ValueError, match="model resource template"):
            translation_job._google_model_resource(policy, "valid-project-123")
