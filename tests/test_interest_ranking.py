"""M1: saved interests produce a bounded, measurable ranking signal."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from curator.personalization.ranking import (
    InterestArtifactError,
    InterestProfile,
    build_interest_artifact,
    interest_score,
    load_interest_artifact,
    measure_ranking_impact,
    ranking_config_digest,
    story_key,
)
from curator.config import Config
from curator.config import Category
from curator.rank import rank_items
from tests.conftest import make_item


SNAPSHOT_DIGEST = "a" * 64
CONFIG_DIGEST = "b" * 64


def test_interest_match_is_whole_word_and_title_only() -> None:
    assert interest_score(make_item("AI agents ship a new tool"), ("AI agents",)) == 0.5
    assert interest_score(make_item("Malaria vaccine update"), ("AI",)) == 0.0
    item = make_item("Unrelated title")
    item.description = "AI agents appear only in this summary"
    assert interest_score(item, ("AI agents",)) == 0.0


def test_ascii_interests_keep_word_boundaries_in_chinese_items() -> None:
    item = make_item("China launches a new model")
    item.language = "zh"
    assert interest_score(item, ("AI",)) == 0.0

    item.title = "AI模型发布"
    assert interest_score(item, ("AI",)) == 0.5

    item.title = "人工智能模型发布"
    assert interest_score(item, ("人工智能",)) == 0.5


def test_mixed_script_interests_keep_boundaries_around_latin_segments() -> None:
    item = make_item("OpenAI 模型发布")
    item.language = "zh"
    assert interest_score(item, ("AI 模型",)) == 0.0

    item.title = "AI 模型发布"
    assert interest_score(item, ("AI 模型",)) == 0.5

    item.title = "AI模型发布"
    assert interest_score(item, ("AI 模型",)) == 0.5

    item.title = "AI 模型发布"
    assert interest_score(item, ("AI模型",)) == 0.5

    item.title = "OpenAI模型发布"
    assert interest_score(item, ("AI模型",)) == 0.0


def test_duplicate_interests_do_not_inflate_the_score() -> None:
    item = make_item("AI agents ship")
    assert interest_score(item, ("AI", "ai")) == interest_score(item, ("AI",))


def test_score_key_survives_a_headline_change_for_one_canonical_story() -> None:
    publisher = make_item("Publisher headline", "https://example.com/story")
    aggregator = make_item("Quantum networking breakthrough", "https://example.com/story")

    payload = build_interest_artifact(
        InterestProfile(revision=1, interests=("quantum networking",)),
        [publisher, aggregator],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )

    assert story_key(publisher) in payload["scores"]
    assert story_key(aggregator) == story_key(publisher)


def test_saved_interest_changes_rank_while_empty_profile_preserves_baseline(now) -> None:
    topic = type("Topic", (), {"id": "ai", "terms_for": lambda self, language: ["AI"]})()
    interested = make_item("AI agents release", "https://example.com/interested", hours_ago=8)
    baseline = make_item("AI market update", "https://example.com/baseline", hours_ago=1)
    for item in (interested, baseline):
        item.matched_keywords = ["AI"]
    cfg = {
        "recency_half_life_hours": 12.0,
        "weight_recency": 1.0,
        "weight_keyword": 0.6,
        "weight_source": 0.4,
        "weight_echo": 0.5,
        "weight_interest": 1.0,
    }

    ordinary = rank_items([interested, baseline], topic, now, cfg)
    personalized = rank_items(
        [interested, baseline],
        topic,
        now,
        cfg,
        interest_scores={story_key(interested): 1.0},
    )

    assert ordinary[0] is baseline
    assert personalized[0] is interested
    assert rank_items([interested, baseline], topic, now, cfg, interest_scores={}) == ordinary


def test_all_zero_no_match_profile_preserves_weighted_baseline_order(now) -> None:
    topic = type("Topic", (), {"id": "ai", "terms_for": lambda self, language: []})()
    weighted = make_item("Weighted", "https://example.com/weighted", hours_ago=8)
    fresh = make_item("Fresh", "https://example.com/fresh", hours_ago=1)
    weighted.source_weight = 2.0
    fresh.source_weight = 0.0
    cfg = {"weight_recency": 0.0, "weight_keyword": 0.0, "weight_source": 1.0,
           "weight_echo": 0.0, "weight_interest": 1.0}
    ordinary = rank_items([fresh, weighted], topic, now, cfg)

    assert ordinary == [weighted, fresh]
    assert rank_items(
        [fresh, weighted], topic, now, cfg,
        interest_scores={story_key(weighted): 0.0, story_key(fresh): 0.0},
    ) == ordinary
    assert rank_items(
        [fresh, weighted], topic, now, cfg,
        interest_scores={"story:" + "0" * 64: 1.0},
    ) == ordinary


def test_preference_match_is_primary_and_freshness_breaks_equal_preferences(now) -> None:
    topic = type("Topic", (), {"id": "ai", "terms_for": lambda self, language: ["AI"]})()
    old_match = make_item("AI old match", "https://example.com/old", hours_ago=24)
    fresh_match = make_item("AI fresh match", "https://example.com/fresh", hours_ago=1)
    fresh_unmatched = make_item("AI newest", "https://example.com/newest", hours_ago=0)
    for item in (old_match, fresh_match, fresh_unmatched):
        item.matched_keywords = ["AI"]
    scores = {story_key(old_match): 0.5, story_key(fresh_match): 0.5}

    assert rank_items(
        [fresh_unmatched, old_match, fresh_match], topic, now, {}, interest_scores=scores
    ) == [fresh_match, old_match, fresh_unmatched]


def test_trending_uses_preferences_when_present_and_native_rank_without_them(now) -> None:
    topic = type("Topic", (), {"id": "trending", "terms_for": lambda self, language: []})()
    native_first = make_item("Native first", "https://example.com/first", hours_ago=1)
    preferred = make_item("Preferred", "https://example.com/preferred", hours_ago=0)
    native_first.native_categories = {"trending"}
    preferred.native_categories = {"trending"}
    native_first.native_rank = 1
    preferred.native_rank = 2

    assert rank_items([preferred, native_first], topic, now, {}) == [native_first, preferred]
    assert rank_items(
        [native_first, preferred], topic, now, {}, interest_scores={story_key(preferred): 0.5}
    ) == [preferred, native_first]
    assert rank_items([preferred, native_first], topic, now, {}, interest_scores={}) == [
        native_first, preferred,
    ]


def test_ranking_config_digest_is_available() -> None:
    cfg = Config([], [], {}, {"weight_interest": 0.8}, {}, {}, {})
    assert len(ranking_config_digest(cfg)) == 64


def test_more_like_topic_signal_materializes_and_changes_next_rank(now) -> None:
    topic = Category(name="Energy", id="energy", keywords=["grid"])
    old_match = make_item("Grid storage expands", "https://example.com/grid", hours_ago=12)
    fresh_other = make_item("AI release", "https://example.com/ai", hours_ago=1)
    old_match.matched_keywords = ["grid"]
    fresh_other.matched_keywords = []
    profile = InterestProfile(
        revision=2,
        interests=(),
        topic_signals=(("energy", "more_like"),),
        more_like_topic_weight=0.8,
    )
    payload = build_interest_artifact(
        profile, [old_match, fresh_other], categories=[topic],
        source_snapshot_digest=SNAPSHOT_DIGEST, configuration_digest=CONFIG_DIGEST,
        generated_at=now,
    )

    ranked = rank_items(
        [fresh_other, old_match], topic, now, {}, interest_scores=payload["scores"]
    )

    assert payload["scores"] == {story_key(old_match): 0.8}
    assert ranked[0] is old_match


def test_aggregated_topic_adjustment_preserves_all_story_signal_weight(now) -> None:
    topic = Category(name="Energy", id="energy", keywords=["grid"])
    item = make_item("Grid storage expands", "https://example.com/grid", hours_ago=12)
    item.matched_keywords = ["grid"]

    repeated = build_interest_artifact(
        InterestProfile(
            revision=205,
            interests=(),
            topic_signals=(("energy", "more_like"),) * 3 + (("energy", "less_like"),),
            more_like_topic_weight=0.2,
        ),
        [item], categories=[topic], source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST, generated_at=now,
    )
    aggregated = build_interest_artifact(
        InterestProfile(
            revision=205,
            interests=(),
            topic_adjustments=(("energy", 2.0),),
            more_like_topic_weight=0.2,
        ),
        [item], categories=[topic], source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST, generated_at=now,
    )

    assert aggregated["scores"] == repeated["scores"] == {story_key(item): 0.4}


def test_artifact_contains_scores_and_receipt_but_not_interests_or_user_id(tmp_path) -> None:
    matching = make_item("Quantum networking breakthrough", "https://example.com/q")
    other = make_item("Space launch", "https://example.com/s")
    profile = InterestProfile(revision=7, interests=("quantum networking",))
    payload = build_interest_artifact(
        profile,
        [matching, other],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )
    path = tmp_path / "interest-ranking.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    encoded = path.read_text(encoding="utf-8")
    assert "quantum networking" not in encoded
    assert "user_id" not in encoded
    artifact = load_interest_artifact(
        path,
        expected_source_snapshot_digest=SNAPSHOT_DIGEST,
        expected_configuration_digest=CONFIG_DIGEST,
    )
    assert artifact.preference_revision == 7
    assert artifact.interest_count == 1
    assert artifact.matched_story_count == 1
    assert artifact.scores == {story_key(matching): 0.5}


def test_empty_interest_artifact_is_a_valid_no_op(tmp_path) -> None:
    payload = build_interest_artifact(
        InterestProfile(revision=8, interests=()),
        [make_item("AI agents", "https://example.com/a")],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )
    path = tmp_path / "empty-interest-ranking.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    artifact = load_interest_artifact(
        path,
        expected_source_snapshot_digest=SNAPSHOT_DIGEST,
        expected_configuration_digest=CONFIG_DIGEST,
    )

    assert artifact.interest_count == 0
    assert artifact.matched_story_count == 0
    assert artifact.scores == {}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(source_snapshot_digest="c" * 64),
        lambda value: value.update(configuration_digest="d" * 64),
        lambda value: value["scores"].update({"not-a-digest": 1.0}),
        lambda value: value["scores"].update({"e" * 64: 1.1}),
        lambda value: value.update(interest_count=0),
    ],
)
def test_artifact_fails_closed_on_wrong_binding_or_invalid_scores(tmp_path, mutation) -> None:
    item = make_item("AI agents", "https://example.com/a")
    payload = build_interest_artifact(
        InterestProfile(revision=0, interests=("AI agents",)),
        [item],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )
    mutation(payload)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InterestArtifactError):
        load_interest_artifact(
            path,
            expected_source_snapshot_digest=SNAPSHOT_DIGEST,
            expected_configuration_digest=CONFIG_DIGEST,
        )


def test_artifact_normalizes_json_value_errors(tmp_path, monkeypatch) -> None:
    path = tmp_path / "interest-ranking.json"
    path.write_text("{}", encoding="utf-8")

    def fail_to_decode(_raw: str) -> object:
        raise ValueError("decoder limit")

    monkeypatch.setattr("curator.personalization.ranking.json.loads", fail_to_decode)

    with pytest.raises(InterestArtifactError, match="artifact is invalid"):
        load_interest_artifact(
            path,
            expected_source_snapshot_digest=SNAPSHOT_DIGEST,
            expected_configuration_digest=CONFIG_DIGEST,
        )


def test_artifact_normalizes_json_recursion_errors(tmp_path, monkeypatch) -> None:
    path = tmp_path / "interest-ranking.json"
    path.write_text("{}", encoding="utf-8")

    def fail_to_decode(_raw: str) -> object:
        raise RecursionError("nested too deeply")

    monkeypatch.setattr("curator.personalization.ranking.json.loads", fail_to_decode)

    with pytest.raises(InterestArtifactError, match="artifact is invalid"):
        load_interest_artifact(
            path,
            expected_source_snapshot_digest=SNAPSHOT_DIGEST,
            expected_configuration_digest=CONFIG_DIGEST,
        )


def test_artifact_rejects_a_numeric_score_that_overflows_float(tmp_path) -> None:
    item = make_item("AI agents", "https://example.com/a")
    payload = build_interest_artifact(
        InterestProfile(revision=0, interests=("AI agents",)),
        [item],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )
    payload["scores"][story_key(item)] = 10**4000
    path = tmp_path / "overflowing-score.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InterestArtifactError, match="artifact is invalid"):
        load_interest_artifact(
            path,
            expected_source_snapshot_digest=SNAPSHOT_DIGEST,
            expected_configuration_digest=CONFIG_DIGEST,
        )


def test_impact_receipt_counts_changed_positions_without_story_content() -> None:
    a = make_item("A", "https://example.com/a")
    b = make_item("B", "https://example.com/b")
    receipt = measure_ranking_impact({"AI": [a, b]}, {"AI": [b, a]})
    assert receipt == {"moved_rows": 2, "max_position_delta": 1}


def test_impact_receipt_counts_items_entering_and_leaving_a_capped_digest() -> None:
    a = make_item("A", "https://example.com/a")
    b = make_item("B", "https://example.com/b")

    receipt = measure_ranking_impact({"AI": [a]}, {"AI": [b]})

    assert receipt == {"moved_rows": 2, "max_position_delta": 1}
