"""The candidate recipe: four labeled pools with quotas, caps and a profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.recommendation.composition import load_composition_policy
from curator.recommendation.profile import BehaviorProfile, build_profile
from curator.recommendation.recipe import assign_lane, build_window, lane_window_quotas

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def policy():
    return load_composition_policy(POLICY_PATH)


def row(story: str, *, hours: float = 1.0, source: str = "reuters", categories=("world",),
        independent: int | None = None, aggregator: bool = False, title: str | None = None, **extra):
    # A non-aggregator row counts its own publisher as one independent source,
    # which is what the v2 lane RPC returns for a story nobody else carried.
    independent = (0 if aggregator else 1) if independent is None else independent
    return {"story_id": f"story:{story.rjust(64, '0')}", "title": title or f"title {story}",
            "summary": "", "source_id": source, "source_name": source, "language": "en",
            "canonical_url": f"https://example.test/{story}",
            "published_at": (NOW - timedelta(hours=hours)).isoformat(),
            "category_ids": list(categories), "independent_source_count": independent,
            "source_is_aggregator": aggregator, **extra}


def event(kind: str, *, source: str, topic: str | None = None, hours: float = 1.0, **payload):
    body = {"surface": "reader", **payload}
    if topic:
        body["topic_id"] = topic
    return {"event_id": f"e{kind}{source}{topic}", "event_type": kind, "source_id": source,
            "payload": body, "occurred_at": (NOW - timedelta(hours=hours)).isoformat()}


def snapshot(events, *, learning=True):
    return {"learning_enabled": learning, "events": events}


# --- profile ---------------------------------------------------------------

def test_learning_off_yields_an_empty_profile(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world")], learning=False),
                            policy=policy, now=NOW)
    assert profile.is_empty and profile.event_count == 0


def test_positive_actions_build_affinity_and_negatives_suppress(policy):
    profile = build_profile(snapshot([
        event("save", source="reuters", topic="world", saved=True),
        event("open_original", source="reuters", topic="world"),
        event("less_like_this", source="cnbeta", topic="gadgets"),
    ]), policy=policy, now=NOW)
    assert profile.affinity(source_id="reuters", category_ids=("world",)) > 0
    assert profile.affinity(source_id="cnbeta", category_ids=("gadgets",)) < 0
    assert profile.suppressed_sources == frozenset({"cnbeta"})
    assert profile.suppressed_topics == frozenset({"gadgets"})


def test_an_unsave_withdraws_its_own_save(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=False)]),
                            policy=policy, now=NOW)
    assert profile.affinity(source_id="reuters", category_ids=("world",)) < 0


def test_older_behavior_counts_for_less(policy):
    recent = build_profile(snapshot([event("save", source="a", topic="t", hours=1, saved=True)]),
                           policy=policy, now=NOW)
    old = build_profile(snapshot([event("save", source="a", topic="t", hours=500, saved=True)]),
                        policy=policy, now=NOW)
    assert recent.affinity(source_id="a", category_ids=()) > old.affinity(source_id="a", category_ids=())


def test_the_profile_survives_a_freeze_and_thaw(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=True)]),
                            policy=policy, now=NOW)
    assert BehaviorProfile.from_snapshot(profile.as_snapshot()).as_snapshot() == profile.as_snapshot()


# --- lane assignment -------------------------------------------------------

def test_a_brand_new_story_is_fresh(policy):
    assert assign_lane(row("a", hours=1), profile=BehaviorProfile(), policy=policy, now=NOW)[0] == "updates"


def test_corroboration_makes_a_story_hot(policy):
    lane, score = assign_lane(row("a", hours=10, independent=3), profile=BehaviorProfile(), policy=policy, now=NOW)
    assert lane == "hot" and score == 3.0


def test_aggregator_echoes_alone_are_not_hot(policy):
    # Three observations, zero independent publishers. Not hot, by construction.
    lane, _ = assign_lane(row("a", hours=10, independent=0, aggregator=True),
                          profile=BehaviorProfile(), policy=policy, now=NOW)
    assert lane != "hot"


def test_a_profile_match_lands_in_for_you(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=True)]),
                            policy=policy, now=NOW)
    assert assign_lane(row("a", hours=10, source="reuters"), profile=profile, policy=policy, now=NOW)[0] == "interested"


def test_an_off_profile_story_is_a_surprise(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=True)]),
                            policy=policy, now=NOW)
    assert assign_lane(row("a", hours=10, source="quanta", categories=("science",)),
                       profile=profile, policy=policy, now=NOW)[0] == "surprise"


def test_with_no_profile_nothing_is_a_surprise(policy):
    lanes = {assign_lane(row(str(index), hours=10), profile=BehaviorProfile(), policy=policy, now=NOW)[0]
             for index in range(5)}
    assert "surprise" not in lanes


def test_an_aggregator_never_holds_an_exploration_slot(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=True)]),
                            policy=policy, now=NOW)
    lane, _ = assign_lane(row("a", hours=10, source="buzzing", categories=("misc",), aggregator=True),
                          profile=profile, policy=policy, now=NOW)
    assert lane != "surprise"


# --- window assembly -------------------------------------------------------

def test_quotas_sum_to_the_window(policy):
    quotas = lane_window_quotas(policy, policy.candidate_window_size)
    assert sum(quotas.values()) == policy.candidate_window_size


def test_no_single_firehose_can_fill_the_window(policy):
    rows = [row(str(index), hours=index % 5 + 1, source="buzzing", aggregator=True,
                categories=("misc",)) for index in range(60)]
    window = build_window(rows, profile=BehaviorProfile(), policy=policy, now=NOW)
    assert len(window) == policy.per_aggregator_cap_per_window
    publishers = [row(f"p{index}", hours=1, source="reuters") for index in range(10)]
    window = build_window(publishers, profile=BehaviorProfile(), policy=policy, now=NOW)
    assert len(window) == policy.per_source_cap_per_window


def test_the_window_is_no_longer_the_newest_fifty(policy):
    profile = build_profile(snapshot([event("save", source="reuters", topic="world", saved=True)]),
                            policy=policy, now=NOW)
    rows = ([row(f"f{index}", hours=1, source=f"fresh{index}", categories=("misc",)) for index in range(40)]
            + [row(f"h{index}", hours=12, source=f"hot{index}", independent=4, categories=("markets",))
               for index in range(10)]
            + [row(f"a{index}", hours=20, source="reuters", categories=("world",)) for index in range(10)])
    window = build_window(rows, profile=profile, policy=policy, now=NOW)
    lanes = {lane: sum(1 for item in window if item.lane == lane) for lane in policy.lane_priority}
    assert lanes["hot"] > 0 and lanes["interested"] > 0
    # Fresh no longer owns the whole window the way "the 50 newest rows" did.
    assert lanes["updates"] <= lane_window_quotas(policy, policy.candidate_window_size)["updates"]


def test_a_short_pool_ships_short_and_never_borrows(policy):
    rows = [row(str(index), hours=1, source=f"s{index}") for index in range(4)]
    window = build_window(rows, profile=BehaviorProfile(), policy=policy, now=NOW)
    assert len(window) == 4
    assert all(item.lane == "updates" for item in window)


def test_the_window_is_replayable(policy):
    rows = [row(str(index), hours=index % 30 + 1, source=f"s{index % 7}") for index in range(40)]
    first = build_window(rows, profile=BehaviorProfile(), policy=policy, now=NOW)
    second = build_window(list(reversed(rows)), profile=BehaviorProfile(), policy=policy, now=NOW)
    assert [item.story_id for item in first] == [item.story_id for item in second]
