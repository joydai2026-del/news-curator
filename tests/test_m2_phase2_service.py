"""The Phase 2 feed, end to end through RankingService.

What these prove, in JJ's terms: the page is no longer the newest fifty rows,
every card says why it is there, reading a story does not re-bill the next page,
and a save in another tab no longer throws away a rank that was already paid for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.recommendation.composition import load_composition_policy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.service import RankingService, ServicePolicy, StaleRankingError

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
    for offset in range(16):  # on profile
        # Distinct sources on purpose: a single source is capped at three per
        # window, so an aligned pool built from one outlet starves by design.
        rows.append(corpus_row(index, hours=20, source=f"aligned{offset}",
                               categories=["world"] if offset % 2 else [f"liked{offset}"]))
        index += 1
    for offset in range(12):  # off profile, quality-gated
        rows.append(corpus_row(index, hours=30, source=f"odd{offset}", categories=[f"odd{offset}"]))
        index += 1
    return rows


class Store:
    """A corpus that answers the lane RPC the way PostgreSQL does."""

    def __init__(self, rows=None, *, events=(), learning=True):
        self.rows = list(rows if rows is not None else default_corpus())
        self.events = list(events)
        self.learning = learning
        self.frozen = {}
        self.sequence = 0
        self.reservations = []
        self.settlements = []
        self.runs = []
        self.revision = 0
        self.revision_after_provider = None

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
                               limit, before_published_at=None, before_story_id=None):
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
            selected.append(row)
        if lane == "hot":
            selected.sort(key=lambda row: -row["independent_source_count"])
        return selected[:limit]

    def retained_candidates_language_exclusive(self, **kwargs):
        return []

    # --- runs --------------------------------------------------------------
    def open_reading_run(self, *, user_id, idle_minutes, profile):
        if self.runs:
            return self.runs[-1]
        run = {"run_id": f"run-{len(self.runs) + 1}", "profile_snapshot": profile, "created": True}
        self.runs.append(run)
        return run

    # --- owner state and budget -------------------------------------------
    def owner_states(self, token, story_ids):
        return {}

    def reserve_budget(self, **kwargs):
        self.reservations.append(kwargs)
        return False

    def settle_budget(self, **kwargs):
        self.settlements.append(kwargs)

    def save_frozen_order(self, **kwargs):
        # The epoch trigger refuses bindings whose server_commit_revision is not
        # the CURRENT one. Reproduced here, because that trigger is what turns a
        # mis-scoped staleness check into a discarded paid call.
        current = self.commit_revision if self.revision_after_provider is None else self.revision_after_provider
        if kwargs["bindings"].get("server_commit_revision") != current:
            raise RuntimeError("stale frozen ranking bindings")
        self.sequence += 1
        key = f"frozen-{self.sequence}"
        self.frozen[key] = kwargs
        return key

    def load_frozen_order(self, *, user_id, frozen_order_id):
        value = self.frozen[frozen_order_id]
        return {"expires_at": value["expires_at"], "page_size": value["page_size"],
                "bindings": value["bindings"], "cards": value["cards"]}


def build(store, *, composition=True, page_size=25):
    adapter = RankLLMAdapter(policy=RankerPolicy("openai", "gpt-5-mini", "https://provider.invalid", "policy",
        input_cost_per_million_tokens_usd=.25, output_cost_per_million_tokens_usd=2), engine=object())
    policy = ServicePolicy("policy", "gpt-5-mini", "policy", "tenant", candidate_limit=50,
        maximum_page_size=page_size, enabled=True,
        composition=load_composition_policy(POLICY_PATH) if composition else None)
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
    assert {card["lane"] for card in response["cards"]} <= {"updates", "hot"}
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
