"""Dedup, and the line between a certain merge and a guessed one."""

from __future__ import annotations

from curator.dedup import dedupe, numbers_in, title_similarity
from tests.conftest import make_item


class TestUrlDedup:
    def test_same_url_collapses(self):
        items = [
            make_item("A story", "https://example.com/x", source_id="a", platform="a"),
            make_item("A story", "https://www.example.com/x?utm_source=t", source_id="b", platform="b"),
        ]
        assert len(dedupe(items)) == 1

    def test_same_url_from_two_platforms_counts_as_echo(self):
        items = [
            make_item("A story", "https://example.com/x", source_id="a", platform="a"),
            make_item("Same story", "https://example.com/x", source_id="b", platform="b"),
        ]
        assert len(dedupe(items)[0].echo_platforms) == 2

    def test_different_urls_stay_separate(self):
        items = [
            make_item("One thing", "https://example.com/1"),
            make_item("Totally other", "https://example.com/2"),
        ]
        assert len(dedupe(items)) == 2


class TestFuzzyMerge:
    def test_near_identical_titles_collapse(self):
        items = [
            make_item("Apple ships a new laptop today", "https://a.com/1", platform="a"),
            make_item("Apple ships a new laptop today!", "https://b.com/2", platform="b"),
        ]
        assert len(dedupe(items)) == 1

    def test_fuzzy_merge_never_claims_corroboration(self):
        # Regression: a fuzzy title match used to inflate the "N sources" badge.
        # A guess must not become the evidence behind a factual claim.
        items = [
            make_item("Apple ships a new laptop today", "https://a.com/1", platform="a"),
            make_item("Apple ships a new laptop today!", "https://b.com/2", platform="b"),
        ]
        assert len(dedupe(items)[0].echo_platforms) == 1

    def test_differing_version_numbers_block_a_merge(self):
        # These score 0.96 on raw similarity but are two different releases.
        items = [
            make_item("Apple releases iOS 18.6.1", "https://a.com/1"),
            make_item("Apple releases iOS 18.6.2", "https://b.com/2"),
        ]
        assert len(dedupe(items)) == 2

    def test_differing_amounts_block_a_merge(self):
        items = [
            make_item("Startup raises 20 million dollars", "https://a.com/1"),
            make_item("Startup raises 200 million dollars", "https://b.com/2"),
        ]
        assert len(dedupe(items)) == 2

    def test_far_apart_in_time_never_merges(self):
        items = [
            make_item("Annual results announced", "https://a.com/1", hours_ago=1),
            make_item("Annual results announced", "https://b.com/2", hours_ago=400),
        ]
        assert len(dedupe(items, time_bucket_hours=36)) == 2


class TestAggregatorPreference:
    def test_publisher_title_beats_submitter_paraphrase(self):
        # Same link, two titles. The publisher wrote one of them; a Hacker News
        # submitter wrote the other. Showing the paraphrase under the
        # publisher's URL is the accuracy breach this prevents.
        items = [
            make_item(
                "Submitter's spicy rewrite",
                "https://publisher.com/story",
                source_id="hackernews",
                source_name="Hacker News",
                platform="hackernews",
                weight=1.5,
                score=900,
                aggregator=True,
            ),
            make_item(
                "The Real Published Headline",
                "https://publisher.com/story",
                source_id="publisher",
                source_name="Publisher",
                platform="publisher",
                weight=1.0,
            ),
        ]
        survivor = dedupe(items)[0]
        assert survivor.title == "The Real Published Headline"
        assert survivor.is_aggregator is False

    def test_aggregator_survives_when_it_is_the_only_source(self):
        items = [make_item("Only on HN", "https://x.com/1", source_id="hackernews", aggregator=True)]
        assert dedupe(items)[0].is_aggregator is True


class TestHelpers:
    def test_numbers_extracted(self):
        assert numbers_in("iOS 18.6.1") == ["18", "6", "1"]

    def test_identical_titles_score_one(self):
        assert title_similarity("Same thing", "Same thing") == 1.0

    def test_unrelated_titles_score_low(self):
        assert title_similarity("Apple ships laptop", "Senate passes bill") < 0.5

    def test_empty_titles_score_zero(self):
        assert title_similarity("", "anything") == 0.0


class TestRound2Threshold:
    """Opposite stories about the same release must not merge."""

    def test_releases_versus_delays_stay_separate(self):
        # Scores 0.875, which passed the old 0.85 threshold. Same numbers, so
        # the numeric guard cannot catch it: only the threshold can.
        items = [
            make_item("Apple releases iOS 18.6.1", "https://a.com/1"),
            make_item("Apple delays iOS 18.6.1", "https://b.com/2"),
        ]
        assert len(dedupe(items)) == 2

    def test_genuine_duplicates_still_merge_at_the_higher_threshold(self):
        items = [
            make_item("Apple ships a brand new laptop today", "https://a.com/1"),
            make_item("Apple ships a brand new laptop today.", "https://b.com/2"),
        ]
        assert len(dedupe(items)) == 1
