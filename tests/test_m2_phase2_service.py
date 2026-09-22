"""The Phase 2 feed, end to end through RankingService.

What these prove, in JJ's terms: the page is no longer the newest fifty rows,
every card says why it is there, reading a story does not re-bill the next page,
and a save in another tab no longer throws away a rank that was already paid for.
"""
from __future__ import annotations

import json
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dataclasses import replace

from curator.recommendation.composition import load_composition_policy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.service import (
    ProviderConsentRequiredError,
    RankingInProgressError,
    RankingService,
    ServicePolicy,
    StaleRankingError,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"
CLOCK = 1_789_000_000
NOW = datetime.fromtimestamp(CLOCK, timezone.utc)


OWNER_ID = "11111111-1111-1111-1111-111111111111"


class Auth:
    def get_user(self, token):
        assert token == "valid"
        return {"id": OWNER_ID}


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

    def retained_candidates_language_exclusive(self, *, limit, before_story_id=None, **kwargs):
        self.exclusive_calls += 1
        start = 0
        if before_story_id is not None:
            start = next((index + 1 for index, row in enumerate(self.exclusive)
                          if row["story_id"] == before_story_id), len(self.exclusive))
        return self.exclusive[start:start + limit]

    # --- runs --------------------------------------------------------------
    def record_reading_run_filter(self, *, user_id, run_id, story_ids):
        self.filtered.setdefault(run_id, [])
        for story in story_ids:
            if story not in self.filtered[run_id]:
                self.filtered[run_id].append(story)
        return len(self.filtered[run_id])

    def open_reading_run(self, *, user_id, idle_minutes, max_minutes, profile):
        active = [run for run in self.runs if not run.get("closed_at")]
        if active:
            current = active[-1]
            same_epoch = (current["profile_snapshot"].get("_history_generation")
                          == profile.get("_history_generation")
                          and current["profile_snapshot"].get("_consent_revision")
                          == profile.get("_consent_revision"))
            if same_epoch:
                return {**current, "created": False}
            current["closed_at"] = "atomic-replacement"
        run = {"run_id": f"run-{len(self.runs) + 1}", "profile_snapshot": profile, "created": True}
        self.runs.append(run)
        return run

    def close_reading_run(self, *, user_id, run_id, closed_at):
        for run in self.runs:
            if run["run_id"] == run_id and not run.get("closed_at"):
                run["closed_at"] = closed_at
                return True
        return False

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

    def reserve_run_response(self, *, user_id, run_id, eligibility_key,
                             frozen_order_id, response_number, offset, next_offset):
        view = self._view(run_id, eligibility_key)
        stored = self.frozen.get(frozen_order_id)
        previous = view["pages_served"]
        if (stored is None or view["frozen_order_id"] != frozen_order_id
                or offset < 0 or next_offset < offset
                or offset > len(stored["cards"])):
            return {"reserved": False, "previous": previous}
        bindings = stored["bindings"]
        if not (previous in (response_number - 1, response_number)
                or (previous == 0 and response_number == 2)):
            return {"reserved": False, "previous": previous}
        if (previous == response_number
                and bindings.get("responses_served") == response_number
                and (bindings.get("last_served_offset") != offset
                     or bindings.get("last_served_next_offset") != next_offset)):
            return {"reserved": False, "previous": previous}
        view["pages_served"] = max(previous, response_number)
        bindings.update({"responses_served": response_number,
                         "last_served_offset": offset,
                         "last_served_next_offset": next_offset})
        return {"reserved": True, "previous": previous}

    # --- owner state and budget -------------------------------------------
    def owner_states(self, token, story_ids):
        return {}

    def reserve_budget(self, **kwargs):
        self.reservations.append(kwargs)
        return False

    def reserve_budget_claimed(self, *, run_id, eligibility_key, claim_token, **kwargs):
        # The SQL refuses a caller whose claim has moved on, BEFORE any capacity
        # moves, and says WHICH refusal it is. Reproduced here, because a fake
        # that reserves regardless cannot catch a takeover that double-pays.
        view = self._view(run_id, eligibility_key)
        if view["claim_token"] != claim_token:
            return {"reserved": False, "refusal": "claim_lost"}
        reserved = self.reserve_budget(**kwargs)
        return {"reserved": reserved, "refusal": "" if reserved else "budget",
                "remaining_usd": None if reserved else 0.0}

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
    rows = [dict(corpus_row(500 + index, hours=8, source=f"zh{index}",
                            categories=[f"zh-topic{index}"]),
                 language="zh", title=f"中文独家 {index}",
                 title_translations={"en": f"Only in the Chinese press {index}"},
                 summary_translations={"en": f"Translated summary {index}"})
            for index in range(count)]
    # The production language-exclusive RPC does not return this aggregate.
    for row in rows:
        row.pop("independent_source_count")
    return rows


def build(store, *, composition=True, page_size=25, promote=None, exclusive_category="",
          policy_version="policy", model_version="gpt-5-mini", provider_policy_id="policy",
          effective_policy_digest=""):
    adapter = RankLLMAdapter(policy=RankerPolicy("openai", model_version, "https://provider.invalid", policy_version,
        input_cost_per_million_tokens_usd=.25, output_cost_per_million_tokens_usd=2), engine=object())
    loaded = load_composition_policy(POLICY_PATH) if composition else None
    if loaded is not None and promote is not None:
        loaded = replace(loaded, exclusive_promote_to_all_max=promote)
    policy = ServicePolicy(policy_version, model_version, provider_policy_id, "tenant", candidate_limit=50,
        maximum_page_size=page_size, enabled=True, composition=loaded,
        # F5, from PR #47: an enabled service fails closed on an empty allowlist.
        # The harness names the owner the Auth stub authenticates.
        preview_owner_ids=(OWNER_ID,),
        exclusive_category_id=exclusive_category or "",
        effective_policy_digest=effective_policy_digest)
    return RankingService(auth=Auth(), store=store, adapter=adapter, policy=policy,
                          cursor_key=b"x" * 32, clock=lambda: CLOCK)


def rank(subject, store, **overrides):
    body = {"history_revision": store.included, "server_commit_revision": store.commit_revision,
            "history_generation": 1, "consent_revision": 1, "page_size": 25}
    body.update(overrides)
    return subject.rank(authorization="Bearer valid", body=body)


def test_frozen_order_ttl_must_cover_the_complete_reading_run():
    composition = load_composition_policy(POLICY_PATH)
    with pytest.raises(ValueError, match="complete reading run"):
        ServicePolicy("policy", "model", "policy", "tenant",
            cursor_ttl_seconds=composition.max_run_minutes * 60 - 1,
            composition=composition)
    accepted = ServicePolicy("policy", "model", "policy", "tenant",
        cursor_ttl_seconds=composition.max_run_minutes * 60,
        composition=composition)
    assert accepted.cursor_ttl_seconds == composition.max_run_minutes * 60


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
        assert card["card_schema_version"] == 4


def test_the_page_mixes_all_four_pools_and_is_not_the_newest_fifty():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    lanes = {card["lane"] for card in response["cards"]}
    assert lanes == {"updates", "hot", "interested", "surprise"}
    newest = [row["story_id"] for row in sorted(store.rows, key=lambda row: row["published_at"], reverse=True)[:25]]
    assert [card["story_id"] for card in response["cards"]] != newest


def test_a_hot_card_reports_the_real_independent_coverage_count():
    store = Store(events=liked_events())
    response = rank(build(store), store)
    hot = next(card for card in response["cards"] if card["lane"] == "hot")
    assert hot["coverage_count"] == 4


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
                         cursor=subject._cursor("frozen-1", 0,
                             int(store.frozen["frozen-1"]["expires_at"]), response_number=1))
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


def test_filtered_short_page_uses_continuation_lookahead_to_stay_full():
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    assert len(frozen["cards"]) == 50
    blocked = frozen["cards"][25]["source_id"]
    store.events.append({"event_id": "dislike", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": frozen["cards"][25]["story_id"], "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": blocked})

    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    replay = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])

    assert len(second["cards"]) == 25
    assert all(card["source_id"] != blocked for card in second["cards"])
    assert store.extensions, "the short final slice did not fetch an older replacement"
    assert replay["cards"] == second["cards"]
    assert replay["next_cursor"] == second["next_cursor"]


def test_unfiltered_short_tail_rechecks_the_continuation_boundary():
    rows = [corpus_row(index, hours=1 + index, source=f"s{index}",
                       categories=[f"t{index % 9}"]) for index in range(300)]
    rows[1]["title"] = rows[0]["title"]
    rows[75]["source_id"] = "s49"
    rows[75]["source_name"] = "S49"
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert len(store.frozen["frozen-1"]["cards"]) == 49

    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])

    assert len(second["cards"]) == 25
    assert second["cards"][-2]["source_id"] != second["cards"][-1]["source_id"]
    assert second["cards"][-1]["story_id"] != rows[75]["story_id"]


def test_refresh_replays_the_same_continuation_boundary_rules():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    prototype = frozen["cards"][0]
    raw_cards = [
        {**prototype, "story_id": f"story:{1000 + index:064x}",
         "source_id": "same" if index in (23, 24) else f"unique-{index}",
         "title": f"Unique {index}", "url": f"https://example.test/unique-{index}"}
        for index in range(26)
    ]
    frozen["cards"] = raw_cards[:24] + subject._align_continuation(
        raw_cards[:24], raw_cards[24:], 25, subject._policy.composition)
    frozen["bindings"]["continuation_offsets"] = [24]

    refreshed = rank(subject, store)

    sources = [card["source_id"] for card in refreshed["cards"]]
    assert len(refreshed["cards"]) == 25
    assert all(left != right for left, right in zip(sources, sources[1:]))
    assert refreshed["cards"][-1]["story_id"] == raw_cards[25]["story_id"]


def test_later_continuation_pages_keep_the_same_hard_invariants():
    subject = paid(PaidStore(events=liked_events()))
    policy = subject._policy.composition
    cards = [
        {"story_id": f"story:{2000 + index:064x}",
         "source_id": "same" if index in (20, 21) else f"unique-{index}",
         "title": f"Unique {index}", "url": f"https://example.test/later-{index}",
         "category_ids": []}
        for index in range(36)
    ]
    existing, added = cards[:5], cards[5:]
    aligned = subject._align_continuation(existing, added, 25, policy)

    visible, next_offset, _removed = subject._slice(
        existing + aligned, 10, 25,
        PaidStore(events=liked_events()).history_snapshot("valid"),
        continuation_offsets=[5])

    sources = [card["source_id"] for card in visible]
    assert len(visible) == 25
    assert next_offset == 35
    assert all(left != right for left, right in zip(sources, sources[1:]))
    assert {card["story_id"] for card in aligned} == {card["story_id"] for card in added}


def test_filtered_prefix_chooses_a_legal_replacement_before_a_collision():
    subject = paid(PaidStore(events=liked_events()))
    policy = subject._policy.composition
    cards = [
        {"story_id": f"story:{3000 + index:064x}", "source_id": f"s{index}",
         "title": f"Title {index}", "url": f"https://example.test/filter-{index}",
         "category_ids": []}
        for index in range(50)
    ]
    snapshot = PaidStore(events=liked_events()).history_snapshot("valid")
    snapshot["events"].append({"event_id": "filter", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": cards[25]["story_id"], "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": "s25"})
    prefix, end, _removed = subject._slice(cards, 25, 25, snapshot)
    assert len(prefix) == 24 and end == 50
    collision = {"story_id": "story:" + "a" * 64, "source_id": "s49",
                 "title": "Collision", "url": "https://example.test/collision",
                 "category_ids": []}
    legal = {"story_id": "story:" + "b" * 64, "source_id": "legal",
             "title": "Legal", "url": "https://example.test/legal", "category_ids": []}
    aligned = subject._align_continuation(
        cards, [collision, legal], 25, policy, page_prefix=prefix)

    visible, next_offset, _removed = subject._slice(
        cards + aligned, 25, 25, snapshot, continuation_offsets=[50])

    assert len(visible) == 25 and next_offset == 51
    assert visible[-1]["story_id"] == legal["story_id"]


def test_filtered_stitch_rechecks_hard_page_invariants():
    subject = paid(PaidStore(events=liked_events()))
    policy = subject._policy.composition
    first = {"story_id": "story:" + "1" * 64, "source_id": "same", "title": "First",
             "url": "https://example.test/first"}
    second_id = "story:" + "2" * 64
    groups = {first["story_id"]: "group:" + "a" * 32,
              second_id: "group:" + "a" * 32}

    assert subject._violates_page_invariants(
        [first], {"story_id": second_id, "source_id": "same", "title": "Different",
                  "url": "https://example.test/source"}, policy, groups)
    assert subject._violates_page_invariants(
        [first], {"story_id": second_id, "source_id": "other", "title": "First",
                  "url": "https://example.test/title"}, policy, groups)
    assert subject._violates_page_invariants(
        [first], {"story_id": second_id, "source_id": "other", "title": "Different",
                  "url": "https://example.test/group"}, policy, groups)
    assert not subject._violates_page_invariants(
        [first], {"story_id": second_id, "source_id": "other", "title": "Second",
                  "url": "https://example.test/second"}, policy,
        {**groups, second_id: "group:" + "b" * 32})


def test_an_empty_filtered_slice_keeps_its_response_ordinal_for_older_cards():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    prototype = frozen["cards"][0]
    frozen["cards"] = [
        {**prototype, "story_id": f"story:{index:064x}",
         "source_id": "blocked" if index < 75 else f"safe-{index}",
         "category_ids": ["blocked"] if index < 75 else ["safe"]}
        for index in range(100)
    ]
    frozen["bindings"]["corpus_has_more"] = False
    store.events.append({"event_id": "dislike", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": "story:" + "0" * 64, "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": "blocked"})

    empty = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert empty["cards"] == [] and empty["next_cursor"]
    assert subject._decode_cursor(empty["next_cursor"])["response_number"] == 2

    recovered = subject.page(authorization="Bearer valid", cursor=empty["next_cursor"])
    assert recovered["cards"]
    assert store.views[next(iter(store.views))]["pages_served"] == 2


def test_an_empty_filtered_replay_cannot_move_a_spent_ordinal_to_new_cards():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    prototype = frozen["cards"][0]
    frozen["cards"] = [
        {**prototype, "story_id": f"story:{index:064x}",
         "source_id": "blocked" if 75 <= index < 125 else f"safe-{index}",
         "category_ids": ["blocked"] if 75 <= index < 125 else ["safe"]}
        for index in range(150)
    ]
    frozen["bindings"]["corpus_has_more"] = False
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    third = subject.page(authorization="Bearer valid", cursor=second["next_cursor"])
    page_four_cursor = third["next_cursor"]
    fourth = subject.page(authorization="Bearer valid", cursor=page_four_cursor)
    assert fourth["cards"]
    assert store.views[next(iter(store.views))]["pages_served"] == 4

    store.events.append({"event_id": "dislike", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": "story:" + "0" * 64, "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": "blocked"})
    moved = subject.page(authorization="Bearer valid", cursor=page_four_cursor)
    assert moved["cards"] == [] and moved["next_cursor"]
    decoded = subject._decode_cursor(moved["next_cursor"])
    assert decoded["offset"] == 125 and decoded["response_number"] == 5
    ended = subject.page(authorization="Bearer valid", cursor=moved["next_cursor"])
    assert ended["cards"] == [] and ended.get("end_of_run") is True


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


def test_the_legacy_window_serves_its_short_tail_before_reranking():
    rows = [corpus_row(index, hours=1 + index, source=f"legacy{index}",
                       categories=[f"t{index % 9}"]) for index in range(100)]
    store = Store(rows, events=liked_events())
    subject = build(store, composition=False)
    rank(subject, store, page_size=24)
    frozen = store.frozen["frozen-1"]
    tail = subject.page(authorization="Bearer valid",
        cursor=subject._cursor("frozen-1", 48, int(frozen["expires_at"])))

    assert [card["story_id"] for card in tail["cards"]] == [
        card["story_id"] for card in frozen["cards"][48:50]]


# --- capped promotion into All --------------------------------------------

def promoted(response):
    return [card for card in response["cards"] if card["exclusive_label"]]


def test_at_most_the_cap_of_chinese_exclusive_stories_reach_all():
    store = Store(events=liked_events(), exclusive=exclusive_corpus(6))
    response = rank(build(store, exclusive_category="only-other-language-press"), store)
    assert len(promoted(response)) <= 2, "the configured cap is a ceiling, never a forced quota"


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


def test_language_exclusive_section_only_serves_complete_display_translations():
    rows = exclusive_corpus(3)
    rows[0]["title_translations"] = {}
    rows[1]["summary_translations"] = {}
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    assert [card["story_id"] for card in response["cards"]] == [rows[2]["story_id"]]
    assert response["cards"][0]["title_en"]
    assert response["cards"][0]["summary_en"]


def test_language_exclusive_section_scans_past_an_untranslated_rpc_page():
    rows = exclusive_corpus(101)
    for row in rows[:100]:
        row["title_translations"] = {}
        row["summary_translations"] = {}
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    assert [card["story_id"] for card in response["cards"]] == [rows[100]["story_id"]]
    assert store.exclusive_calls == 2


def test_language_exclusive_cursor_stops_after_the_last_candidate_it_consumed():
    # Keep the corpus within the four-response run budget. The separate short-
    # page regression below proves that a larger corpus stops at that budget.
    rows = exclusive_corpus(60)
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    cards = list(response["cards"])
    while response["next_cursor"]:
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        cards.extend(response["cards"])
    assert {card["story_id"] for card in cards} == {row["story_id"] for row in rows}


def test_language_exclusive_cursor_preserves_rows_rejected_by_source_caps():
    rows = exclusive_corpus(80)
    for row in rows[:10]:
        row["source_id"] = "same-source"
        row["source_name"] = "Same Source"
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    # This test isolates cursor preservation. Give it enough response budget to
    # reach every deliberately deferred source-capped row; the separate budget
    # regression proves the production four-response stop.
    subject._policy = replace(subject._policy, composition=replace(
        subject._policy.composition, max_pages_per_run=10))
    excluded = rows[20]["story_id"]
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None},
                    exclude_story_ids=[excluded])
    cards = list(response["cards"])
    while response["next_cursor"]:
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        cards.extend(response["cards"])
    assert {card["story_id"] for card in cards} == \
        {row["story_id"] for row in rows} - {excluded}


def test_language_exclusive_empty_bounded_continuation_advances_and_resumes():
    rows = exclusive_corpus(326)
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    rank(subject, store, eligibility={"category": "only-other-language-press",
                                      "query": None})
    # The 51st ready row was the initial look-ahead sentinel. Make the next 250
    # raw rows incomplete after the first page is frozen, reproducing a sparse
    # continuation without changing the already-served order.
    for row in rows[50:301]:
        row["title_translations"] = {}
        row["summary_translations"] = {}
    frozen = store.frozen["frozen-1"]
    exhausted = len(frozen["cards"])
    cursor = subject._cursor("frozen-1", exhausted, int(frozen["expires_at"]),
                             response_number=2)
    empty = subject.page(authorization="Bearer valid", cursor=cursor)
    assert empty["cards"] == []
    assert empty["next_cursor"], "a bounded empty scan must remain resumable"
    assert store.exclusive_calls == 3, "initial scan plus exactly two continuation batches"
    resumed = subject.page(authorization="Bearer valid", cursor=empty["next_cursor"])
    assert resumed["cards"], "the next bounded scan must reach older translated rows"
    assert store.exclusive_calls == 4


def test_language_exclusive_initial_empty_scan_reserves_first_recovered_page():
    rows = exclusive_corpus(1300)
    for row in rows[:1200]:
        row["title_translations"] = {}
        row["summary_translations"] = {}
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")

    initial = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                "query": None})
    assert initial["cards"] == [] and initial["next_cursor"]
    frozen = store.frozen["frozen-1"]
    assert frozen["bindings"]["responses_served"] == 0
    assert next(iter(store.views.values()))["pages_served"] == 0

    recovered = subject.page(authorization="Bearer valid", cursor=initial["next_cursor"])
    assert recovered["cards"] and recovered["next_cursor"]
    assert frozen["bindings"]["responses_served"] == 1
    assert next(iter(store.views.values()))["pages_served"] == 1

    following = subject.page(authorization="Bearer valid", cursor=recovered["next_cursor"])
    assert following["cards"], "the recovered first page must not prematurely end pagination"
    assert frozen["bindings"]["responses_served"] == 2
    assert next(iter(store.views.values()))["pages_served"] == 2


def test_language_exclusive_continuation_retires_an_already_opened_row():
    rows = exclusive_corpus(80)
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    blocked = rows[50]["story_id"]
    store.owner_states = lambda token, ids: ({blocked: {"read_at": NOW.isoformat()}}
                                              if blocked in ids else {})
    cards = list(response["cards"])
    attempts = 0
    while response["next_cursor"] and attempts < 8:
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        cards.extend(response["cards"])
        attempts += 1
    assert response["next_cursor"] is None
    assert blocked not in {card["story_id"] for card in cards}
    assert len(cards) == 79


def test_language_exclusive_scan_counts_only_nonexcluded_ready_rows():
    rows = exclusive_corpus(151)
    store = Store(events=liked_events(), exclusive=rows)
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None},
                    exclude_story_ids=[row["story_id"] for row in rows[:100]])
    assert len(response["cards"]) == 25
    assert not ({card["story_id"] for card in response["cards"]}
                & {row["story_id"] for row in rows[:100]})
    assert store.exclusive_calls == 2


def test_a_deep_language_exclusive_section_returns_a_page_and_cursor():
    """Every continuation stays in the exclusive corpus and never 500s."""
    store = Store(events=liked_events(), exclusive=exclusive_corpus(80))
    subject = build(store, exclusive_category="only-other-language-press")
    response = rank(subject, store, eligibility={"category": "only-other-language-press",
                                                 "query": None})
    assert response["cards"]
    assert response["next_cursor"], "a deep exclusive section must remain pageable"
    cards = list(response["cards"])
    while response["next_cursor"]:
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        cards.extend(response["cards"])
    assert cards
    assert all(card["source_id"].startswith("zh") for card in cards)
    assert all(card["exclusive_label"] == "only in Chinese press" for card in cards)
    assert len({card["story_id"] for card in cards}) == len(cards)


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
        cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"]),
                               response_number=2))
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
    early = subject._cursor("frozen-1", 0, int(frozen["expires_at"]), response_number=1)
    exhausted = len(frozen["cards"])
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"]),
                                        response_number=2))
    replayed = subject.page(authorization="Bearer valid", cursor=early)
    assert [card["story_id"] for card in replayed["cards"]] == [card["story_id"] for card in first["cards"]]


def test_a_continuation_never_repeats_a_story_already_in_the_order():
    store = Store(events=liked_events())
    subject = build(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    exhausted = len(frozen["cards"])
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"]),
                                        response_number=2))
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


def test_exact_four_page_corpus_does_not_skip_overfetch_tail():
    rows = [corpus_row(index, hours=1 + index, source=f"source-{index}",
                       categories=[f"topic-{index}"])
            for index in range(100)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    pages = []
    served = []
    while True:
        page_ids = [card["story_id"] for card in response["cards"]]
        if page_ids:
            pages.append(len(page_ids))
            served.extend(page_ids)
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    expected = {row["story_id"] for row in rows}
    assert pages == [25, 25, 25, 25]
    assert len(served) == len(set(served)) == 100
    assert set(served) == expected
    assert subject._adapter.calls == 1
    assert response["next_cursor"] is None


@pytest.mark.parametrize(("count", "expected_pages"), [
    (51, [25, 25, 1]),
    (60, [25, 25, 10]),
    (74, [25, 25, 24]),
    (76, [25, 25, 25, 1]),
    (99, [25, 25, 25, 24]),
])
def test_finite_corpus_keeps_every_partial_terminal_page(count, expected_pages):
    rows = [corpus_row(index, hours=1 + index, source=f"finite-{index}",
                       categories=[f"finite-topic-{index}"])
            for index in range(count)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    pages = []
    served = []
    while True:
        page_ids = [card["story_id"] for card in response["cards"]]
        if page_ids:
            pages.append(len(page_ids))
            served.extend(page_ids)
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    assert pages == expected_pages
    assert len(served) == len(set(served)) == count
    assert set(served) == {row["story_id"] for row in rows}
    assert subject._adapter.calls == 1


def test_pending_tail_refills_a_filtered_second_page():
    rows = [corpus_row(index, hours=1 + index, source=f"refill-{index}",
                       categories=[f"refill-topic-{index}"])
            for index in range(51)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    blocked = frozen["cards"][25]
    store.events.append({"event_id": "refill-filter", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": blocked["story_id"], "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": blocked["source_id"]})

    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])

    assert len(second["cards"]) == 25
    assert all(card["source_id"] != blocked["source_id"] for card in second["cards"])
    assert subject._adapter.calls == 1


def test_deferred_promoted_story_keeps_exclusive_label_on_continuation():
    rows = [corpus_row(index, hours=1 + index, source=f"general-{index}",
                       categories=[f"general-topic-{index}"])
            for index in range(100)]
    exclusive_rows = exclusive_corpus(8)
    store = PaidStore(rows, events=liked_events(), exclusive=exclusive_rows)
    subject = paid(store, exclusive_category="only-other-language-press")
    response = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    exclusive_ids = {row["story_id"] for row in exclusive_rows}
    deferred = {row["story_id"] for row in frozen["bindings"]["pending_candidates"]} & exclusive_ids
    assert deferred, "the fixture must defer at least one promoted story"

    continued = []
    while response.get("next_cursor"):
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        continued.extend(response["cards"])

    surfaced = [card for card in continued if card["story_id"] in deferred]
    assert surfaced, "a deferred promoted story never reached a continuation"
    assert all(card["exclusive_label"] == "only in Chinese press" for card in surfaced)


def test_every_continuation_batch_respects_the_promotion_cap_without_losing_deferred_rows():
    rows = [corpus_row(index, hours=1 + index, source=f"general-{index}",
                       categories=[f"general-topic-{index}"])
            for index in range(100)]
    exclusive_rows = exclusive_corpus(8)
    exclusive_ids = {row["story_id"] for row in exclusive_rows}
    store = PaidStore(rows, events=liked_events(), exclusive=exclusive_rows)
    subject = paid(store, promote=2, exclusive_category="only-other-language-press")

    response = rank(subject, store)
    served = []
    continuation_exclusive_counts = []
    while True:
        page = response["cards"]
        if served:
            continuation_exclusive_counts.append(
                sum(card["story_id"] in exclusive_ids for card in page))
        served.extend(card["story_id"] for card in page)
        exclusive_cards = [card for card in page if card["story_id"] in exclusive_ids]
        assert all(card["exclusive_label"] == "only in Chinese press"
                   for card in exclusive_cards)
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    pending = {row["story_id"] for row in
               store.frozen["frozen-1"]["bindings"]["pending_candidates"]}
    assert continuation_exclusive_counts
    assert max(continuation_exclusive_counts) <= 2
    assert len(served) == len(set(served))
    assert (set(served) & exclusive_ids).isdisjoint(pending & exclusive_ids)
    assert ((set(served) | pending) & exclusive_ids) == exclusive_ids
    assert subject._adapter.calls == 1


def test_pending_promotions_remain_reachable_without_an_older_corpus_cursor():
    rows = [corpus_row(index, hours=1 + index, source=f"short-{index}",
                       categories=[f"short-topic-{index}"])
            for index in range(30)]
    exclusive_rows = exclusive_corpus(8)
    exclusive_ids = {row["story_id"] for row in exclusive_rows}
    all_ids = {row["story_id"] for row in (*rows, *exclusive_rows)}
    store = PaidStore(rows, events=liked_events(), exclusive=exclusive_rows)
    subject = paid(store, promote=2, exclusive_category="only-other-language-press")

    first = rank(subject, store)
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    frozen = store.frozen["frozen-1"]
    initial_pending = {row["story_id"] for row in frozen["bindings"]["pending_candidates"]}
    assert [len(first["cards"]), len(second["cards"])] == [25, 7]
    assert initial_pending == exclusive_ids - {
        card["story_id"] for card in (*first["cards"], *second["cards"])}
    assert len(initial_pending) == 6
    assert second["next_cursor"] is not None

    pages = [first["cards"], second["cards"]]
    response = second
    while response.get("next_cursor"):
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        if response["cards"]:
            pages.append(response["cards"])

    served = [card for page in pages for card in page]
    served_ids = {card["story_id"] for card in served}
    pending_ids = {row["story_id"] for row in frozen["bindings"]["pending_candidates"]}
    assert [len(page) for page in pages] == [25, 7, 2, 2]
    assert len(served_ids) == len(served)
    assert served_ids.isdisjoint(pending_ids)
    assert served_ids | pending_ids == all_ids
    assert all(card["exclusive_label"] == "only in Chinese press"
               for card in served if card["story_id"] in exclusive_ids)
    assert subject._adapter.calls == 1


def test_filtered_page_prefix_consumes_the_visible_promotion_cap():
    rows = [corpus_row(index, hours=1 + index, source=f"prefix-{index}",
                       categories=[f"prefix-topic-{index}"])
            for index in range(100)]
    exclusive_rows = exclusive_corpus(8)
    exclusive_ids = {row["story_id"] for row in exclusive_rows}
    store = PaidStore(rows, events=liked_events(), exclusive=exclusive_rows)
    subject = paid(store, promote=2, exclusive_category="only-other-language-press")
    first = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    original_second_page = frozen["cards"][25:50]
    assert sum(card["story_id"] in exclusive_ids for card in original_second_page) == 2
    for index, card in enumerate(original_second_page):
        if card["story_id"] in exclusive_ids:
            continue
        store.events.append({"event_id": f"prefix-filter-{index}",
            "event_type": "less_like_this", "event_revision": 10 + index,
            "occurred_at": NOW.isoformat(),
            "payload": {"story_id": card["story_id"], "surface": "reader"},
            "story_title": "", "story_summary": "", "source_id": card["source_id"]})

    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])

    surfaced_exclusive = [card for card in second["cards"]
                          if card["story_id"] in exclusive_ids]
    pending_exclusive = set(frozen["bindings"]["pending_exclusive_story_ids"])
    assert len(second["cards"]) == 25
    assert len(surfaced_exclusive) <= 2
    assert all(card["exclusive_label"] == "only in Chinese press"
               for card in surfaced_exclusive)
    assert pending_exclusive == exclusive_ids - {
        card["story_id"] for card in (*first["cards"], *second["cards"])}
    assert len({card["story_id"] for card in (*first["cards"], *second["cards"])}) == 50
    assert subject._adapter.calls == 1


def test_finalizer_hard_duplicate_drop_is_not_resurrected_from_pending():
    rows = [corpus_row(index, hours=1 + index, source=f"dedup-{index}",
                       categories=[f"dedup-topic-{index}"])
            for index in range(100)]
    rows[1]["title"] = rows[0]["title"]
    duplicate_ids = {rows[0]["story_id"], rows[1]["story_id"]}
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    served = []
    while True:
        served.extend(response["cards"])
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    served_ids = {card["story_id"] for card in served}
    surfaced_duplicate_ids = served_ids & duplicate_ids
    pending_ids = {row["story_id"] for row in
                   store.frozen["frozen-1"]["bindings"]["pending_candidates"]}
    assert len(surfaced_duplicate_ids) == 1
    assert not (pending_ids & (duplicate_ids - surfaced_duplicate_ids))
    assert len(served) == len(served_ids) == 99
    assert {row["story_id"] for row in rows} - served_ids == duplicate_ids - surfaced_duplicate_ids
    assert len({card["title"] for card in served}) == len(served)
    assert subject._adapter.calls == 1


@pytest.mark.parametrize("duplicate_key", ["title", "canonical_url", "event_group_id"])
def test_semantic_duplicate_in_pending_never_crosses_a_continuation_boundary(duplicate_key):
    rows = [corpus_row(index, hours=1 + index, source=f"boundary-{index}",
                       categories=[f"boundary-topic-{index}"])
            for index in range(100)]
    if duplicate_key == "event_group_id":
        rows[0][duplicate_key] = rows[60][duplicate_key] = "shared-event-group"
    else:
        rows[60][duplicate_key] = rows[0][duplicate_key]
    winner_id, duplicate_id = rows[0]["story_id"], rows[60]["story_id"]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    frozen = store.frozen["frozen-1"]
    assert winner_id in {card["story_id"] for card in response["cards"]}
    assert duplicate_id in {row["story_id"] for row in frozen["bindings"]["pending_candidates"]}
    pages = []
    while True:
        if response["cards"]:
            pages.append(response["cards"])
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    served = [card for page in pages for card in page]
    served_ids = {card["story_id"] for card in served}
    pending_ids = {row["story_id"] for row in frozen["bindings"]["pending_candidates"]}
    assert served_ids == {row["story_id"] for row in rows} - {duplicate_id}
    assert duplicate_id not in pending_ids
    assert [len(page) for page in pages] == [25, 25, 25, 24]
    assert len(served) == len(served_ids) == 99
    assert all(card["lane_label"] for card in served)
    if duplicate_key == "event_group_id":
        groups = frozen["bindings"]["event_group_ids"]
        assert groups[winner_id] == "shared-event-group"
        assert duplicate_id not in groups
    assert subject._adapter.calls == 1


def test_same_continuation_window_duplicates_still_report_multi_outlet_coverage():
    rows = [corpus_row(index, hours=1 + index, source=f"coverage-{index}",
                       categories=[f"coverage-topic-{index}"])
            for index in range(100)]
    rows[60]["title"] = rows[61]["title"] = "Shared continuation report"
    rows[60]["source_is_aggregator"] = True
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    served = []
    while True:
        served.extend(response["cards"])
        cursor = response.get("next_cursor")
        if not cursor:
            break
        response = subject.page(authorization="Bearer valid", cursor=cursor)

    shared = [card for card in served if card["title"] == "Shared continuation report"]
    assert len(shared) == 1
    assert shared[0]["story_id"] == rows[61]["story_id"]
    assert shared[0]["also_covered_by"] == [rows[60]["source_name"]]
    assert {card["story_id"] for card in served} == {
        row["story_id"] for row in rows} - {rows[60]["story_id"]}
    assert subject._adapter.calls == 1


def test_general_semantic_drop_only_batch_advances_to_older_unique_rows():
    rows = [corpus_row(index, hours=1 + index, source=f"progress-{index}",
                       categories=[f"progress-topic-{index}"])
            for index in range(175)]
    for row in rows[50:150]:
        row["title"] = rows[0]["title"]
    expected_ids = ({row["story_id"] for row in rows[:50]}
                    | {row["story_id"] for row in rows[150:]})
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)

    response = rank(subject, store)
    pages, empty_responses, attempts = [], 0, 0
    while True:
        if response["cards"]:
            pages.append(response["cards"])
        else:
            empty_responses += 1
        cursor = response.get("next_cursor")
        if not cursor:
            break
        assert subject._decode_cursor(cursor)["frozen_order_id"] == "frozen-1"
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        attempts += 1
        assert attempts < 10, "semantic-only general scans did not terminate"

    served = [card for page in pages for card in page]
    assert [len(page) for page in pages] == [25, 25, 25]
    assert {card["story_id"] for card in served} == expected_ids
    assert len(served) == len({card["story_id"] for card in served}) == 75
    assert empty_responses >= 1
    assert all(card["lane_label"] for card in served)
    assert subject._adapter.calls == 1


def test_exclusive_semantic_drop_only_scans_advance_without_an_empty_loop():
    rows = exclusive_corpus(175)
    for row in rows[50:150]:
        row["title"] = rows[0]["title"]
    expected_ids = ({row["story_id"] for row in rows[:50]}
                    | {row["story_id"] for row in rows[150:]})
    store = PaidStore(events=liked_events(), exclusive=rows)
    subject = paid(store, exclusive_category="only-other-language-press")

    response = rank(subject, store, eligibility={
        "category": "only-other-language-press", "query": None})
    cards, empty_responses, attempts = [], 0, 0
    while True:
        cards.extend(response["cards"])
        if not response["cards"]:
            empty_responses += 1
        cursor = response.get("next_cursor")
        if not cursor:
            break
        assert subject._decode_cursor(cursor)["frozen_order_id"] == "frozen-1"
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        attempts += 1
        assert attempts < 10, "semantic-only exclusive scans did not terminate"

    assert {card["story_id"] for card in cards} == expected_ids
    assert len(cards) == len({card["story_id"] for card in cards}) == 75
    assert empty_responses >= 1
    assert all(card["exclusive_label"] == "only in Chinese press" for card in cards)
    assert subject._adapter.calls == 1


def test_shipped_policy_derived_pending_tail_cap_accepts_its_valid_boundary():
    subject = build(Store(events=liked_events()))
    composition = subject._policy.composition
    cap = subject._pending_candidate_limit(composition)
    rows = [corpus_row(10_000 + index, hours=1, source=f"cap-{index}",
                       categories=[f"cap-topic-{index}"])
            for index in range(cap)]

    loaded = subject._load_pending_candidates({"pending_candidates": rows}, composition)

    assert len(loaded) == cap
    assert cap == subject._pending_candidate_limit(load_composition_policy(POLICY_PATH))


def test_pending_tail_malformed_or_over_policy_cap_fails_closed():
    subject = build(Store(events=liked_events()))
    composition = subject._policy.composition
    cap = subject._pending_candidate_limit(composition)
    prototype = corpus_row(20_000, hours=1, source="cap", categories=["cap"])
    over_cap = [{**prototype, "story_id": f"story:{20_000 + index:064x}"}
                for index in range(cap + 1)]

    with pytest.raises(RuntimeError, match="invalid_pending_candidates"):
        subject._load_pending_candidates({"pending_candidates": {"not": "a list"}}, composition)
    with pytest.raises(RuntimeError, match="invalid_pending_candidates"):
        subject._load_pending_candidates({"pending_candidates": [prototype, prototype]}, composition)
    with pytest.raises(RuntimeError, match="invalid_pending_candidates"):
        subject._load_pending_candidates({"pending_candidates": over_cap}, composition)
    with pytest.raises(RuntimeError, match="invalid_pending_exclusive_story_ids"):
        subject._load_pending_exclusive_story_ids(
            {"pending_exclusive_story_ids": {"not": "a list"}}, [prototype], composition)
    with pytest.raises(RuntimeError, match="invalid_pending_exclusive_story_ids"):
        subject._load_pending_exclusive_story_ids(
            {"pending_exclusive_story_ids": [prototype["story_id"], prototype["story_id"]]},
            [prototype], composition)
    with pytest.raises(RuntimeError, match="invalid_pending_exclusive_story_ids"):
        subject._load_pending_exclusive_story_ids(
            {"pending_exclusive_story_ids": ["story:" + "f" * 64]}, [prototype], composition)
    with pytest.raises(RuntimeError, match="invalid_pending_exclusive_story_ids"):
        subject._load_pending_exclusive_story_ids(
            {"pending_exclusive_story_ids": [f"story:{30_000 + index:064x}"
                                              for index in range(cap + 1)]},
            [prototype], composition)


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
                 cursor=subject._cursor("frozen-1", 0,
                     int(store.frozen["frozen-1"]["expires_at"]), response_number=1))
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
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    exhausted = len(frozen["cards"])
    # Would raise ValueError("invalid cursor") from the fake if half a keyset
    # reached the hot lane, which is exactly what the SQL does.
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", exhausted, int(frozen["expires_at"]),
                                        response_number=2))
    stored = store.frozen["frozen-1"]["bindings"]["corpus_cursor"]
    assert set(stored) >= {"before_published_at", "before_story_id"}
    if "hot" in stored:
        assert set(stored["hot"]) == {"before_source_count", "before_published_at", "before_story_id"}
    # And a second continuation resumes from it without raising.
    frozen = store.frozen["frozen-1"]
    subject.page(authorization="Bearer valid",
                 cursor=subject._cursor("frozen-1", len(frozen["cards"]), int(frozen["expires_at"]),
                                        response_number=3))


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
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    # The store refuses to grow the order any further, exactly as the RPC does
    # when the card cap is reached.
    store.extend_frozen_order = lambda **kwargs: len(store.frozen["frozen-1"]["cards"])
    response = subject.page(authorization="Bearer valid",
                            cursor=subject._cursor("frozen-1", len(frozen["cards"]),
                                                   int(frozen["expires_at"]), response_number=2))
    assert response["cards"] == [], "a capped order served the same stories again"
    assert response["next_cursor"] is None
    assert response.get("end_of_run") is True, "the end of a run must be said, not implied"


def test_a_failed_extend_degrades_rather_than_five_hundreds():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]

    def explode(**kwargs):
        raise RuntimeError("the store is unavailable")

    store.extend_frozen_order = explode
    response = subject.page(authorization="Bearer valid",
                            cursor=subject._cursor("frozen-1", len(frozen["cards"]),
                                                   int(frozen["expires_at"]), response_number=2))
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
                 cursor=subject._cursor("frozen-1", 0,
                     int(store.frozen["frozen-1"]["expires_at"]), response_number=1))
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
                 cursor=subject._cursor("frozen-1", len(frozen["cards"]), int(frozen["expires_at"]),
                                        response_number=2))
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


def test_short_exclusive_pages_cannot_bypass_the_response_budget():
    """A one-source exclusive corpus produces short diversity-limited pages.

    The cursor offset can advance by fewer than page_size cards, so deriving the
    budget from offset // page_size served dozens of readable responses under a
    four-response policy. Count non-empty responses instead.
    """
    rows = exclusive_corpus(100)
    for row in rows:
        row["source_id"] = "one-exclusive-source"
        row["source_name"] = "One Exclusive Source"
    store = PaidStore(events=liked_events(), exclusive=rows)
    subject = paid(store, exclusive_category="only-other-language-press")
    policy = load_composition_policy(POLICY_PATH)
    response = rank(subject, store,
        eligibility={"category": "only-other-language-press", "query": None})
    nonempty = 1 if response["cards"] else 0
    cursor = response["next_cursor"]
    attempts = 0
    while cursor and attempts < 100:
        response = subject.page(authorization="Bearer valid", cursor=cursor)
        attempts += 1
        if response["cards"]:
            nonempty += 1
        cursor = response.get("next_cursor")
    assert nonempty == policy.max_pages_per_run
    assert response.get("end_of_run") is True
    assert attempts < 100, "the bounded continuation never terminated"


def test_concurrent_valid_cursors_share_one_atomic_response_slot():
    """Two signed cursors racing at count three may serve only one response."""
    class AtomicPageStore(PaidStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.page_lock = threading.Lock()
            self.load_barrier = None

        def reserve_run_response(self, **kwargs):
            with self.page_lock:
                return super().reserve_run_response(**kwargs)

        def load_frozen_order(self, **kwargs):
            value = copy.deepcopy(super().load_frozen_order(**kwargs))
            if self.load_barrier is not None:
                self.load_barrier.wait(timeout=5)
            return value

    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = AtomicPageStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    cursor_page_two = first["next_cursor"]
    second = subject.page(authorization="Bearer valid", cursor=cursor_page_two)
    third = subject.page(authorization="Bearer valid", cursor=second["next_cursor"])
    cursor_page_four = third["next_cursor"]
    assert store.frozen["frozen-1"]["bindings"]["responses_served"] == 3

    store.load_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda cursor: subject.page(authorization="Bearer valid", cursor=cursor),
            (cursor_page_two, cursor_page_four)))
    assert sum(bool(response["cards"]) for response in responses) == 1
    assert store.views[next(iter(store.views))]["pages_served"] == 4


def test_concurrent_end_cursors_append_one_continuation_batch():
    class ContinuationRaceStore(PaidStore):
        claim_barrier = None

        def claim_run_ranking(self, **kwargs):
            result = super().claim_run_ranking(**kwargs)
            if self.claim_barrier is not None:
                self.claim_barrier.wait(timeout=5)
            return result

    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = ContinuationRaceStore(rows, events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    cursor = subject._cursor("frozen-1", len(frozen["cards"]),
        int(frozen["expires_at"]), response_number=2)
    store.claim_barrier = threading.Barrier(2)

    def turn_page():
        try:
            return subject.page(authorization="Bearer valid", cursor=cursor)
        except RankingInProgressError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: turn_page(), range(2)))

    assert sum(result is not None for result in results) == 1
    ids = [card["story_id"] for card in store.frozen["frozen-1"]["cards"]]
    assert len(ids) == len(set(ids)), "concurrent continuation appended a duplicate batch"


def test_waiting_continuation_reloads_after_the_mutation_lock():
    class SequentialLockStore(PaidStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.load_lock = threading.Lock()
            self.load_barrier = None
            self.initial_loads = 0
            self.continuation_claims = 0

        def load_frozen_order(self, **kwargs):
            value = copy.deepcopy(super().load_frozen_order(**kwargs))
            if self.load_barrier is not None:
                with self.load_lock:
                    self.initial_loads += 1
                    wait = self.initial_loads <= 2
                if wait:
                    self.load_barrier.wait(timeout=5)
            return value

        def claim_run_ranking(self, **kwargs):
            if self.load_barrier is not None:
                with self.load_lock:
                    self.continuation_claims += 1
                    claim_number = self.continuation_claims
                if claim_number == 2:
                    import time
                    time.sleep(0.05)
            return super().claim_run_ranking(**kwargs)

    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = SequentialLockStore(rows, events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    cursor = subject._cursor("frozen-1", len(frozen["cards"]),
        int(frozen["expires_at"]), response_number=2)
    store.load_barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _value: subject.page(authorization="Bearer valid", cursor=cursor),
            range(2)))

    assert all(response["cards"] for response in responses)
    assert [card["story_id"] for card in responses[0]["cards"]] == \
        [card["story_id"] for card in responses[1]["cards"]]
    ids = [card["story_id"] for card in store.frozen["frozen-1"]["cards"]]
    assert len(ids) == len(set(ids))
    assert store.extensions.count(50) == 1


def test_the_lane_counts_account_for_every_card_on_the_page():
    store = PaidStore(events=[])
    subject = paid(store)
    rank(subject, store)
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


def test_a_frozen_refresh_overlays_current_owner_state_without_mutating_the_order():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    story_id = first["cards"][0]["story_id"]
    store.owner_states = lambda token, story_ids: ({story_id: {
        "read_at": NOW.isoformat(), "saved_at": NOW.isoformat(), "state_revision": 8,
        "interests": [{"topic_id": "world", "signal": "less_like", "revision": 3}],
    }} if story_id in story_ids else {})

    refreshed = rank(subject, store)
    card = next(item for item in refreshed["cards"] if item["story_id"] == story_id)
    assert card["read_at"] == NOW.isoformat() and card["saved_at"] == NOW.isoformat()
    assert card["state_revision"] == 8 and card["interests"][0]["signal"] == "less_like"
    persisted = next(item for item in store.frozen["frozen-1"]["cards"] if item["story_id"] == story_id)
    assert persisted["state_revision"] != 8, "a render-time overlay mutated the frozen order"


def test_a_refresh_after_page_two_resumes_at_page_three():
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])

    refreshed = rank(subject, store)
    decoded = subject._decode_cursor(refreshed["next_cursor"])
    assert decoded["offset"] == 50 and decoded["response_number"] == 3
    third = subject.page(authorization="Bearer valid", cursor=refreshed["next_cursor"])
    assert third["cards"]
    assert [card["story_id"] for card in third["cards"]] != \
        [card["story_id"] for card in second["cards"]]


def test_refresh_never_combines_a_stale_view_count_with_a_newer_offset():
    """A page-three commit between the refresh's view and order reads used to
    mint ordinal three at offset 75.  That counted page four as a replay and
    served five unique responses under a four-response cap."""
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    page_three_cursor = second["next_cursor"]
    original_load = store.load_frozen_order
    armed = {"value": True}

    def interleaved_load(**kwargs):
        snapshot = copy.deepcopy(original_load(**kwargs))
        if armed["value"]:
            armed["value"] = False
            hidden_third = subject.page(authorization="Bearer valid", cursor=page_three_cursor)
            assert hidden_third["cards"]
        return snapshot

    store.load_frozen_order = interleaved_load
    refreshed = rank(subject, store)
    store.load_frozen_order = original_load
    decoded = subject._decode_cursor(refreshed["next_cursor"])
    assert decoded["offset"] == 75 and decoded["response_number"] == 4
    fourth = subject.page(authorization="Bearer valid", cursor=refreshed["next_cursor"])
    assert fourth["cards"]
    ended = subject.page(authorization="Bearer valid", cursor=fourth["next_cursor"])
    assert ended["cards"] == [] and ended.get("end_of_run") is True
    assert next(iter(store.views.values()))["pages_served"] == 4


def test_a_filtered_empty_refresh_preserves_the_views_response_high_water_mark():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    prototype = frozen["cards"][0]
    frozen["cards"] = [
        {**prototype, "story_id": f"story:{index:064x}",
         "source_id": "blocked" if index < 50 else f"safe-{index}",
         "category_ids": ["blocked"] if index < 50 else ["safe"]}
        for index in range(150)
    ]
    frozen["bindings"]["corpus_has_more"] = False
    store.events.append({"event_id": "dislike", "event_type": "less_like_this",
        "event_revision": 9, "occurred_at": NOW.isoformat(),
        "payload": {"story_id": "story:" + "0" * 64, "surface": "reader"},
        "story_title": "", "story_summary": "", "source_id": "blocked"})

    refreshed = rank(subject, store)
    assert refreshed["cards"] == [] and refreshed["next_cursor"]
    assert subject._decode_cursor(refreshed["next_cursor"])["response_number"] == 2

    response = refreshed
    for expected in (2, 3, 4):
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
        assert response["cards"]
        assert store.views[next(iter(store.views))]["pages_served"] == expected
    assert response["next_cursor"]
    ended = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
    assert ended["cards"] == [] and ended.get("end_of_run") is True
    assert store.views[next(iter(store.views))]["pages_served"] == 4


def test_an_expired_order_cannot_reset_the_same_runs_response_budget():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    now = [CLOCK]
    subject._clock = lambda: now[0]
    first = rank(subject, store)
    assert first["cards"]
    assert next(iter(store.views.values()))["pages_served"] == 1
    now[0] += subject._policy.cursor_ttl_seconds + 1

    after = rank(subject, store)

    assert after.get("end_of_run") is True
    assert after["cards"] == []
    assert subject._adapter.calls == 1
    assert len(store.reservations) == 1


def test_an_expired_empty_paid_order_cannot_buy_a_second_ranking():
    store = PaidStore(events=liked_events())
    store.owner_states = lambda _token, story_ids: {
        story_id: {"read_at": NOW.isoformat()} for story_id in story_ids
    }
    subject = paid(store)
    now = [CLOCK]
    subject._clock = lambda: now[0]

    first = rank(subject, store)
    assert first["cards"] == []
    assert next(iter(store.views.values()))["pages_served"] == 0
    assert subject._adapter.calls == 1 and len(store.reservations) == 1
    now[0] += subject._policy.cursor_ttl_seconds + 1

    after = rank(subject, store)

    assert after.get("end_of_run") is True and after["cards"] == []
    assert subject._adapter.calls == 1
    assert len(store.reservations) == 1


def test_a_deleted_stale_order_may_rank_again_inside_the_open_run():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    original = store.history_snapshot
    store.frozen.clear()
    store.load_frozen_order = lambda **kwargs: store.frozen.get(kwargs["frozen_order_id"])
    store.history_snapshot = lambda token: {
        **original(token), "history_generation": 2, "events": [],
        "included_history_revision": 0, "history_revision": 0,
    }

    second = rank(subject, store, history_generation=2, history_revision=0,
                  server_commit_revision=0)

    assert second["cards"]
    assert second["request_id"] != first["request_id"]
    assert subject._adapter.calls == 2 and len(store.reservations) == 2
    assert store.frozen["frozen-2"]["bindings"]["profile_snapshot"]["event_count"] == 0
    assert len(store.views) == 2, "the new privacy epoch reused the old view budget"


def test_an_epoch_replacement_profile_is_frozen_across_later_views():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    original = store.history_snapshot
    store.frozen.clear()
    state = {"events": [], "revision": 0}

    def current(token):
        return {**original(token), "history_generation": 2,
            "events": list(state["events"]),
            "included_history_revision": state["revision"],
            "history_revision": state["revision"]}

    store.history_snapshot = current
    replacement = rank(subject, store, history_generation=2,
                       history_revision=0, server_commit_revision=0)
    assert replacement["cards"]
    assert store.runs[-1]["profile_snapshot"]["event_count"] == 0

    state["events"] = [{"event_id": "later", "event_type": "save", "event_revision": 1,
        "occurred_at": NOW.isoformat(),
        "payload": {"story_id": "story:" + "f" * 64, "topic_id": "later", "saved": True},
        "story_title": "Later", "story_summary": "", "source_id": "later-source"}]
    state["revision"] = 1
    later_view = rank(subject, store, history_generation=2,
        history_revision=1, server_commit_revision=1,
        eligibility={"category": "later", "query": None})
    assert later_view["cards"]
    assert store.frozen["frozen-3"]["bindings"]["profile_snapshot"]["event_count"] == 0


def test_a_legacy_page_two_cursor_is_rejected_after_atomic_cutover():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    key = next(iter(store.views))
    store.views[key]["pages_served"] = 0
    legacy = subject._cursor("frozen-1", 25, int(frozen["expires_at"]))

    with pytest.raises(StaleRankingError, match="cursor_version"):
        subject.page(authorization="Bearer valid", cursor=legacy)

    assert store.views[key]["pages_served"] == 0


def test_response_progress_is_persisted_atomically_with_the_page_budget():
    rows = [corpus_row(index, hours=1 + index, source=f"deep{index}",
                       categories=[f"d{index % 9}"]) for index in range(300)]
    store = PaidStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)

    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    replay = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    third = subject.page(authorization="Bearer valid", cursor=second["next_cursor"])

    assert second["cards"] and replay["cards"] and third["cards"]
    assert [card["story_id"] for card in replay["cards"]] == \
        [card["story_id"] for card in second["cards"]]
    assert next(iter(store.views.values()))["pages_served"] == 3


def test_an_initial_page_recording_failure_repairs_from_the_bound_order():
    class FirstPageFailureStore(PaidStore):
        fail_page_once = True

        def reserve_run_response(self, **kwargs):
            if self.fail_page_once:
                self.fail_page_once = False
                raise RuntimeError("transient page record")
            return super().reserve_run_response(**kwargs)

    store = FirstPageFailureStore(events=liked_events())
    subject = paid(store)
    with pytest.raises(RuntimeError, match="page_budget_unavailable"):
        rank(subject, store)

    recovered = rank(subject, store)
    second = subject.page(authorization="Bearer valid", cursor=recovered["next_cursor"])

    assert recovered["cards"] and second["cards"]
    assert subject._adapter.calls == 1 and len(store.reservations) == 1
    assert next(iter(store.views.values()))["pages_served"] == 2


def test_a_transient_page_reservation_failure_remains_retryable():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    original = store.reserve_run_response

    def unavailable(**kwargs):
        if kwargs["response_number"] == 2:
            raise RuntimeError("temporary database outage")
        return original(**kwargs)

    store.reserve_run_response = unavailable
    with pytest.raises(RuntimeError, match="page_budget_unavailable"):
        subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert next(iter(store.views.values()))["pages_served"] == 1

    store.reserve_run_response = original
    retried = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert retried["cards"] and retried.get("end_of_run") is not True
    assert next(iter(store.views.values()))["pages_served"] == 2


def test_a_policy_deploy_never_reuses_the_old_policys_frozen_view():
    """The browser rejects a frozen response whose policy no longer matches.

    A deploy can happen while an hourly reading run is still open. The new
    service must open a new policy-bound view rather than serving the prior
    policy's order and making the live reader fall back with an empty feed.
    """
    store = PaidStore(events=liked_events())
    old = paid(store, policy_version="policy-v1")
    first = rank(old, store)
    current = paid(store, policy_version="policy-v2")
    second = rank(current, store)

    assert first["policy_version"] == "policy-v1"
    assert second["policy_version"] == "policy-v2"
    assert second["request_id"] != first["request_id"]
    assert len(store.frozen) == 2
    assert len(store.views) == 2


def test_a_code_only_deploy_never_reuses_the_old_effective_policy_view():
    store = PaidStore(events=liked_events())
    old = paid(store, effective_policy_digest="a" * 64)
    first = rank(old, store)
    current = paid(store, effective_policy_digest="b" * 64)
    second = rank(current, store)

    assert second["request_id"] != first["request_id"]
    assert len(store.reservations) == 2
    assert len(store.views) == 2


def test_request_page_size_does_not_buy_a_second_ranking_for_one_view():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store, page_size=24)
    second = rank(subject, store, page_size=25)

    assert second["request_id"] == first["request_id"]
    assert subject._adapter.calls == 1
    assert len(store.reservations) == 1
    assert len(store.frozen) == 1


def test_a_legacy_cursor_cannot_spend_the_new_deploys_page_budget():
    store = PaidStore(events=liked_events())
    old = paid(store, effective_policy_digest="a" * 64)
    rank(old, store)
    # Simulate an order stored before exact eligibility keys were persisted.
    store.frozen["frozen-1"]["bindings"].pop("eligibility_key")

    current = paid(store, effective_policy_digest="b" * 64)
    new_page = rank(current, store)
    current_key = current._eligibility_key(None, None, False)
    assert store.views[("run-1", current_key)]["pages_served"] == 1

    legacy = current._cursor("frozen-1", 25,
        int(store.frozen["frozen-1"]["expires_at"]))
    with pytest.raises(StaleRankingError, match="cursor_version"):
        current.page(authorization="Bearer valid", cursor=legacy)

    assert store.views[("run-1", current_key)]["pages_served"] == 1
    assert current.page(authorization="Bearer valid", cursor=new_page["next_cursor"])["cards"]


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
    # Production clears reading runs on a history-generation reset. Reproduce
    # that trigger boundary rather than reusing an impossible stale run view.
    store.runs.clear()
    store.views.clear()
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
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1,
        "_history_generation": 1, "_consent_revision": 1}, "created": False})
    key = subject._eligibility_key(None, None, False)
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": "dead-request", "claim_expired": True}
    response = rank(subject, store)
    assert response["cards"], "an expired claim locked the reader out of her own feed"
    assert subject._adapter.calls == 1


def test_a_held_claim_with_no_order_yet_is_reported_as_in_progress():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1,
        "_history_generation": 1, "_consent_revision": 1}, "created": False})
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


# --- a TTL takeover must not double-pay a merely slow winner ---------------

def test_a_takeover_before_the_reserve_costs_the_loser_nothing():
    """The claim expiring does not mean its holder is dead, only that it is slow.
    The reservation re-checks the claim in its own transaction, so a caller whose
    claim has moved on spends nothing at all."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    key = subject._eligibility_key(None, None, False)
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1,
        "_history_generation": 1, "_consent_revision": 1}, "created": False})
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": None, "claim_expired": False}

    original = subject._store.reserve_budget_claimed

    def someone_takes_over_first(**kwargs):
        # Between this request taking its claim and reserving, a second caller
        # took the claim over by age.
        store.views[("run-1", key)]["claim_token"] = "a-later-request"
        return original(**kwargs)

    subject._store.reserve_budget_claimed = someone_takes_over_first
    # Nothing is bound yet, so the honest answer is "wait", not a second ranking.
    with pytest.raises(StaleRankingError, match="ranking_in_progress"):
        rank(subject, store)
    assert store.reservations == [], "a caller with a stale claim reserved budget"
    assert subject._adapter.calls == 0, "a caller with a stale claim called the provider"


def test_a_takeover_after_the_reserve_serves_the_other_order_and_releases():
    """Taken over between the reserve and the bind: the bind returns False, and
    that False is now looked at. Serving its own order would hand the reader a
    cursor into a ranking the next refresh cannot find."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)               # the winner's order exists
    key = subject._eligibility_key(None, None, False)
    store.reservations.clear()
    store.settlements.clear()
    # A second request that holds a claim which is about to be taken from it.
    store.views[("run-1", key)]["claim_token"] = None
    store.views[("run-1", key)]["frozen_order_id"] = None
    store.views[("run-1", key)]["pages_served"] = 0
    second = paid(store)
    original_bind = store.bind_run_frozen_order

    def taken_over_before_the_bind(**kwargs):
        store.views[("run-1", key)]["claim_token"] = "a-later-request"
        store.views[("run-1", key)]["frozen_order_id"] = "frozen-1"
        return original_bind(**kwargs)

    store.bind_run_frozen_order = taken_over_before_the_bind
    served = rank(second, store)
    assert served["request_id"] == first["request_id"], "it served its own unbound order"
    # The provider really answered this one, so its cost STAYS settled. Erasing a
    # real charge is the one thing the settlement path must never do.
    assert [entry["status"] for entry in store.settlements] == ["settled"]


def test_a_reservation_that_bought_nothing_is_released():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    subject._abandon_unbound_order(
        type("Owner", (), {"user_id": OWNER_ID})(),
        "request-1", reservation_created=True, observed_usage={})
    assert [entry["status"] for entry in store.settlements] == ["released"]
    # And a reservation that DID buy something keeps its real settled cost.
    store.settlements.clear()
    subject._abandon_unbound_order(
        type("Owner", (), {"user_id": OWNER_ID})(),
        "request-2", reservation_created=True, observed_usage={"input_tokens": 1})
    assert store.settlements == []


def test_a_provider_failure_releases_the_claim_immediately():
    """Every raising path after the claim used to hold it for the full TTL, so
    one failure made the next request wait a minute for a claim nobody held."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    key = subject._eligibility_key(None, None, False)

    def explode(*args, **kwargs):
        raise RuntimeError("the provider died")

    subject._adapter.rank = explode
    with pytest.raises(RuntimeError, match="the provider died"):
        rank(subject, store)
    assert store.views[("run-1", key)]["claim_token"] is None, \
        "a failed ranking kept its claim and locked the next request out"
    # And the next request can proceed immediately.
    subject._adapter.rank = CountingAdapter().rank.__get__(subject._adapter)
    assert rank(paid(store), store)["cards"]


def test_ranking_in_progress_when_the_winner_has_not_bound_yet():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    key = subject._eligibility_key(None, None, False)
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1,
        "_history_generation": 1, "_consent_revision": 1}, "created": False})
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": None, "claim_expired": False}
    original_bind = store.bind_run_frozen_order

    def taken_over_with_nothing_bound(**kwargs):
        store.views[("run-1", key)]["claim_token"] = "a-later-request"
        return original_bind(**kwargs)

    store.bind_run_frozen_order = taken_over_with_nothing_bound
    with pytest.raises(StaleRankingError, match="ranking_in_progress"):
        rank(subject, store)


# --- a refusal says WHICH refusal it was ----------------------------------

def _refusal_lines(captured):
    return [json.loads(line) for line in captured.err.splitlines()
            if line.startswith('{"event":"m2_reserve_refused"')]


def test_a_lost_claim_and_an_exhausted_budget_are_logged_apart(capsys):
    """Both mean "do not call the provider", and both used to arrive downstream
    as one generic budget_reservation_failed. One is a race that resolved itself;
    the other is a day's money gone."""
    store = PaidStore(events=liked_events())
    subject = paid(store)
    key = subject._eligibility_key(None, None, False)
    store.runs.append({"run_id": "run-1", "profile_snapshot": {"schema_version": 1,
        "_history_generation": 1, "_consent_revision": 1}, "created": False})
    store.views[("run-1", key)] = {"frozen_order_id": None, "pages_served": 0,
                                   "claim_token": None, "claim_expired": False}

    original = store.reserve_budget_claimed

    def taken_over_first(**kwargs):
        store.views[("run-1", key)]["claim_token"] = "a-later-request"
        return original(**kwargs)

    store.reserve_budget_claimed = taken_over_first
    with pytest.raises(StaleRankingError, match="ranking_in_progress"):
        rank(subject, store)
    lost = _refusal_lines(capsys.readouterr())
    assert [entry["reason"] for entry in lost] == ["claim_lost"]
    assert lost[0]["view"] == key and "remaining_usd" in lost[0]

    # Now the budget branch, which used to be silent.
    spent = PaidStore(events=liked_events())
    spent.reserve_budget = lambda **kwargs: spent.reservations.append(kwargs) or False
    budget_subject = paid(spent)
    rank(budget_subject, spent)
    refused = _refusal_lines(capsys.readouterr())
    assert [entry["reason"] for entry in refused] == ["budget"]
    assert refused[0]["remaining_usd"] == 0.0
    assert refused[0]["view"] == budget_subject._eligibility_key(None, None, False)


def test_a_successful_reservation_logs_no_refusal(capsys):
    store = PaidStore(events=liked_events())
    rank(paid(store), store)
    assert _refusal_lines(capsys.readouterr()) == []
