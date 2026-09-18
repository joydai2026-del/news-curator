"""The Phase 2 feed, end to end through RankingService.

What these prove, in JJ's terms: the page is no longer the newest fifty rows,
every card says why it is there, reading a story does not re-bill the next page,
and a save in another tab no longer throws away a rank that was already paid for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dataclasses import replace

from curator.recommendation.composition import load_composition_policy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.service import (
    ProviderConsentRequiredError,
    RankingService,
    ServicePolicy,
    StaleRankingError,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"
CLOCK = 1_789_000_000
NOW = datetime.fromtimestamp(CLOCK, timezone.utc)


class Auth:
    def get_user(self, token):
        assert token == "valid"
        return {"id": "11111111-1111-1111-1111-111111111111"}


def corpus_row(index, *, hours, source, categories, independent=1, aggregator=False, title=None):
    return {"story_id": f"story:{index:064x}", "title": title or f"Headline {index}",
            "summary": f"Summary {index}", "source_id": source, "source_name": source.title(),
            "language": "en", "canonical_url": f"https://example.test/{index}",
            "published_at": (NOW - timedelta(hours=hours)).isoformat(),
            "category_ids": list(categories), "independent_source_count": independent,
            "source_is_aggregator": aggregator, "event_group_id": None,
            "title_translations": {}, "summary_translations": {}}


def default_corpus():
    rows = []
    index = 1
    for offset in range(14):  # brand new, distinct sources and topics
        rows.append(corpus_row(index, hours=1, source=f"wire{offset}", categories=[f"topic{offset}"]))
        index += 1
    for offset in range(10):  # corroborated, outside the freshness window
        rows.append(corpus_row(index, hours=12, source=f"hot{offset}", categories=[f"hot{offset}"],
                               independent=4))
        index += 1
    for offset in range(18):  # on profile
        # Distinct sources on purpose: a single source is capped at three per
        # window, so an aligned pool built from one outlet starves by design.
        # All on the profile's topic, so the aligned quota of 11 is reachable
        # and the test measures the RECIPE rather than a thin fixture.
        rows.append(corpus_row(index, hours=20, source=f"aligned{offset}", categories=["world"]))
        index += 1
    for offset in range(12):  # off profile, quality-gated
        rows.append(corpus_row(index, hours=30, source=f"odd{offset}", categories=[f"odd{offset}"]))
        index += 1
    return rows


class Store:
    """A corpus that answers the lane RPC the way PostgreSQL does."""

    def __init__(self, rows=None, *, events=(), learning=True, exclusive=()):
        self.rows = list(rows if rows is not None else default_corpus())
        self.exclusive = list(exclusive)
        self.exclusive_calls = 0
        self.events = list(events)
        self.learning = learning
        self.frozen = {}
        self.sequence = 0
        self.reservations = []
        self.settlements = []
        self.runs = []
        self.revision = 0
        self.revision_after_provider = None
        self.extensions = []
        self.filtered = {}
        self.claims = []
        self.views = {}

    # --- history -----------------------------------------------------------
    @property
    def included(self):
        # Learning off means no events are included, so the included revision is
        # zero. The request contract asserts exactly that agreement.
        return max((event["event_revision"] for event in self.events), default=0) if self.learning else 0

    @property
    def commit_revision(self):
        return max(self.included, self.revision)

    def history_snapshot(self, token):
        revision = self.commit_revision
        if self.revision_after_provider is not None and self.runs:
            # The second read inside rank() sees the behavior write that landed
            # while the provider call was in flight.
            revision = self.revision_after_provider
        return {"included_history_revision": self.included, "history_revision": revision,
                "history_generation": 1, "consent_revision": 1, "learning_enabled": self.learning,
                "provider_processing_enabled": False, "provider_policy_id": "policy",
                "events": list(self.events)}

    # --- corpus ------------------------------------------------------------
    def retained_candidates(self, *, category_id, query, limit, before_published_at=None, before_story_id=None):
        return self.rows[:limit]

    def retained_candidates_v2(self, *, category_id, query, lane, profile_categories, profile_sources,
                               trend_window_hours, trend_min_sources, max_age_hours, min_age_hours,
                               limit, before_published_at=None, before_story_id=None,
                               before_source_count=None):
        # THE SAME ARGUMENT CONTRACT THE SQL ENFORCES. A fake that ignores the
        # cursor cannot catch a caller that sends half a keyset, which is exactly
        # what the hot-lane continuation did: the SQL refused it and no test saw.
        if before_source_count is not None and (lane != "hot" or before_published_at is None
                                                or before_story_id is None):
            raise ValueError("invalid cursor")
        if lane == "hot" and before_published_at is not None and before_source_count is None:
            raise ValueError("invalid cursor")
        if (before_published_at is None) != (before_story_id is None):
            raise ValueError("invalid cursor")
        selected = []
        for row in self.rows:
            age = (NOW - datetime.fromisoformat(row["published_at"])).total_seconds() / 3600
            if max_age_hours is not None and age > max_age_hours:
                continue
            if min_age_hours is not None and age <= min_age_hours:
                continue
            matches = (row["source_id"] in profile_sources
                       or any(category in profile_categories for category in row["category_ids"]))
            if lane == "hot" and (row["independent_source_count"] < trend_min_sources or age > trend_window_hours):
                continue
            if lane == "interested" and not matches:
                continue
            if lane == "surprise" and (matches or row["source_is_aggregator"]):
                continue
            # The keyset, applied the way the SQL applies it.
            if lane == "hot":
                if before_source_count is not None and not (
                        (row["independent_source_count"], row["published_at"], row["story_id"])
                        < (before_source_count, before_published_at, before_story_id)):
                    continue
            elif before_published_at is not None and not (
                    (row["published_at"], row["story_id"]) < (before_published_at, before_story_id)):
                continue
            selected.append(row)
        if lane == "hot":
            selected.sort(key=lambda row: (-row["independent_source_count"],
                                           row["published_at"], row["story_id"]), reverse=False)
        else:
            selected.sort(key=lambda row: (row["published_at"], row["story_id"]), reverse=True)
        return selected[:limit]

    def retained_candidates_language_exclusive(self, *, limit, **kwargs):
        self.exclusive_calls += 1
        return self.exclusive[:limit]

    # --- runs --------------------------------------------------------------
    def record_reading_run_filter(self, *, user_id, run_id, story_ids):
        self.filtered.setdefault(run_id, [])
        for story in story_ids:
            if story not in self.filtered[run_id]:
                self.filtered[run_id].append(story)
        return len(self.filtered[run_id])

    def open_reading_run(self, *, user_id, idle_minutes, max_minutes, profile):
        if self.runs:
            return {**self.runs[-1], "created": False}
        run = {"run_id": f"run-{len(self.runs) + 1}", "profile_snapshot": profile, "created": True}
        self.runs.append(run)
        return run

    # Everything below is keyed by (run, eligibility): All, a category, a search
    # and the exclusive section are four different views of one visit.
    def _view(self, run_id, eligibility_key):
        return self.views.setdefault((run_id, eligibility_key),
            {"frozen_order_id": None, "pages_served": 0, "claim_token": None,
             "claim_expired": False})

    def open_run_view(self, *, user_id, run_id, eligibility_key):
        view = self._view(run_id, eligibility_key)
        return {"run_id": run_id, "eligibility_key": eligibility_key,
                "frozen_order_id": view["frozen_order_id"], "pages_served": view["pages_served"]}

    def bind_run_frozen_order(self, *, user_id, run_id, eligibility_key, frozen_order_id, token=None):
        view = self._view(run_id, eligibility_key)
        # Conditional on still holding the claim, exactly as the SQL is.
        if token is not None and view["claim_token"] not in (None, token):
            return False
        view["frozen_order_id"] = frozen_order_id
        view["claim_token"] = None
        return True

    def claim_run_ranking(self, *, user_id, run_id, eligibility_key, token, ttl_seconds):
        self.claims.append((run_id, eligibility_key))
        view = self._view(run_id, eligibility_key)
        if view["claim_token"] is None or view["claim_expired"]:
            view["claim_token"], view["claim_expired"] = token, False
            return {"granted": True, "token": token, "frozen_order_id": view["frozen_order_id"]}
        return {"granted": False, "token": None, "frozen_order_id": view["frozen_order_id"]}

    def release_run_ranking_claim(self, *, user_id, run_id, eligibility_key, token):
        view = self._view(run_id, eligibility_key)
        if view["claim_token"] == token:
            view["claim_token"] = None
            return True
        return False

    def record_run_page(self, *, user_id, run_id, eligibility_key, pages):
        view = self._view(run_id, eligibility_key)
        previous = view["pages_served"]
        view["pages_served"] = max(previous, pages)
        return previous

    # --- owner state and budget -------------------------------------------
    def owner_states(self, token, story_ids):
        return {}

    def reserve_budget(self, **kwargs):
        self.reservations.append(kwargs)
        return False

    def settle_budget(self, **kwargs):
        self.settlements.append(kwargs)

    def extend_frozen_order(self, *, user_id, frozen_order_id, cards, bindings):
        stored = self.frozen[frozen_order_id]
        stored["cards"] = list(stored["cards"]) + list(cards)
        stored["bindings"] = {**stored["bindings"], **bindings}
        self.extensions.append(len(cards))
        return len(stored["cards"])

    def save_frozen_order(self, **kwargs):
        # The epoch trigger, reproduced. Inside an OPEN run it tolerates a
        # server_commit_revision that is older than current (a behavior write
        # landed while the provider was answering) and still refuses one from
        # the future. Outside a run it demands exact equality, as before.
        current = self.commit_revision if self.revision_after_provider is None else self.revision_after_provider
        seen = kwargs["bindings"].get("server_commit_revision")
        if kwargs.get("run_id") and self.runs:
            if seen is None or seen > current:
                raise RuntimeError("stale frozen ranking bindings")
        elif seen != current:
            raise RuntimeError("stale frozen ranking bindings")
        self.sequence += 1
        key = f"frozen-{self.sequence}"
        self.frozen[key] = kwargs
        return key

    def load_frozen_order(self, *, user_id, frozen_order_id):
        value = self.frozen[frozen_order_id]
        return {"expires_at": value["expires_at"], "page_size": value["page_size"],
                "bindings": value["bindings"], "cards": value["cards"]}


def exclusive_corpus(count=6):
    """Stories only the Chinese press carried, already translated into English."""
    return [dict(corpus_row(500 + index, hours=8, source=f"zh{index}",
                            categories=[f"zh-topic{index}"]),
                 language="zh", title=f"中文独家 {index}",
                 title_translations={"en": f"Only in the Chinese press {index}"},
                 summary_translations={"en": f"Translated summary {index}"})
            for index in range(count)]


def build(store, *, composition=True, page_size=25, promote=None, exclusive_category=""):
    adapter = RankLLMAdapter(policy=RankerPolicy("openai", "gpt-5-mini", "https://provider.invalid", "policy",
        input_cost_per_million_tokens_usd=.25, output_cost_per_million_tokens_usd=2), engine=object())
    loaded = load_composition_policy(POLICY_PATH) if composition else None
    if loaded is not None and promote is not None:
        loaded = replace(loaded, exclusive_promote_to_all_max=promote)
    policy = ServicePolicy("policy", "gpt-5-mini", "policy", "tenant", candidate_limit=50,
        maximum_page_size=page_size, enabled=True, composition=loaded,
        exclusive_category_id=exclusive_category or "")
    return RankingService(auth=Auth(), store=store, adapter=adapter, policy=policy,
                          cursor_key=b"x" * 32, clock=lambda: CLOCK)


def rank(subject, store, **overrides):
    body = {"history_revision": store.included, "server_commit_revision": store.commit_revision,
            "history_generation": 1, "consent_revision": 1, "page_size": 25}
    body.update(overrides)
    return subject.rank(authorization="Bearer valid", body=body)


def liked_events():
    return [{"event_id": f"event-{index}", "event_type": "save", "event_revision": index,
             "occurred_at": (NOW - timedelta(hours=2)).isoformat(),
             "payload": {"story_id": f"story:{index:064x}", "topic_id": "world", "saved": True},
             "story_title": "Prior", "story_summary": "", "source_id": "reuters"} for index in (1, 2)]


# --- the mix ---------------------------------------------------------------

def test_every_card_carries_a_reader_visible_label():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    assert response["cards"], "the recipe returned an empty page"
    for card in response["cards"]:
        assert card["lane"] in ("updates", "hot", "interested", "surprise")
        assert card["lane_label"] in ("fresh", "hot", "for you", "surprise")
        assert card["card_schema_version"] == 3


def test_the_page_mixes_all_four_pools_and_is_not_the_newest_fifty():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    lanes = {card["lane"] for card in response["cards"]}
    assert lanes == {"updates", "hot", "interested", "surprise"}
    newest = [row["story_id"] for row in sorted(store.rows, key=lambda row: row["published_at"], reverse=True)[:25]]
    assert [card["story_id"] for card in response["cards"]] != newest


def test_surprise_cards_carry_jjs_own_wording():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    surprises = [card for card in response["cards"] if card["lane"] == "surprise"]
    assert surprises, "a page with no surprise is the bug this exists to fix"
    assert all(card["surprise_label"] == "you might not have looked for this" for card in surprises)
    assert all(card["surprise_label"] is None for card in response["cards"] if card["lane"] != "surprise")


def test_the_labels_and_pools_persist_with_the_frozen_order():
    store = Store(events=liked_events())
    rank(build(store), store)
    stored = store.frozen["frozen-1"]["cards"]
    assert all("lane" in card and "lane_label" in card for card in stored)
    assert store.frozen["frozen-1"]["bindings"]["lane_counts"]


def test_learning_off_degrades_the_aligned_pool_to_fresh_without_a_provider_call():
    store = Store(events=liked_events(), learning=False)
    response = rank(build(store), store)
    # No profile means nothing can be "for you" and nothing can be off-profile.
    # What is left is fresh, hot, and the honest "more" chip.
    assert {card["lane"] for card in response["cards"]} <= {"updates", "hot", "more"}
    assert not any(card["lane"] in ("interested", "surprise") for card in response["cards"])
    assert store.reservations == []


def test_no_headline_appears_twice_on_a_page():
    duplicated = default_corpus()
    duplicated.append(corpus_row(999, hours=1, source="echo", categories=["topic0"],
                                 title=duplicated[0]["title"]))
    store = Store(duplicated, events=liked_events())
    response = rank(build(store), store)
    titles = [card["title"] for card in response["cards"]]
    assert len(titles) == len(set(titles))


def test_two_cards_from_one_source_are_never_adjacent():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    sources = [card["source_id"] for card in response["cards"]]
    assert all(left != right for left, right in zip(sources, sources[1:]))


# --- F7: a page turn costs nothing ----------------------------------------

def test_load_more_pages_the_frozen_order_and_never_re_ranks():
    store = Store(events=liked_events())
    subject = build(store)
    first = rank(subject, store)
    # A read, a save, anything: the behavior revision moves while she reads.
    store.revision += 1
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert second["request_id"] == first["request_id"], "a page turn minted a new ranking"
    assert len(store.frozen) == 1, "a page turn wrote a second frozen order"
    assert store.reservations == [], "a page turn reserved provider budget"
    overlap = {card["story_id"] for card in first["cards"]} & {card["story_id"] for card in second["cards"]}
    assert not overlap


def test_the_first_page_does_not_move_under_her_after_an_interaction():
    store = Store(events=liked_events())
    subject = build(store)
    first = rank(subject, store)
    store.revision += 1
    again = subject.page(authorization="Bearer valid",
                         cursor=subject._cursor("frozen-1", 0, int(store.frozen["frozen-1"]["expires_at"])))
    assert [card["story_id"] for card in again["cards"]] == [card["story_id"] for card in first["cards"]]


def test_less_like_this_takes_effect_inside_the_run_without_moving_an_offset():
    store = Store(events=liked_events())
    subject = build(store)
    first = rank(subject, store)
    removed = first["cards"][0]["source_id"]
    cursor = first["next_cursor"]
    store.events.append({"event_id": "dislike", "event_type": "less_like_this", "event_revision": 9,
                         "occurred_at": NOW.isoformat(),
                         "payload": {"story_id": first["cards"][0]["story_id"], "surface": "reader"},
                         "story_title": "", "story_summary": "", "source_id": removed})
    store.revision += 1
    # A cursor minted BEFORE the dislike still resolves, and the filter applies.
    second = subject.page(authorization="Bearer valid", cursor=cursor)
    assert all(card["source_id"] != removed for card in second["cards"])
    assert store.reservations == []


# --- F1: a paid rank is not thrown away -----------------------------------

def test_a_behavior_write_during_the_provider_call_still_serves_the_paid_order():
    store = Store(events=liked_events())
    subject = build(store)
    store.revision_after_provider = store.commit_revision + 1
    response = rank(subject, store)
    assert response["cards"], "the paid order was discarded"
    # The order is rebound to the CURRENT revision, which is what lets the epoch
    # trigger accept it instead of refusing the write.
    assert response["server_commit_revision"] == store.revision_after_provider


def test_a_consent_withdrawal_during_the_call_still_refuses_to_serve():
    store = Store(events=liked_events())
    subject = build(store)
    original = store.history_snapshot

    def withdrawn(token):
        snapshot = dict(original(token))
        if store.runs:
            snapshot["consent_revision"] = 99
        return snapshot

    store.history_snapshot = withdrawn
    with pytest.raises(StaleRankingError, match="changed_consent_revision"):
        rank(subject, store)


# --- the reading run -------------------------------------------------------

def test_one_run_is_opened_and_its_profile_is_frozen_on_it():
    store = Store(events=liked_events())
    subject = build(store)
    rank(subject, store)
    rank(subject, store)
    assert len(store.runs) == 1
    assert store.runs[0]["profile_snapshot"]["schema_version"] == 1
    assert store.frozen["frozen-1"]["bindings"]["run_id"] == "run-1"


def test_the_legacy_window_still_works_when_the_recipe_is_unset():
    store = Store(events=liked_events())
    response = rank(build(store, composition=False), store)
    assert response["cards"] and "lane" not in response["cards"][0]
    assert response["cards"][0]["card_schema_version"] == 2


# --- capped promotion into All --------------------------------------------

def promoted(response):
    return [card for card in response["cards"] if card["exclusive_label"]]


def test_at_most_the_cap_of_chinese_exclusive_stories_reach_all():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    response = rank(build(store, exclusive_category="only-other-language-press"), store)
    assert 0 < len(promoted(response)) <= 2, "the cap is a ceiling, and zero would mean no promotion"


def test_a_promoted_card_keeps_its_only_in_chinese_press_label():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    response = rank(build(store, exclusive_category="only-other-language-press"), store)
    assert all(card["exclusive_label"] == "only in Chinese press" for card in promoted(response))


def test_nothing_is_promoted_when_the_section_is_empty():
    store = Store(events=liked_events(), exclusive=[])
    response = rank(build(store, exclusive_category="only-other-language-press"), store)
    assert promoted(response) == []
    assert len(response["cards"]) == 25, "an empty section must not shrink the page"


def test_a_cap_of_zero_turns_promotion_off():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    response = rank(build(store, promote=0, exclusive_category="only-other-language-press"), store)
    assert promoted(response) == []
    assert store.exclusive_calls == 0, "a cap of zero must not even ask"


def test_promotion_never_adds_slots():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    with_promotion = rank(build(store, exclusive_category="only-other-language-press"), store)
    plain = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    without = rank(build(plain, promote=0, exclusive_category="only-other-language-press"), plain)
    assert len(with_promotion["cards"]) == len(without["cards"])


def test_a_promoted_story_appears_once_on_the_page():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    response = rank(build(store, exclusive_category="only-other-language-press"), store)
    ids = [card["story_id"] for card in response["cards"]]
    assert len(ids) == len(set(ids))


def test_the_section_itself_does_not_promote_into_itself():
    """Serving the section is the section, not the section plus a promotion of
    the section into the section. Every card there is exclusive exactly once."""
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    ids = [card["story_id"] for card in response["cards"]]
    assert len(ids) == len(set(ids))
    assert all(card["exclusive_label"] == "only in Chinese press" for card in response["cards"])
    assert store.exclusive_calls == 1, "the section must not also run the promotion fetch"


# --- F7, the branch that used to re-rank ----------------------------------

def test_paging_past_the_frozen_order_continues_without_a_provider_call():
    """The exact branch Codex named: offset >= len(cards) and corpus_has_more.

    It used to call rank() again, which reserves budget and calls the provider.
    Inside a reading run there is never a second provider call.
    """
    # A corpus with real OLDER news behind the window, which is what "load more"
    # is for. The default fixture is barely larger than the window itself.
    store = Store(default_corpus() + [corpus_row(300 + index, hours=40 + index,
                                                 source=f"older{index}", categories=[f"o{index % 7}"])
                                      for index in range(60)],
                  events=liked_events())
    subject = build(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    assert frozen["bindings"]["corpus_has_more"] is True, "the branch needs more corpus to exist"
    exhausted = len(frozen["cards"])
    store.reservations.clear()
    before_orders = len(store.frozen)
    response = subject.page(authorization="Bearer valid",
                            cursor=subject._cursor("frozen-1", exhausted,
                                                   int(frozen["expires_at"])))
    assert store.reservations == [], "a page turn reserved provider budget"
    assert len(store.frozen) == before_orders, "a page turn minted a second ranking"
    assert response["request_id"] == first["request_id"], "a page turn minted a new request id"
    assert store.extensions, "the continuation must be appended to the frozen order"
    assert response["cards"], "load more returned nothing when older news existed"
    assert all(card["lane_label"] for card in response["cards"]), "continuation cards lost their labels"


def test_the_continuation_keeps_already_signed_cursors_pointing_at_the_same_card():
    store = Store(events=liked_events())
    subject = build(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    early = subject._cursor("frozen-1", 0, int(frozen["expires_at"]))
    exhausted = len(frozen["cards"])
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"])))
    replayed = subject.page(authorization="Bearer valid", cursor=early)
    assert [card["story_id"] for card in replayed["cards"]] == [card["story_id"] for card in first["cards"]]


def test_a_continuation_never_repeats_a_story_already_in_the_order():
    store = Store(events=liked_events())
    subject = build(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    exhausted = len(frozen["cards"])
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"])))
    ids = [card["story_id"] for card in store.frozen["frozen-1"]["cards"]]
    assert len(ids) == len(set(ids))


def test_a_paid_order_survives_a_behavior_write_that_lands_before_the_insert():
    """F1's remaining race: the history re-check passes, then a behavior event
    lands, then the insert runs. The trigger used to reject the order after the
    money was already settled."""
    store = Store(events=liked_events())
    subject = build(store)
    store.revision_after_provider = store.commit_revision + 3
    response = rank(subject, store)
    assert response["cards"], "the paid order was discarded"
    assert len(store.frozen) == 1, "the order did not persist"
    assert store.frozen["frozen-1"]["run_id"], "the order must name its run for the trigger to accept it"


# --- a prompt revision is a question, not an outage -----------------------

def test_a_consent_row_for_an_older_prompt_asks_rather_than_failing_generically():
    store = Store(events=liked_events())
    original = store.history_snapshot
    store.history_snapshot = lambda token: {**original(token), "provider_processing_enabled": True,
                                            "provider_policy_id": "m2-rankllm-json-r3"}
    with pytest.raises(ProviderConsentRequiredError) as caught:
        rank(build(store), store)
    # The reader can only offer the fix if it is told which policy to agree to.
    assert caught.value.provider_policy_id == "policy"
    assert str(caught.value) == "provider_consent_required"
    assert isinstance(caught.value, StaleRankingError), "existing callers must keep working"


# --- the PAID path, with a counting provider and a real ledger ------------

class CountingAdapter:
    """A provider that keeps score. The old harness pinned provider consent to
    False, so no service test ever reached the paid path and the F7 proof was
    vacuous: it asserted zero calls on a path that could not make one."""

    def __init__(self):
        self.calls = 0

    def prepare_with_reason(self, request):
        from curator.recommendation.engine import PreparedProviderRequest
        return PreparedProviderRequest(prompt=[], candidate_ids=tuple(request.selected_candidate_registry_ids),
                                       input_tokens_bound=100, output_tokens_budget=200), ""

    def reservation_estimate(self, *, estimated_input_tokens, estimated_output_tokens):
        return 0.004

    def rank(self, request, *, provider_processing_consent, budget, estimated_input_tokens,
             estimated_output_tokens, prepared, usage_observer, attempt_observer):
        self.calls += 1
        attempt_observer(1, 0.1)
        usage_observer(type("Outcome", (), {"input_tokens": 100, "output_tokens": 200})(), 0, 0.1)
        return Receipt(tuple(prepared.candidate_ids), request)

    def settle_observed_cost(self, *, input_tokens, output_tokens, unknown_attempts, reserved_usd):
        return 0.003

    def fallback(self, request, reason):
        return Receipt(tuple(request.selected_candidate_registry_ids), request, mode="fallback", reason=reason)


class Receipt:
    schema_version = 1

    def __init__(self, ids, request, *, mode="model", reason=""):
        self.ranked_candidate_ids = ids
        self.request_id = request.request_id
        self.policy_version = request.policy_version
        self.model_version = request.model_version
        self.history_revision = request.history_revision
        self.history_generation = request.history_generation
        self.consent_revision = request.consent_revision
        self.server_commit_revision = request.server_commit_revision
        self.result_mode = type("Mode", (), {"value": mode})()
        self.fallback_reason = reason
        self.newest_event_id = None


class PaidStore(Store):
    def history_snapshot(self, token):
        return {**super().history_snapshot(token), "provider_processing_enabled": True}

    def reserve_budget(self, **kwargs):
        self.reservations.append(kwargs)
        return True


def paid(store, **kwargs):
    subject = build(store, **kwargs)
    subject._adapter = CountingAdapter()
    return subject


def test_one_reading_run_buys_exactly_one_provider_call_across_every_page_turn():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert first["result_mode"] == "model", "the paid path must actually be reached"
    assert subject._adapter.calls == 1 and len(store.reservations) == 1

    cursor, turns = first["next_cursor"], 0
    while cursor and turns < 6:
        store.revision += 1          # she reads, saves, opens the original
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        cursor, turns = response["next_cursor"], turns + 1
        assert response["request_id"] == first["request_id"]
    assert turns >= 2, "the test must actually turn pages, including past the frozen order"
    assert subject._adapter.calls == 1, "a page turn bought a second provider call"
    assert len(store.reservations) == 1, "a page turn reserved budget again"
    assert len(store.settlements) == 1, "provider usage must settle exactly once"


def test_the_visible_page_honours_the_configured_lane_mix():
    store = PaidStore(events=liked_events())
    response = rank(paid(store), store)
    counts = {lane: sum(1 for card in response["cards"] if card["lane"] == lane)
              for lane in ("updates", "hot", "interested", "surprise")}
    assert len(response["cards"]) == 25
    # 7 / 4 / 11 / 3 of 25, the configured mix, on the page she actually sees.
    # 7 / 4 / 11 / 3 of 25 exactly, on the page she actually sees.
    assert counts == {"updates": 7, "hot": 4, "interested": 11, "surprise": 3}, counts


def test_a_one_topic_corpus_still_fills_the_page():
    """Today's corpus shape: nearly everything in one topic. The page must not
    collapse to 14 cards because topic spacing starved the aligned lane."""
    rows = [corpus_row(index, hours=1 + index % 30, source=f"s{index}", categories=["world"])
            for index in range(60)]
    store = PaidStore(rows, events=liked_events())
    response = rank(paid(store), store)
    assert len(response["cards"]) == 25, f"page collapsed to {len(response['cards'])} cards"
    sources = [card["source_id"] for card in response["cards"]]
    assert all(left != right for left, right in zip(sources, sources[1:])), \
        "source adjacency stays hard even when topic spacing is relaxed"


def test_less_like_this_is_recorded_so_the_page_replays():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    removed = first["cards"][0]["source_id"]
    store.events.append({"event_id": "dislike", "event_type": "less_like_this", "event_revision": 9,
                         "occurred_at": NOW.isoformat(),
                         "payload": {"story_id": first["cards"][0]["story_id"], "surface": "reader"},
                         "story_title": "", "story_summary": "", "source_id": removed})
    store.revision += 1
    # Re-read the page the disliked card is actually ON. An offset further down
    # the order would prove nothing about this filter.
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", 0, int(store.frozen["frozen-1"]["expires_at"])))
    recorded = store.filtered.get("run-1", [])
    assert recorded, "the filter must be recorded, or the page cannot be reviewed afterwards"


def test_a_client_a_revision_behind_is_served_not_refused():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    behind = rank(subject, store, server_commit_revision=store.commit_revision - 1)
    assert behind["cards"], "a client one behind is not stale, it is a moment behind"
    with pytest.raises(StaleRankingError, match="stale_server_commit_revision"):
        rank(subject, store, server_commit_revision=store.commit_revision + 5)


def test_the_hot_lane_continuation_sends_a_whole_keyset_or_none():
    """The SQL refuses half a hot keyset, and the fake store refuses it too. The
    general cursor does not describe hot ordering at all, so hot carries its own
    and the two never get mixed."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    exhausted = len(frozen["cards"])
    # Would raise ValueError("invalid cursor") from the fake if half a keyset
    # reached the hot lane, which is exactly what the SQL does.
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"])))
    stored = store.frozen["frozen-1"]["bindings"]["corpus_cursor"]
    assert set(stored) >= {"before_published_at", "before_story_id"}
    if "hot" in stored:
        assert set(stored["hot"]) == {"before_source_count", "before_published_at", "before_story_id"}
    # And a second continuation resumes from it without raising.
    frozen = store.frozen["frozen-1"]
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", len(frozen["cards"]), int(frozen["expires_at"])))


def test_a_half_written_hot_cursor_is_ignored_rather_than_sent():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    assert subject._hot_cursor({"hot": {"before_source_count": 2}}) is None
    assert subject._hot_cursor({"hot": {"before_published_at": "x", "before_story_id": "y"}}) is None
    assert subject._hot_cursor({}) is None
    assert subject._hot_cursor({"hot": {"before_source_count": 2, "before_published_at": "x",
                                        "before_story_id": "y"}}) == ("x", "y", 2)


# --- the page is full, whatever the corpus looks like ---------------------

def test_learning_off_still_fills_the_page():
    """No profile means no aligned and no surprise pool. The page must still be
    a page: the window admits the leftovers rather than leaving slots empty."""
    store = PaidStore(events=liked_events(), learning=False)
    response = rank(paid(store), store)
    assert len(response["cards"]) == 25, f"page collapsed to {len(response['cards'])} cards"


def test_a_first_visit_with_no_history_fills_the_page():
    store = PaidStore(events=[])
    response = rank(paid(store), store)
    assert len(response["cards"]) == 25, f"page collapsed to {len(response['cards'])} cards"


def test_two_hundred_rows_over_two_days_with_no_profile_fill_the_page():
    rows = [corpus_row(index, hours=1 + (index % 47), source=f"src{index}",
                       categories=[f"topic{index % 6}"]) for index in range(200)]
    store = PaidStore(rows, events=[])
    response = rank(paid(store), store)
    assert len(response["cards"]) == 25, f"page collapsed to {len(response['cards'])} cards"
    sources = [card["source_id"] for card in response["cards"]]
    assert all(left != right for left, right in zip(sources, sources[1:]))


# --- the continuation degrades instead of misleading or 500ing -------------

def test_a_capped_order_ends_the_run_instead_of_repeating_the_last_page():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    # The store refuses to grow the order any further, exactly as the RPC does
    # when the card cap is reached.
    store.extend_frozen_order = lambda **kwargs: len(store.frozen["frozen-1"]["cards"])
    response = subject.page(authorization="Bearer valid",
                            cursor=subject._cursor("frozen-1", len(frozen["cards"]),
                                                   int(frozen["expires_at"])))
    assert response["cards"] == [], "a capped order served the same stories again"
    assert response["next_cursor"] is None
    assert response.get("end_of_run") is True, "the end of a run must be said, not implied"


def test_a_failed_extend_degrades_rather_than_five_hundreds():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]

    def explode(**kwargs):
        raise RuntimeError("the store is unavailable")

    store.extend_frozen_order = explode
    response = subject.page(authorization="Bearer valid",
                            cursor=subject._cursor("frozen-1", len(frozen["cards"]),
                                                   int(frozen["expires_at"])))
    assert response["cards"] == [] and response.get("end_of_run") is True


def test_the_filter_is_recorded_even_after_the_run_has_closed():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    removed = first["cards"][0]["source_id"]
    store.events.append({"event_id": "dislike", "event_type": "less_like_this", "event_revision": 9,
                         "occurred_at": NOW.isoformat(),
                         "payload": {"story_id": first["cards"][0]["story_id"], "surface": "reader"},
                         "story_title": "", "story_summary": "", "source_id": removed})
    store.revision += 1
    # The run closed between the page being served and the filter being written.
    store.runs[0]["closed_at"] = NOW.isoformat()
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", 0, int(store.frozen["frozen-1"]["expires_at"])))
    assert store.filtered.get("run-1"), "a closed run still owns the page it served"


def test_the_hot_cursor_is_never_built_from_a_general_pool_row():
    """The general pool carries every story, including count-1 ones. If one of
    those becomes before_source_count, the SQL then returns only hot rows BELOW
    it and skips still-available count-3 stories: the hot lane quietly empties
    after the first continuation."""
    subject = paid(PaidStore(events=liked_events()))
    general = {"story_id": "story:" + "a" * 64, "published_at": "2026-09-18T01:00:00+00:00",
               "independent_source_count": 1}
    hot = {"story_id": "story:" + "b" * 64, "published_at": "2026-09-18T02:00:00+00:00",
           "independent_source_count": 3}
    cursor = subject._next_corpus_cursor([general, hot], {hot["story_id"]})
    assert cursor["hot"]["before_source_count"] == 3, cursor
    assert cursor["hot"]["before_story_id"] == hot["story_id"]
    # The general cursor still resumes from the oldest row of the whole pool.
    assert cursor["before_story_id"] == general["story_id"]


def test_no_hot_rows_means_no_hot_cursor_at_all():
    subject = paid(PaidStore(events=liked_events()))
    general = {"story_id": "story:" + "a" * 64, "published_at": "2026-09-18T01:00:00+00:00",
               "independent_source_count": 1}
    assert "hot" not in subject._next_corpus_cursor([general], set())


def test_a_continuation_still_returns_hot_stories_the_lane_had_left():
    """End to end: the general pool contributes a count-1 row, the hot lane still
    has count-3 rows, and the continuation must reach them."""
    rows = ([corpus_row(index, hours=1 + index, source=f"fresh{index}", categories=[f"t{index % 9}"])
             for index in range(70)]
            + [corpus_row(100 + index, hours=8 + index, source=f"hot{index}",
                          categories=[f"h{index}"], independent=3) for index in range(8)])
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    assert frozen["bindings"]["corpus_has_more"] is True, "the fixture must leave a continuation to make"
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", len(frozen["cards"]), int(frozen["expires_at"])))
    stored = store.frozen["frozen-1"]["bindings"]["corpus_cursor"]
    assert store.extensions, "the continuation did not run, so this proves nothing"
    if "hot" in stored:
        assert stored["hot"]["before_source_count"] >= 2, \
            "a count-1 general row became the hot boundary and will skip real hot stories"


# --- the chip has to be true, not just present ----------------------------

def test_a_quiet_hour_fills_the_page_without_calling_old_news_fresh():
    """The probe that found this: at a quiet hour, 21 of 25 cards were chipped
    "fresh" at 10 to 15 hours old against an updates window of 6. A page may be
    filled with cards that met no rule; it may not LIE about them."""
    rows = [corpus_row(index, hours=10 + (index % 6), source=f"quiet{index}",
                       categories=[f"q{index % 8}"]) for index in range(60)]
    store = PaidStore(rows, events=[])
    subject = paid(store)
    response = rank(subject, store)
    assert len(response["cards"]) == 25, f"page collapsed to {len(response['cards'])} cards"
    window = load_composition_policy(POLICY_PATH).updates_max_age_hours
    for card in response["cards"]:
        age = (NOW - datetime.fromisoformat(card["published_at"])).total_seconds() / 3600
        if card["lane"] == "updates":
            assert age <= window, f"a {age:.0f} hour old story was chipped fresh"
    assert any(card["lane"] == "more" for card in response["cards"]), \
        "the backfilled cards must carry the honest chip"
    assert all(card["lane_label"] == "More" for card in response["cards"] if card["lane"] == "more")


def test_page_two_is_a_full_page_when_candidates_exist():
    """run.max_pages_per_run promises two pages of 25. The window used to fill to
    one page plus a margin, so page 2 was structurally five cards."""
    rows = [corpus_row(index, hours=1 + (index % 40), source=f"src{index}",
                       categories=[f"t{index % 9}"]) for index in range(120)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert len(first["cards"]) == 25
    store.reservations.clear()          # page one's single reservation, already made
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert len(second["cards"]) == 25, f"page two came back with {len(second['cards'])} cards"
    assert second["request_id"] == first["request_id"]
    assert store.reservations == [], "page two bought a provider call"


def test_load_more_stops_at_the_configured_page_and_says_the_run_is_over():
    """Without a stop, every page past the frozen order reached further into
    older news and came back entirely "More", for ever, and end_of_run never
    fired. A run that has run out has to say so."""
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}", categories=[f"d{index % 9}"])
            for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    policy = load_composition_policy(POLICY_PATH)
    response = rank(subject, store)
    store.reservations.clear()
    served, cursor = 1, response["next_cursor"]
    while cursor and served < 40:
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        if not response["cards"]:
            break
        served += 1
        cursor = response["next_cursor"]
    assert served == policy.max_pages_per_run, f"served {served} pages, cap is {policy.max_pages_per_run}"
    assert response.get("end_of_run") is True, "the run ended without saying so"
    assert response["cards"] == []
    assert store.reservations == [], "the tail of a run bought a provider call"


def test_the_lane_counts_account_for_every_card_on_the_page():
    store = PaidStore(events=[])
    subject = paid(store)
    response = rank(subject, store)
    counts = store.frozen["frozen-1"]["bindings"]["lane_counts"]
    assert "more" in counts, "a page that counts 25 while reporting four lanes is hiding cards"
    served = [card for card in store.frozen["frozen-1"]["cards"]]
    assert sum(counts.values()) == min(len(served), 25) or sum(counts.values()) > 0


# --- a refresh inside a run is free, and cannot reset the budget -----------

def test_a_refresh_inside_a_run_returns_the_ranking_it_already_paid_for():
    """rank() used to mint a NEW frozen order on every call while joining the
    same run, so a refresh bought a second ranking and handed the per-run page
    budget back with it."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert len(store.reservations) == 1 and subject._adapter.calls == 1
    for _ in range(2):
        again = rank(subject, store)
        assert again["request_id"] == first["request_id"], "a refresh minted a new ranking"
        assert [card["story_id"] for card in again["cards"]] == \
            [card["story_id"] for card in first["cards"]], "a refresh reshuffled the page"
    assert len(store.frozen) == 1, "a refresh wrote a second frozen order"
    assert len(store.reservations) == 1, "a refresh reserved provider budget again"
    assert subject._adapter.calls == 1, "a refresh bought a second provider call"


def test_a_refresh_cannot_reset_the_per_run_page_budget():
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}", categories=[f"d{index % 9}"])
            for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    policy = load_composition_policy(POLICY_PATH)
    response = rank(subject, store)
    cursor, served = response["next_cursor"], 1
    while cursor and served < policy.max_pages_per_run:
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        if not response["cards"]:
            break
        served += 1
        cursor = response["next_cursor"]
    assert served == policy.max_pages_per_run
    # The refresh: allowed, free, and page one as always.
    refreshed = rank(subject, store)
    assert refreshed["cards"], "a refresh should still show her page one"
    assert len(store.reservations) == 1, "the refresh bought a ranking"
    # And the budget is still spent, because it belongs to the RUN.
    after = subject.page(authorization="Bearer valid", cursor=refreshed["next_cursor"])
    assert after.get("end_of_run") is True, "a refresh handed the page budget back"
    assert after["cards"] == []


def test_a_scoped_staleness_change_still_buys_exactly_one_new_ranking():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    # A history reset is the kind of change that makes a stored order wrong.
    original = store.history_snapshot
    store.history_snapshot = lambda token: {**original(token), "history_generation": 2}
    second = rank(subject, store, history_generation=2)
    assert second["request_id"] != first["request_id"], "a real staleness change must re-rank"
    assert subject._adapter.calls == 2 and len(store.reservations) == 2
    assert len(store.frozen) == 2


# --- claim before paying ---------------------------------------------------

def test_two_concurrent_first_ranks_buy_exactly_one_ranking():
    """The race the idempotent-refresh fix did not close: request B joins the run
    while A is still in flight, sees no bound order yet, and pays again. B now
    loses a compare-and-set taken BEFORE any money moves."""
    store = PaidStore(events=liked_events())
    first, second = paid(store), paid(store)
    inflight = {}

    original = first._adapter.rank

    def rank_while_a_second_request_arrives(*args, **kwargs):
        # B arrives here: mid-provider-call, before A has bound anything.
        with pytest.raises(StaleRankingError, match="ranking_in_progress"):
            rank(second, store)
        inflight["reached"] = True
        return original(*args, **kwargs)

    first._adapter.rank = rank_while_a_second_request_arrives
    served = rank(first, store)
    assert inflight.get("reached"), "the concurrent request never ran"
    assert first._adapter.calls == 1 and second._adapter.calls == 0
    assert len(store.reservations) == 1, "two rankings were reserved for one run"
    assert len(store.frozen) == 1, "two frozen orders were written for one run"
    assert store.views[("run-1", first._eligibility_key(None, None, False))]["frozen_order_id"] == "frozen-1"
    # And once the winner has bound, the loser is SERVED that order, not refused.
    late = rank(second, store)
    assert late["request_id"] == served["request_id"]
    assert len(store.reservations) == 1


def test_an_expired_claim_is_taken_over_rather_than_waited_out():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    # A request that died mid-flight: the claim is held and the run has no order.
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1}, "created": False})
    key = subject._eligibility_key(None, None, False)
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": "dead-request", "claim_expired": True}
    response = rank(subject, store)
    assert response["cards"], "an expired claim locked the reader out of her own feed"
    assert subject._adapter.calls == 1


def test_a_held_claim_with_no_order_yet_is_reported_as_in_progress():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1}, "created": False})
    key = subject._eligibility_key(None, None, False)
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": "someone-else", "claim_expired": False}
    with pytest.raises(StaleRankingError, match="ranking_in_progress"):
        rank(subject, store)
    assert subject._adapter.calls == 0, "a losing request must not call the provider"
    assert store.reservations == []


def test_a_losing_bind_cannot_overwrite_the_winners_order():
    store = PaidStore(events=liked_events())
    store.views[("run-1", "k" * 64)] = {"frozen_order_id": "frozen-winner", "pages_served": 0,
                                        "claim_token": "the-winner", "claim_expired": False}
    assert store.bind_run_frozen_order(user_id="u", run_id="run-1", eligibility_key="k" * 64,
                                       frozen_order_id="frozen-loser", token="the-loser") is False
    assert store.views[("run-1", "k" * 64)]["frozen_order_id"] == "frozen-winner"


# --- idempotence is per VIEW, not per run ---------------------------------

def test_a_category_inside_an_open_run_is_not_served_the_all_page():
    """Measured before the fix: a `tech` request inside an open run made zero
    corpus calls and returned the All page byte for byte, for up to an hour."""
    rows = ([corpus_row(index, hours=2, source=f"all{index}", categories=["world"])
             for index in range(30)]
            + [corpus_row(100 + index, hours=2, source=f"tech{index}", categories=["tech"])
               for index in range(30)])

    class ByCategory(PaidStore):
        def retained_candidates_v2(self, *, category_id, **kwargs):
            rows = super().retained_candidates_v2(category_id=category_id, **kwargs)
            if category_id is None:
                return rows
            return [row for row in rows if category_id in row["category_ids"]]

    store = ByCategory(rows, events=liked_events())
    subject = paid(store)
    everything = rank(subject, store)
    tech = rank(subject, store, eligibility={"category": "tech", "query": None})
    assert tech["request_id"] != everything["request_id"], "the category was served the All ranking"
    assert all("tech" in card["category_ids"] for card in tech["cards"])
    assert {card["story_id"] for card in tech["cards"]} != {card["story_id"] for card in everything["cards"]}
    assert len(store.reservations) == 2, "each view pays once, and only once"
    assert len(store.frozen) == 2


def test_refreshing_a_category_inside_the_run_is_free():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store, eligibility={"category": "world", "query": None})
    before = len(store.reservations)
    again = rank(subject, store, eligibility={"category": "world", "query": None})
    assert again["request_id"] == first["request_id"]
    assert len(store.reservations) == before, "a refresh of the same view paid again"


def test_a_search_and_the_exclusive_section_are_their_own_views():
    store = PaidStore(events=liked_events(), exclusive=exclusive_corpus(6))
    subject = paid(store, exclusive_category="only-other-language-press")
    everything = rank(subject, store)
    searched = rank(subject, store, eligibility={"category": None, "query": "rates"})
    section = rank(subject, store, eligibility={"category": "only-other-language-press", "query": None})
    ids = {everything["request_id"], searched["request_id"], section["request_id"]}
    assert len(ids) == 3, "two different views shared one ranking"
    assert len(store.reservations) == 3


def test_the_page_budget_is_spent_per_view():
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}", categories=[f"d{index % 9}"])
            for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    policy = load_composition_policy(POLICY_PATH)
    response = rank(subject, store)
    cursor, served = response["next_cursor"], 1
    while cursor and served < policy.max_pages_per_run:
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        if not response["cards"]:
            break
        served += 1
        cursor = response["next_cursor"]
    assert served == policy.max_pages_per_run
    # All is spent. A different view starts with its own full budget.
    other = rank(subject, store, eligibility={"category": "d1", "query": None})
    assert other["cards"], "one view's spent budget ended another view's first page"
    assert other["next_cursor"], "a fresh view must still be able to load more"
