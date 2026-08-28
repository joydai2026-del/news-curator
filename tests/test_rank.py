"""Ranking. Pure functions with an explicit `now`, so no clock mocking."""

from __future__ import annotations

from curator.config import Topic
from curator.rank import echo_score, keyword_score, rank_items, recency_score, score_item
from tests.conftest import make_item

CFG = {
    "recency_half_life_hours": 12.0,
    "weight_recency": 1.0,
    "weight_keyword": 0.6,
    "weight_source": 0.4,
    "weight_echo": 0.5,
    "echo_max_sources": 3,
    "title_lead_chars": 40,
    "title_lead_bonus": 0.25,
}
TOPIC = Topic(name="AI", keywords=["AI", "machine learning"])


class TestRecency:
    def test_brand_new_scores_one(self, now):
        assert recency_score(make_item("x", hours_ago=0), now, 12.0) == 1.0

    def test_half_life_halves_it(self, now):
        assert abs(recency_score(make_item("x", hours_ago=12), now, 12.0) - 0.5) < 1e-9

    def test_older_is_always_lower(self, now):
        a = recency_score(make_item("x", hours_ago=1), now, 12.0)
        b = recency_score(make_item("x", hours_ago=30), now, 12.0)
        assert a > b


class TestKeywordScore:
    def test_no_match_scores_zero(self, now):
        assert keyword_score(make_item("x"), TOPIC, lead_chars=40, lead_bonus=0.25) == 0.0

    def test_more_keywords_scores_higher(self):
        one = make_item("AI arrives")
        one.matched_keywords = ["AI"]
        two = make_item("AI and machine learning arrive")
        two.matched_keywords = ["AI", "machine learning"]
        assert keyword_score(two, TOPIC, lead_chars=0, lead_bonus=0.0) > keyword_score(
            one, TOPIC, lead_chars=0, lead_bonus=0.0
        )

    def test_lead_bonus_applies_at_the_front(self):
        lead = make_item("AI changes everything for the industry at large")
        lead.matched_keywords = ["AI"]
        late = make_item("A very long headline about many other things and then AI")
        late.matched_keywords = ["AI"]
        assert keyword_score(lead, TOPIC, lead_chars=40, lead_bonus=0.25) > keyword_score(
            late, TOPIC, lead_chars=40, lead_bonus=0.25
        )

    def test_lead_bonus_ignores_substring_lookalikes(self):
        # Regression: a substring search awarded the lead bonus because "ai"
        # occurs inside "Malaria", which is the exact confusion the filter
        # module exists to avoid.
        item = make_item("Malaria spreads while the AI industry debates safety")
        item.matched_keywords = ["AI"]
        no_bonus = keyword_score(item, TOPIC, lead_chars=10, lead_bonus=0.25)
        assert no_bonus == keyword_score(item, TOPIC, lead_chars=10, lead_bonus=0.0)


class TestEcho:
    def test_single_platform_no_bonus(self):
        assert echo_score(make_item("x"), max_sources=3) == 0.0

    def test_two_platforms_earns_a_bonus(self):
        item = make_item("x")
        item.echo_platforms = {"a", "b"}
        assert echo_score(item, max_sources=3) > 0

    def test_bonus_is_capped(self):
        many = make_item("x")
        many.echo_platforms = {"a", "b", "c", "d", "e"}
        assert echo_score(many, max_sources=3) == 1.0


class TestRankOrdering:
    def test_newer_wins_all_else_equal(self, now):
        old, new = make_item("AI news", hours_ago=30), make_item("AI news", hours_ago=1)
        for i in (old, new):
            i.matched_keywords = ["AI"]
        assert rank_items([old, new], TOPIC, now, CFG)[0] is new

    def test_corroborated_story_outranks_a_lone_one(self, now):
        lone = make_item("AI thing one", hours_ago=2)
        echoed = make_item("AI thing two", hours_ago=2)
        for i in (lone, echoed):
            i.matched_keywords = ["AI"]
        echoed.echo_platforms = {"a", "b", "c"}
        assert rank_items([lone, echoed], TOPIC, now, CFG)[0] is echoed

    def test_ordering_is_deterministic(self, now):
        items = [make_item(f"AI story {i}", f"https://e.com/{i}", hours_ago=2) for i in range(6)]
        for i in items:
            i.matched_keywords = ["AI"]
        first = [i.title for i in rank_items(list(items), TOPIC, now, CFG)]
        second = [i.title for i in rank_items(list(reversed(items)), TOPIC, now, CFG)]
        assert first == second

    def test_empty_input(self, now):
        assert rank_items([], TOPIC, now, CFG) == []

    def test_score_is_finite(self, now):
        item = make_item("AI news")
        item.matched_keywords = ["AI"]
        assert 0 < score_item(item, TOPIC, now, CFG) < 10
