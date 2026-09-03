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
    story_key,
)
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


def test_score_key_does_not_transfer_between_different_headlines_for_one_url() -> None:
    publisher = make_item("Publisher headline", "https://example.com/story")
    aggregator = make_item("Quantum networking breakthrough", "https://example.com/story")

    payload = build_interest_artifact(
        InterestProfile(revision=1, interests=("quantum networking",)),
        [publisher, aggregator],
        source_snapshot_digest=SNAPSHOT_DIGEST,
        configuration_digest=CONFIG_DIGEST,
        generated_at=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
    )

    assert story_key(publisher) not in payload["scores"]
    assert story_key(aggregator) in payload["scores"]


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
