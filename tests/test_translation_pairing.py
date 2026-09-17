"""The model decides exclusivity. Never the network here: the provider is a stub."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from curator.grouping import GroupingCandidate, GroupingPolicy, event_group_id_for, exact_matches
from curator.translation.pairing import (
    PairingCost,
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
        self.context_sizes = []

    def decide(self, *, story, context):
        self.asked.append(story.story_id)
        self.context_sizes.append(len(context))
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
    from curator.translation import TranslationErrorReason, TranslationProviderError
    for answer in (TranslationProviderError("openai", TranslationErrorReason.PROVIDER_REJECTED),
                   (99, 0, 0), (True, 0, 0)):
        model = StubModel([answer])
        result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                    provider=model, now=NOW)
        assert result.undecided == {"story:zh1"}
        assert result.exclusive_story_ids == () and result.group_ids == {}


def test_a_story_is_decided_once_and_later_runs_reuse_the_record():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW - timedelta(hours=3),
                                model="gpt-5-mini", policy_id="pairing-json-v1", match_story_id=None,
                                outcome=EXCLUSIVE)
    model = StubModel([])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == [], "a decided story is never re-asked and never re-billed"
    assert result.exclusive_story_ids == ("story:zh1",)
    assert result.calls == 0


def test_a_prior_match_still_yields_the_same_group_id_in_a_later_run():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW, model="gpt-5-mini",
                                policy_id="pairing-json-v1", match_story_id="story:en1",
                                outcome=MATCH)
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
    assert model.asked == ["story:zh1"] and model.context_sizes == [5]


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


class StubLedger:
    """The persisted UTC-day pairing budget."""

    def __init__(self, allow=True):
        self.allow, self.reserved, self.settled = allow, [], []

    def reserve_call(self, amount_usd):
        self.reserved.append(amount_usd)
        return self.allow

    def settle_call(self, reserved_usd, settled_usd):
        self.settled.append((reserved_usd, settled_usd))


def test_every_pairing_call_is_reserved_and_settled_against_the_ledger():
    ledger = StubLedger()
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=StubModel([(None, 800, 12)]), now=NOW, ledger=ledger)
    assert len(ledger.reserved) == 1 and ledger.reserved[0] > 0
    assert len(ledger.settled) == 1
    reserved, settled = ledger.settled[0]
    # 800 input at 0.25/M plus 12 output at 2.0/M.
    assert settled == pytest.approx((800 * 0.25 + 12 * 2.0) / 1_000_000)
    assert result.exclusive_story_ids == ("story:zh1",)


def test_a_refused_reservation_asks_nothing_and_claims_nothing():
    ledger = StubLedger(allow=False)
    model = StubModel([])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=model, now=NOW, ledger=ledger)
    assert model.asked == [] and result.budget_refusals == 1
    assert result.undecided == {"story:zh1"} and result.exclusive_story_ids == ()


def test_a_provider_failure_after_reservation_still_costs_the_day():
    from curator.translation import TranslationErrorReason, TranslationProviderError
    ledger = StubLedger()
    decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                       provider=StubModel([TranslationProviderError("openai", TranslationErrorReason.TRANSPORT_FAILURE)]),
                       now=NOW, ledger=ledger)
    assert len(ledger.settled) == 1, "a failed paid call is not free"


def test_a_programmer_error_in_the_provider_is_not_swallowed_as_undecided():
    class Defective:
        provider_id = "openai"
        model_version = "gpt-5-mini:pairing-json-v1"

        def decide(self, *, story, context):
            raise TypeError("decide() got an unexpected keyword argument")

    with pytest.raises(TypeError):
        decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                           provider=Defective(), now=NOW)


def test_a_decision_that_cannot_be_persisted_is_treated_as_undecided():
    """An unpersisted answer must not drive money or visibility this run."""
    def failing_persist(decision):
        from curator.translation import StoreErrorReason, TranslationStoreError
        raise TranslationStoreError(StoreErrorReason.UNAVAILABLE)

    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=StubModel([(None, 10, 2)]), now=NOW, persist=failing_persist)
    assert result.exclusive_story_ids == ()
    assert result.undecided == {"story:zh1"} and result.persistence_failures == 1


def test_a_match_puts_both_rows_in_the_group():
    """Writing only the foreign row left the peer NULL, which is what made a
    matched story look exclusive to the display RPC."""
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                                provider=StubModel([(0, 10, 2)]), now=NOW)
    group = event_group_id_for("story:en1")
    assert result.group_ids["story:zh1"] == group
    assert result.group_ids["story:en1"] == group
    assert result.matched_pairs == {"story:zh1": "story:en1"}


def test_an_undecided_answer_is_retried_a_bounded_number_of_times():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW - timedelta(hours=8),
                                model="gpt-5-mini", policy_id="pairing-json-v1", match_story_id=None,
                                outcome=UNDECIDED, attempts=1,
                                retry_after=NOW - timedelta(hours=1))
    model = StubModel([(None, 10, 2)])
    decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(max_attempts=2),
                       provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == ["story:zh1"], "attempt 2 of 2 is asked"

    exhausted = ExclusivityDecision(story_id="story:zh1", decided_at=NOW - timedelta(hours=8),
                                    model="gpt-5-mini", policy_id="pairing-json-v1", match_story_id=None,
                                    outcome=UNDECIDED, attempts=2, retry_after=NOW - timedelta(hours=1))
    quiet = StubModel([])
    result = decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(max_attempts=2),
                                provider=quiet, now=NOW, already_decided={"story:zh1": exhausted})
    assert quiet.asked == [], "a story is never re-asked for ever"
    assert result.undecided == {"story:zh1"}


def test_an_undecided_answer_is_not_re_asked_before_its_retry_time():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW, model="gpt-5-mini",
                                policy_id="pairing-json-v1", match_story_id=None, outcome=UNDECIDED,
                                attempts=1, retry_after=NOW + timedelta(hours=5))
    model = StubModel([])
    decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                       provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == []


def test_a_decision_from_another_policy_is_re_asked():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW, model="gpt-5-mini",
                                policy_id="pairing-json-v0", match_story_id=None, outcome=EXCLUSIVE)
    model = StubModel([(None, 10, 2)])
    decide_exclusivity([ZH], [EN_MATCH], display_language="en", policy=policy(),
                       provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == ["story:zh1"]


def test_an_english_peer_published_after_the_decision_forces_one_recheck():
    """English coverage lags the Chinese wire, so the first look is the wrong
    moment to decide for ever."""
    decided_at = NOW - timedelta(hours=8)
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=decided_at, model="gpt-5-mini",
                                policy_id="pairing-json-v1", match_story_id=None, outcome=EXCLUSIVE)
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=9)
    # The peer arrived 2 hours AFTER the exclusive decision was made.
    peer = story("story:en1", "en", "Nvidia announces a new chip", hours=6)
    model = StubModel([(0, 10, 2)])
    result = decide_exclusivity([zh], [peer], display_language="en", policy=policy(recheck_hours=6),
                                provider=model, now=NOW, already_decided={"story:zh1": prior})
    assert model.asked == ["story:zh1"], "the recheck happens"
    assert result.decisions["story:zh1"].outcome == MATCH
    assert result.exclusive_story_ids == (), "it leaves the section"

    # And it is re-checked only ONCE: a decision already rechecked is final.
    rechecked = ExclusivityDecision(story_id="story:zh1", decided_at=decided_at, model="gpt-5-mini",
                                    policy_id="pairing-json-v1", match_story_id=None, outcome=EXCLUSIVE,
                                    rechecked_at=NOW - timedelta(hours=1))
    quiet = StubModel([])
    decide_exclusivity([zh], [peer], display_language="en", policy=policy(recheck_hours=6),
                       provider=quiet, now=NOW, already_decided={"story:zh1": rechecked})
    assert quiet.asked == []


def test_no_new_peer_means_no_recheck_and_no_new_spend():
    prior = ExclusivityDecision(story_id="story:zh1", decided_at=NOW - timedelta(hours=8),
                                model="gpt-5-mini", policy_id="pairing-json-v1", match_story_id=None,
                                outcome=EXCLUSIVE)
    old_peer = story("story:en1", "en", "Nvidia announces a new chip", hours=20)
    model, ledger = StubModel([]), StubLedger()
    result = decide_exclusivity([story("story:zh1", "zh", "英伟达发布新芯片", hours=9)], [old_peer],
                                display_language="en", policy=policy(recheck_hours=6), provider=model,
                                now=NOW, ledger=ledger, already_decided={"story:zh1": prior})
    assert model.asked == [] and ledger.reserved == []
    assert result.exclusive_story_ids == ("story:zh1",)


class DecisionStore:
    """Stands in for the SQL table, with the SAME refusal rules.

    The point of using this instead of hand-built decisions is that a test can
    no longer fabricate `rechecked_at`: only a real recheck call sets it.
    """

    def __init__(self):
        self.rows = {}
        self.records = 0
        self.rechecks = 0

    def key(self, decision):
        return (decision.story_id, decision.display_language, decision.policy_id)

    def record(self, decision):
        self.records += 1
        current = self.rows.get(self.key(decision))
        if current is not None and current.outcome in (EXCLUSIVE, MATCH):
            return current
        if current is not None:
            decision = replace(decision, attempts=current.attempts + 1)
        self.rows[self.key(decision)] = decision
        return decision

    def recheck(self, decision):
        self.rechecks += 1
        current = self.rows.get(self.key(decision))
        if current is None or current.outcome != EXCLUSIVE or current.rechecked_at is not None:
            return current
        # 'undecided' stamps that we looked and leaves the decision exclusive.
        outcome = current.outcome if decision.outcome == UNDECIDED else decision.outcome
        match = decision.match_story_id if decision.outcome == MATCH else None
        stored = replace(current, outcome=outcome, match_story_id=match,
                         rechecked_at=decision.rechecked_at or NOW)
        self.rows[self.key(decision)] = stored
        return stored

    def snapshot(self):
        return {story_id: decision for (story_id, _, _), decision in self.rows.items()}


def test_a_recheck_can_flip_exclusive_to_matched_through_the_real_store():
    """No fabricated rechecked_at: the store sets it, and the lane follows."""
    store = DecisionStore()
    first = decide_exclusivity([ZH], [], display_language="en", policy=policy(),
                               provider=StubModel([(None, 10, 2)]), now=NOW - timedelta(hours=8),
                               persist=store.record, recheck=store.recheck)
    # Nothing to compare against yet, so the first look is undecided...
    assert first.undecided == {"story:zh1"}

    # A real first decision, made when no peer existed yet.
    early_peer = story("story:en9", "en", "An unrelated English story", hours=9)
    decided = decide_exclusivity([story("story:zh1", "zh", "英伟达发布新芯片", hours=9)], [early_peer],
                                 display_language="en", policy=policy(),
                                 provider=StubModel([(None, 10, 2)]), now=NOW - timedelta(hours=8),
                                 persist=store.record, recheck=store.recheck)
    assert decided.exclusive_story_ids == ("story:zh1",)

    # The English peer arrives afterwards; the recheck window has passed.
    peer = story("story:en1", "en", "Nvidia announces a new chip", hours=6)
    model = StubModel([(0, 10, 2)])
    rechecked = decide_exclusivity([story("story:zh1", "zh", "英伟达发布新芯片", hours=9)], [early_peer, peer],
                                   display_language="en", policy=policy(recheck_hours=6), provider=model,
                                   now=NOW, already_decided=store.snapshot(),
                                   persist=store.record, recheck=store.recheck)
    assert model.asked == ["story:zh1"]
    assert rechecked.decisions["story:zh1"].outcome == MATCH
    assert rechecked.exclusive_story_ids == (), "the story leaves the section"
    assert store.rows[("story:zh1", "en", "pairing-json-v1")].rechecked_at is not None

    # A SECOND run makes no paid call at all.
    quiet = StubModel([])
    again = decide_exclusivity([story("story:zh1", "zh", "英伟达发布新芯片", hours=9)], [early_peer, peer],
                               display_language="en", policy=policy(recheck_hours=6), provider=quiet,
                               now=NOW + timedelta(hours=1), already_decided=store.snapshot(),
                               persist=store.record, recheck=store.recheck)
    assert quiet.asked == [], "a re-checked decision is final"
    assert again.exclusive_story_ids == ()


def test_one_exclusive_story_costs_exactly_one_recheck_over_two_days_of_hourly_runs():
    """The recheck bound is what stops pairing eating the day's budget."""
    store = DecisionStore()
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=1)
    # Present from the start, so the first run has something to compare against.
    early_peer = story("story:en9", "en", "An unrelated English story", hours=1)
    # Published THREE HOURS AFTER the first decision: this is what earns a recheck.
    late_peer = story("story:en1", "en", "Nvidia announces a new chip", hours=-3)
    model = StubModel([(None, 10, 2), (0, 10, 2)] + [(None, 10, 2)] * 60)
    ledger = StubLedger()
    start = NOW
    for hour in range(48):
        corpus = [early_peer] + ([late_peer] if hour >= 3 else [])
        decide_exclusivity([zh], corpus, display_language="en", policy=policy(recheck_hours=6),
                           provider=model, now=start + timedelta(hours=hour),
                           already_decided=store.snapshot(), persist=store.record,
                           recheck=store.recheck, ledger=ledger)
    # One first decision plus one re-check. Not 48.
    assert len(model.asked) == 2, model.asked
    assert len(ledger.reserved) == 2


def test_an_undecided_story_is_asked_at_most_max_attempts_over_many_runs():
    from curator.translation import TranslationErrorReason, TranslationProviderError
    store = DecisionStore()
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=1)
    peer = story("story:en1", "en", "Nvidia announces a new chip", hours=1)
    model = StubModel([TranslationProviderError("openai", TranslationErrorReason.PROVIDER_REJECTED)] * 60)
    start = NOW
    for hour in range(48):
        decide_exclusivity([zh], [peer], display_language="en",
                           policy=policy(max_attempts=2, recheck_hours=6), provider=model,
                           now=start + timedelta(hours=hour), already_decided=store.snapshot(),
                           persist=store.record, recheck=store.recheck)
    assert len(model.asked) == 2, model.asked


def test_a_recheck_the_store_refuses_leaves_the_stored_answer_in_place():
    store = DecisionStore()
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=9)
    early_peer = story("story:en9", "en", "An unrelated English story", hours=9)
    decide_exclusivity([zh], [early_peer], display_language="en", policy=policy(),
                       provider=StubModel([(None, 10, 2)]), now=NOW - timedelta(hours=8),
                       persist=store.record, recheck=store.recheck)
    # Someone already re-checked it, so the store refuses a second transition.
    key = ("story:zh1", "en", "pairing-json-v1")
    store.rows[key] = replace(store.rows[key], rechecked_at=NOW - timedelta(hours=2))
    peer = story("story:en1", "en", "Nvidia announces a new chip", hours=6)
    result = decide_exclusivity([zh], [early_peer, peer], display_language="en",
                                policy=policy(recheck_hours=6), provider=StubModel([(0, 10, 2)]),
                                now=NOW, already_decided=store.snapshot(),
                                persist=store.record, recheck=store.recheck)
    assert result.exclusive_story_ids == ("story:zh1",), "the persisted answer wins"


def test_a_failing_recheck_is_still_bounded_to_one_attempt():
    """Round 5 bounded the SUCCESS path only: a provider that keeps failing was
    re-asked every run, 504 paid calls for one story over the window."""
    from curator.translation import TranslationErrorReason, TranslationProviderError
    store = DecisionStore()
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=1)
    early_peer = story("story:en9", "en", "An unrelated English story", hours=1)
    late_peer = story("story:en1", "en", "Nvidia announces a new chip", hours=-3)
    # First look succeeds (exclusive); every later call fails.
    model = StubModel([(None, 10, 2)] +
                      [TranslationProviderError("openai", TranslationErrorReason.TRANSPORT_FAILURE)] * 200)
    ledger = StubLedger()
    for hour in range(48):
        corpus = [early_peer] + ([late_peer] if hour >= 3 else [])
        decide_exclusivity([zh], corpus, display_language="en", policy=policy(recheck_hours=6),
                           provider=model, now=NOW + timedelta(hours=hour),
                           already_decided=store.snapshot(), persist=store.record,
                           recheck=store.recheck, ledger=ledger)
    # One first decision, one failed re-check attempt. Not 48, and not 504.
    assert len(model.asked) == 2, len(model.asked)
    assert len(ledger.reserved) == 2
    stored = store.rows[("story:zh1", "en", "pairing-json-v1")]
    assert stored.rechecked_at is not None, "the failed re-check still stamps that we looked"
    assert stored.outcome == EXCLUSIVE, "a failure does not change the answer"


def test_a_recheck_that_returns_an_unusable_index_is_also_bounded():
    store = DecisionStore()
    zh = story("story:zh1", "zh", "英伟达发布新芯片", hours=1)
    early_peer = story("story:en9", "en", "An unrelated English story", hours=1)
    late_peer = story("story:en1", "en", "Nvidia announces a new chip", hours=-3)
    model = StubModel([(None, 10, 2)] + [(99, 10, 2)] * 200)
    for hour in range(24):
        corpus = [early_peer] + ([late_peer] if hour >= 3 else [])
        decide_exclusivity([zh], corpus, display_language="en", policy=policy(recheck_hours=6),
                           provider=model, now=NOW + timedelta(hours=hour),
                           already_decided=store.snapshot(), persist=store.record,
                           recheck=store.recheck)
    assert len(model.asked) == 2, len(model.asked)


def test_the_pairing_reservation_prices_the_allowance_the_request_sends():
    """Under-reserving is how a budget silently stops being a budget."""
    from curator.translation.model_provider import ModelTranslationConfig
    cost = PairingCost()
    assert cost.output_allowance_tokens == ModelTranslationConfig(
        provider_id="openai", model="gpt-5-mini").pairing_output_tokens
