from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from curator.archive_candidate import (
    build_archive_candidate,
    stamp_archive_candidate_site,
    validate_archive_candidate,
    write_archive_candidate,
)
from curator.config import Category
from curator.identity import story_id_for_item
from curator.models import CoverageMention
from curator.rank import score_components
from tests.conftest import make_item

SITE_SHA256 = "f" * 64


def test_candidate_contains_exactly_publishable_rows_with_real_scores(now):
    visible = make_item("Visible", url="https://example.com/visible")
    visible.published_at = now - timedelta(hours=1)
    visible.description = "Publisher summary"
    visible.matched_keywords = ["AI"]
    omitted = make_item("Omitted", url="https://example.com/omitted")
    omitted.published_at = now - timedelta(hours=2)
    topic = Category(name="AI", id="ai", keywords=["AI"])
    ranking = {"weight_interest": 0.8}

    candidate = build_archive_candidate(
        {"AI": [visible, omitted]},
        categories=[topic],
        ranking=ranking,
        now=now,
        build_nonce="run-1",
        commit_sha="a" * 40,
        site_sha256=SITE_SHA256,
        require_summaries=True,
    )

    assert [story["title"] for story in candidate["stories"]] == ["Visible"]
    expected = score_components(visible, topic, now, ranking)
    expected.pop("interest")
    expected["final_score"] = sum(value for key, value in expected.items() if key != "final_score")
    assert candidate["entries"][0]["score_components"] == expected
    assert candidate["entries"][0]["ordering_mode"] == "weighted_total"
    assert candidate["entries"][0]["ordering_key"] == {
        "weighted_total": candidate["entries"][0]["score_components"]["final_score"]
    }
    assert candidate["entries"][0]["topic_ranks"] == {"ai": 1}
    assert candidate["entries"][0]["source_kind"] == "outlet"
    assert candidate["entries"][0]["source_name"] == visible.source_name
    assert candidate["entries"][0]["ranking_explanation"].startswith("Weighted using")
    validate_archive_candidate(candidate)


def test_preference_rank_archive_is_public_safe_without_changing_position(now):
    first = make_item("First", url="https://example.com/first")
    second = make_item("Second", url="https://example.com/second")
    for item in (first, second):
        item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [first, second]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="private-safe", commit_sha="a" * 40,
        site_sha256=SITE_SHA256, require_summaries=True,
        interest_scores={story_id_for_item(first): 0.9375},
    )

    assert [entry["position"] for entry in candidate["entries"]] == [1, 2]
    encoded = json.dumps(candidate, sort_keys=True)
    assert '"interest"' not in encoded
    assert "preference_score" not in encoded
    assert "0.9375" not in encoded
    assert candidate["entries"][0]["ordering_mode"] == "preference_then_freshness"
    assert set(candidate["entries"][0]["ordering_key"]) == {"published_at"}
    assert candidate["entries"][0]["ranking_explanation"] == (
        "Saved interests were considered first, then freshness."
    )


@pytest.mark.parametrize("interest_scores", [{}, {"story:" + "0" * 64: 1.0}])
def test_candidate_uses_baseline_mode_when_no_interest_signal_is_effective(now, interest_scores):
    item = make_item("Baseline", url="https://example.com/baseline")
    item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [item]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="baseline-mode", commit_sha="a" * 40,
        site_sha256=SITE_SHA256, require_summaries=True, interest_scores=interest_scores,
    )
    assert candidate["entries"][0]["ordering_mode"] == "weighted_total"


def test_candidate_carries_every_snapshot_topic_rank_on_each_story_entry(now):
    shared = make_item("Shared", url="https://example.com/shared")
    other = make_item("Other", url="https://example.com/other")
    for item in (shared, other):
        item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [other, shared], "Crypto": [shared]},
        categories=[Category(name="AI", id="ai"), Category(name="Crypto", id="crypto")],
        ranking={}, now=now, build_nonce="topic-ranks", commit_sha="a" * 40,
        site_sha256=SITE_SHA256, require_summaries=True,
    )

    shared_rows = [row for row in candidate["entries"] if row["story_id"] == story_id_for_item(shared)]
    assert [row["topic_ranks"] for row in shared_rows] == [
        {"ai": 2, "crypto": 1}, {"ai": 2, "crypto": 1}
    ]


def test_candidate_validator_rejects_private_ranking_metadata(now):
    item = make_item("Visible", url="https://example.com/visible")
    item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [item]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="reject-private", commit_sha="a" * 40,
        site_sha256=SITE_SHA256, require_summaries=True,
    )
    candidate["entries"][0]["ordering_key"]["preference_score"] = 0.5
    with pytest.raises(ValueError, match="public-safe"):
        validate_archive_candidate(candidate)


def test_candidate_keeps_safe_named_coverage_and_distinct_source_count(now):
    item = make_item("Covered", url="https://example.com/covered")
    item.description = "Publisher summary"
    item.coverage_mentions.extend(
        [
            CoverageMention(
                "mention:" + "b" * 64,
                "newsletter",
                "daily",
                "Daily Brief",
                "https://publisher.example/story",
                "Covered",
                now,
            ),
            CoverageMention(
                "mention:" + "c" * 64,
                "newsletter",
                "daily",
                "Daily Brief",
                "javascript:alert(1)",
                "Covered again",
                now,
            ),
        ]
    )

    candidate = build_archive_candidate(
        {"AI": [item]},
        categories=[Category(name="AI", id="ai")],
        ranking={},
        now=now,
        build_nonce="run-2",
        commit_sha="b" * 40,
        site_sha256=SITE_SHA256,
        require_summaries=True,
    )

    assert len(candidate["coverage_mentions"]) == 2
    assert candidate["stories"][0]["distinct_coverage_source_count"] == 2
    assert all(row["url"].startswith("https://") for row in candidate["coverage_mentions"])


def test_candidate_attaches_a_filtered_newsletter_mention_by_story_identity(now):
    item = make_item("Publisher row", url="https://example.com/story")
    item.description = "Publisher summary"
    mention = CoverageMention(
        "mention:" + "d" * 64,
        "newsletter",
        "newsletter:brief",
        "Daily Brief",
        "https://www.example.com/story/?utm_source=mail",
        "Newsletter headline",
        now,
        canonical_url="https://example.com/story",
        story_id=story_id_for_item(item),
    )

    candidate = build_archive_candidate(
        {"AI": [item]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="run-filtered", commit_sha="d" * 40,
        site_sha256=SITE_SHA256,
        require_summaries=True, coverage_mentions=[mention],
    )

    assert {row["source_name"] for row in candidate["coverage_mentions"]} == {
        "Example", "Daily Brief"
    }


def test_candidate_validation_is_bounded(now):
    item = make_item("Visible", url="https://example.com/visible")
    item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [item]},
        categories=[Category(name="AI", id="ai")],
        ranking={},
        now=now,
        build_nonce="run-3",
        commit_sha="c" * 40,
        site_sha256=SITE_SHA256,
        require_summaries=True,
    )
    candidate["stories"] = candidate["stories"] * 501

    try:
        validate_archive_candidate(candidate)
    except ValueError as exc:
        assert "stories" in str(exc)
    else:
        raise AssertionError("unbounded candidate accepted")


def test_candidate_rejects_invalid_site_hash(now):
    item = make_item("Visible", url="https://example.com/visible")
    item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [item]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="run-site", commit_sha="e" * 40,
        site_sha256=SITE_SHA256, require_summaries=True,
    )
    candidate["site_sha256"] = "F" * 64
    try:
        validate_archive_candidate(candidate)
    except ValueError as exc:
        assert "site_sha256" in str(exc)
    else:
        raise AssertionError("invalid site hash accepted")


def test_restamp_binds_candidate_to_final_site_bytes(now, tmp_path):
    item = make_item("Visible", url="https://example.com/visible")
    item.description = "Publisher summary"
    candidate = build_archive_candidate(
        {"AI": [item]}, categories=[Category(name="AI", id="ai")], ranking={},
        now=now, build_nonce="run-restamp", commit_sha="e" * 40,
        site_sha256=SITE_SHA256, require_summaries=True,
    )
    candidate_path = tmp_path / "candidate.json"
    site_path = tmp_path / "index.html"
    write_archive_candidate(candidate_path, candidate)
    site_path.write_bytes(b"<html>final materialized page</html>")

    stamp_archive_candidate_site(candidate_path, site_path)

    stamped = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert stamped["site_sha256"] == hashlib.sha256(site_path.read_bytes()).hexdigest()
    validate_archive_candidate(stamped)
