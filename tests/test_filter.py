"""The accuracy gate. The malaria case is the reason this module exists."""

from __future__ import annotations

import pytest

from curator.config import Topic
from curator.filter import assign_topics, match_position, matched_terms, topic_match
from tests.conftest import make_item


class TestWordBoundaries:
    def test_ai_does_not_match_malaria(self):
        # The live HN API really did return this story for the query "AI".
        title = "Two German airport workers die of malaria after mosquito arrival"
        assert matched_terms(title, ["AI"]) == []

    @pytest.mark.parametrize("title", ["He said so", "Broken chain of custody", "Main street"])
    def test_ai_does_not_match_substrings(self, title):
        assert matched_terms(title, ["AI"]) == []

    def test_ai_matches_when_it_is_a_word(self):
        assert matched_terms("The AI boom continues", ["AI"]) == ["AI"]

    def test_case_insensitive(self):
        assert matched_terms("the ai boom", ["AI"]) == ["AI"]

    @pytest.mark.parametrize(
        "title",
        ["C++: a retrospective", "Why C++ won", "Learning C++, slowly", "(C++) at 40"],
    )
    def test_punctuation_terms_match_next_to_punctuation(self, title):
        # Requiring whitespace boundaries broke every one of these.
        assert matched_terms(title, ["C++"]) == ["C++"]

    def test_dotted_term(self):
        assert matched_terms("Shipping (.NET) at scale", [".NET"]) == [".NET"]

    def test_phrase_matches_across_flexible_whitespace(self):
        assert matched_terms("Building AI  agents today", ["AI agents"]) == ["AI agents"]

    def test_phrase_does_not_match_when_split(self):
        assert matched_terms("AI is for agents", ["AI agents"]) == []

    def test_smart_quotes_do_not_break_matching(self):
        assert matched_terms("Anthropic’s Claude ships", ["Claude"]) == ["Claude"]


class TestTopicMatch:
    def test_exclude_vetoes_a_keyword_hit(self):
        topic = Topic(name="Claude", keywords=["Claude"], exclude=["Claude Monet"])
        assert topic_match(make_item("Claude Monet painting sells"), topic) is None

    def test_exclude_does_not_veto_unrelated(self):
        topic = Topic(name="Claude", keywords=["Claude"], exclude=["Claude Monet"])
        assert topic_match(make_item("Claude ships a new model"), topic) == ["Claude"]

    def test_no_keyword_means_no_topic(self):
        topic = Topic(name="Rust", keywords=["Rust"])
        assert topic_match(make_item("Python 4 released"), topic) is None

    def test_aliases_count_as_matches(self):
        topic = Topic(name="Claude", keywords=["Claude"], aliases=["Model Context Protocol"])
        assert topic_match(make_item("Model Context Protocol explained"), topic) == [
            "Model Context Protocol"
        ]


class TestMatchPosition:
    def test_reports_real_position(self):
        assert match_position("AI everywhere", ["AI"]) == 0

    def test_ignores_substring_occurrences(self):
        # "ai" appears at index 1 inside "Malaria", but that is not a match.
        pos = match_position("Malaria and AI", ["AI"])
        assert pos == 12

    def test_none_when_absent(self):
        assert match_position("nothing here", ["AI"]) is None


class TestAssignTopics:
    def test_item_can_appear_under_two_topics(self):
        topics = [Topic(name="AI", keywords=["AI"]), Topic(name="Chips", keywords=["chips"])]
        buckets = assign_topics([make_item("AI chips are hot")], topics)
        assert len(buckets["AI"]) == 1 and len(buckets["Chips"]) == 1

    def test_each_copy_carries_its_own_matched_keywords(self):
        topics = [Topic(name="AI", keywords=["AI"]), Topic(name="Chips", keywords=["chips"])]
        buckets = assign_topics([make_item("AI chips are hot")], topics)
        assert buckets["AI"][0].matched_keywords == ["AI"]
        assert buckets["Chips"][0].matched_keywords == ["chips"]

    def test_unmatched_item_appears_nowhere(self):
        buckets = assign_topics([make_item("Gardening tips")], [Topic(name="AI", keywords=["AI"])])
        assert buckets["AI"] == []
