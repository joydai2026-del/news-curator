"""The model decides exclusivity. Never the network here: the provider is a stub."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from curator.grouping import GroupingCandidate, GroupingPolicy, event_group_id_for, exact_matches
from curator.translation.pairing import (
    EXCLUSIVE,
    MATCH,
    UNDECIDED,
    ExclusivityDecision,
    PairingPolicy,
    build_question,
    decide_exclusivity,
    parse_match_index,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def story(story_id, language, title, summary="摘要。", hours=0, url="", categories=("world",), group=None):
    return GroupingCandidate(story_id=story_id, language=language, title=title, summary=summary,
                             published_at=NOW - timedelta(hours=hours), canonical_url=url,
                             category_ids=categories, event_group_id=group)


class StubModel:
    provider_id = "openai"
    model_version = "gpt-5-mini:pairing-json-v1"

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def decide(self, *, story, context):
        self.asked.append((story.story_id, len(context)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def policy(**kwargs):
    return PairingPolicy(model="gpt-5-mini", **kwargs)


ZH = story("story:zh1", "zh", "英伟达发布新芯片")
EN_MATCH = story("story:en1", "en", "Nvidia announces a new chip")
EN_OTHER = story("story:en2", "en", "A bridge opens in Lisbon")


def test_a_match_puts_the_story_in_the_matched_storys_group_and_it_is_not_exclusive():
    model = StubModel([(0, 800, 12)])
    result = decide_exclusivity([ZH], [EN_MATCH, EN_OTHER], display_language="en",
                                policy=policy(), provider=model, now=NOW)
    assert result.decisions["story:zh1"].outcome == MATCH
    assert result.group_ids["story:zh1"] == event_group_id_for("story:en1")
    assert result.exclusive_story_ids == ()


def test_an_explicit_null_marks_the_story_language_exclusive():
    model = StubModel([(None, 800, 8)])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=model, now=NOW)
    decision = result.decisions["story:zh1"]
    assert decision.outcome == EXCLUSIVE and decision.match_story_id is None
    assert decision.model == "gpt-5-mini" and decision.policy_id == "pairing-json-v1"
    assert result.exclusive_story_ids == ("story:zh1",)


def test_an_unusable_answer_is_undecided_and_claims_nothing():
    """Undecided means NOT translated and NOT shown, never 'exclusive'."""
    for answer in (RuntimeError("provider exploded"), (99, 0, 0), (True, 0, 0)):
        model = StubModel([answer])
        result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                    provider=model, now=NOW)
        assert result.undecided == {"story:zh1"}
        assert result.exclusive_story_ids == () and result.group_ids == {}


def test_a_story_is_decided_once_and_later_runs_reuse_the_record():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW - timedelta(hours=3),
                                model="gpt-5-mini", policy_id="pairing-json-v1", match_story_id=None)
    model = StubModel([])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == [], "a decided story is never re-asked and never re-billed"
    assert result.exclusive_story_ids == ("story:zh1",)
    assert result.calls == 0


def test_a_prior_match_still_yields_the_same_group_id_in_a_later_run():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW, model="gpt-5-mini",
                                policy_id="pairing-json-v1", match_story_id="story:en1")
    result = decide_exclusivity([ZH], [], display_language="en", policy=policy(),
                                provider=StubModel([]), now=NOW, already_decided={"story:zh1": prior})
    assert result.group_ids["story:zh1"] == event_group_id_for("story:en1")


def test_an_existing_group_on_the_matched_story_is_joined_not_replaced():
    existing = event_group_id_for("story:en0")
    matched = story("story:en1", "en", "Nvidia announces a new chip", group=existing)
    result = decide_exclusivity([ZH], [matched], display_language="en", policy=policy(),
                                provider=StubModel([(0, 800, 12)]), now=NOW)
    assert result.group_ids["story:zh1"] == existing


def test_the_prefilter_decision_is_free_and_is_never_asked():
    model = StubModel([])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=model, now=NOW, prefilter={"story:zh1": "group:" + "a" * 32})
    assert model.asked == [] and result.calls == 0
    assert result.group_ids["story:zh1"] == "group:" + "a" * 32


def test_no_context_inside_the_window_is_undecided_not_exclusive():
    old = story("story:en1", "en", "Nvidia announces a new chip", hours=400)
    result = decide_exclusivity([ZH], [old], display_language="en", policy=policy(),
                                provider=StubModel([]), now=NOW)
    assert result.undecided == {"story:zh1"} and result.exclusive_story_ids == ()


def test_the_context_is_capped_and_ordered_by_closeness_in_time():
    pool = [story(f"story:en{index}", "en", f"An English story {index}", hours=index)
            for index in range(1, 40)]
    model = StubModel([(None, 100, 4)])
    decide_exclusivity([ZH], pool, display_language="en",
                       policy=policy(max_context_titles=5), provider=model, now=NOW)
    assert model.asked == [("story:zh1", 5)]


def test_the_daily_call_limit_stops_asking_and_leaves_the_rest_undecided():
    stories = [story(f"story:zh{index}", "zh", f"中文报道 {index}") for index in range(4)]
    model = StubModel([(None, 10, 2)] * 4)
    result = decide_exclusivity(stories, [EN_MATCH], display_language="en",
                                policy=policy(daily_call_limit=2), provider=model, now=NOW)
    assert result.calls == 2
    assert len(result.undecided) == 2


def test_display_language_stories_are_never_asked_about():
    result = decide_exclusivity([EN_MATCH], [EN_OTHER], display_language="en", policy=policy(),
                                provider=StubModel([]), now=NOW)
    assert result.decisions == {} and result.undecided == set()


@pytest.mark.parametrize("content,expected", [
    (json.dumps({"match_index": 1}), 1),
    (json.dumps({"match_index": None}), None),
    ("not json", UNDECIDED),
    (json.dumps({"match_index": 1, "why": "because"}), UNDECIDED),
    (json.dumps({"match": 1}), UNDECIDED),
    (json.dumps({"match_index": 9}), UNDECIDED),
    (json.dumps({"match_index": True}), UNDECIDED),
    (json.dumps({"match_index": "1"}), UNDECIDED),
])
def test_only_the_exact_json_contract_is_accepted(content, expected):
    assert parse_match_index(content, context_size=3) == expected


def test_the_question_carries_titles_and_a_bounded_summary_prefix_only():
    long_summary = "详" * 500
    payload = build_question(story("story:zh1", "zh", "标题", long_summary), [EN_MATCH])
    assert set(payload) == {"story", "candidates"}
    assert set(payload["story"]) == {"title", "summary"}
    assert len(payload["story"]["summary"]) == 200
    assert payload["candidates"][0]["index"] == 0
    # No URL, no source, no story id crosses the boundary.
    assert "story_id" not in json.dumps(payload) and "http" not in json.dumps(payload)


@pytest.mark.parametrize("kwargs", [
    {"window_hours": 0}, {"window_hours": 169},
    {"max_context_titles": 0}, {"max_context_titles": 501},
    {"daily_call_limit": -1}, {"daily_call_limit": 5001},
])
def test_pairing_policy_refuses_out_of_range_values(kwargs):
    with pytest.raises(ValueError):
        PairingPolicy(**kwargs)


def test_the_prefilter_only_claims_certain_matches():
    same_url = [story("story:a", "en", "A headline", url="https://example.test/x"),
                story("story:b", "zh", "另一个标题", url="https://example.test/x")]
    assert len(exact_matches(same_url)) == 2
    same_title = [story("story:a", "en", "Nvidia announces a new chip"),
                  story("story:b", "zh", "Nvidia announces a new chip")]
    assert len(exact_matches(same_title)) == 2
    # Two different stories that merely share words are NOT claimed.
    unrelated = [story("story:a", "en", "Nvidia announces a new chip in 2026"),
                 story("story:b", "zh", "英伟达 2026 年发布新芯片")]
    assert exact_matches(unrelated) == {}
    assert exact_matches(same_url, policy=GroupingPolicy(cross_language_enabled=False)) == {}


def test_the_prefilter_joins_an_existing_group_rather_than_minting_one():
    existing = event_group_id_for("story:zero")
    rows = [story("story:a", "en", "A headline", url="https://example.test/x", group=existing),
            story("story:b", "zh", "另一个标题", url="https://example.test/x")]
    assert set(exact_matches(rows).values()) == {existing}
