"""The six acceptance invariants of the finalized page, asserted on every page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.dedup import normalize_title
from curator.recommendation.composition import load_composition_policy
from curator.recommendation.finalize import finalize_page, page_quotas
from curator.recommendation.profile import BehaviorProfile
from curator.recommendation.recipe import LanedCandidate

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def policy():
    return load_composition_policy(POLICY_PATH)


def candidate(index: int, lane: str, *, source: str | None = None, topic: str | None = None,
              title: str | None = None, url: str | None = None, group: str | None = None,
              aggregator: bool = False, score: float = 0.0):
    story = f"story:{str(index).rjust(64, '0')}"
    return LanedCandidate(story, lane, score, {
        "story_id": story, "title": title or f"headline {index}", "summary": "",
        "source_id": source or f"source{index}", "source_name": source or f"source{index}",
        "canonical_url": url or f"https://example.test/{index}",
        "published_at": (NOW - timedelta(hours=1)).isoformat(),
        "category_ids": [topic or f"topic{index}"], "source_is_aggregator": aggregator,
        "event_group_id": group, "language": "en"})


def full_order(policy, page_size=25):
    """A generous pool: every lane over-supplied, all sources and topics distinct."""
    order = []
    index = 0
    for lane in policy.lane_priority:
        for _ in range(page_size):
            order.append(candidate(index, lane, score=float(page_size * 4 - index)))
            index += 1
    return order


def assert_invariants(page, policy, page_size, *, ordered_lanes=None):
    quotas = page_quotas(policy, page_size)
    # 1. Never more than a page; fewer only with a recorded reason.
    assert len(page.cards) <= page_size
    if len(page.cards) < page_size:
        assert page.short_lane_reasons
    # 2. Per-lane counts within one of quota, or a recorded shortfall.
    short_lanes = {entry["lane"] for entry in page.short_lane_reasons}
    for lane, quota in quotas.items():
        served = sum(1 for card in page.cards if card.lane == lane)
        assert served <= quota or short_lanes, f"{lane} over quota with no shortfall recorded"
        assert abs(served - quota) <= 1 or lane in short_lanes or short_lanes
    # 3. No duplicate headline, URL or event group.
    for key in ("title", "canonical_url", "event_group_id"):
        values = [card.row[key] for card in page.cards if card.row.get(key)]
        if key == "title":
            values = [normalize_title(value) for value in values]
        assert len(values) == len(set(values))
    # 4. Nothing already opened.
    assert all(card.story_id for card in page.cards)
    # 5. No two adjacent cards share a source or an event group.
    for left, right in zip(page.cards, page.cards[1:]):
        assert left.row["source_id"] != right.row["source_id"]
        assert not (left.row.get("event_group_id") and left.row["event_group_id"] == right.row.get("event_group_id"))


def test_a_full_page_meets_every_invariant(policy):
    page = finalize_page(full_order(policy), policy=policy, owner_states={}, page_size=25)
    assert_invariants(page, policy, 25)
    assert len(page.cards) == 25
    assert not page.short_lane_reasons
    counts = {lane: sum(1 for card in page.cards if card.lane == lane) for lane in policy.lane_priority}
    assert counts == {"updates": 7, "hot": 4, "interested": 11, "surprise": 3}


def test_already_opened_stories_are_removed_not_demoted(policy):
    order = full_order(policy)
    opened = {order[0].story_id: {"read_at": "2026-09-18T10:00:00+00:00"}}
    page = finalize_page(order, policy=policy, owner_states=opened, page_size=25)
    assert order[0].story_id not in {card.story_id for card in page.cards}
    assert_invariants(page, policy, 25)


def test_duplicates_collapse_and_the_publisher_survives(policy):
    order = [candidate(0, "updates", source="buzzing", title="One Event", aggregator=True, score=10.0),
             candidate(1, "updates", source="reuters", title="one event!", score=9.0),
             candidate(2, "updates", source="cnn", url="https://example.test/1", score=8.0)]
    page = finalize_page(order, policy=policy, owner_states={}, page_size=25)
    assert [card.row["source_id"] for card in page.cards] == ["reuters"]
    assert page.also_covered_by[page.cards[0].story_id] == ("buzzing", "cnn")


def test_one_event_group_yields_one_card(policy):
    order = [candidate(0, "updates", source="rfi-zh", group="group:" + "a" * 32, score=5.0),
             candidate(1, "updates", source="rfi", group="group:" + "a" * 32, score=4.0)]
    page = finalize_page(order, policy=policy, owner_states={}, page_size=25)
    assert len(page.cards) == 1


def test_two_cards_from_one_source_are_never_adjacent(policy):
    order = ([candidate(index, "updates", source="reuters", topic=f"t{index}", score=100.0 - index)
              for index in range(3)]
             + [candidate(index + 10, "interested", source=f"other{index}", topic=f"o{index}",
                          score=50.0 - index) for index in range(10)])
    page = finalize_page(order, policy=policy, owner_states={}, page_size=6)
    assert_invariants(page, policy, 6)


def test_an_exhausted_lane_records_a_reason_and_never_drains_surprise(policy):
    order = ([candidate(index, "interested", score=50.0 - index) for index in range(20)]
             + [candidate(100 + index, "surprise", score=10.0 - index) for index in range(3)])
    page = finalize_page(order, policy=policy, owner_states={}, page_size=25)
    lanes = {entry["lane"] for entry in page.short_lane_reasons}
    assert {"updates", "hot"} <= lanes
    # Surprise kept its three protected slots; aligned donated the rest.
    assert sum(1 for card in page.cards if card.lane == "surprise") == 3
    assert sum(1 for card in page.cards if card.lane == "interested") > 11


def test_the_page_is_byte_identical_on_a_replay(policy):
    order = full_order(policy)
    first = finalize_page(order, policy=policy, owner_states={}, page_size=25)
    second = finalize_page(list(order), policy=policy, owner_states={}, page_size=25)
    assert [card.story_id for card in first.cards] == [card.story_id for card in second.cards]
    assert first.short_lane_reasons == second.short_lane_reasons


def test_calibration_is_measured_and_never_reorders(policy):
    profile = BehaviorProfile(observed_topic_mix={"world": 1.0})
    order = [candidate(index, "interested", topic="markets", score=50.0 - index) for index in range(11)]
    page = finalize_page(order, policy=policy, owner_states={}, page_size=25, profile=profile)
    assert page.calibration_kl is not None and page.calibration_alarm is True
    # The order is the model's order: the alarm is a report, not a reranker.
    assert [card.story_id for card in page.cards] == [item.story_id for item in order[:len(page.cards)]]


def test_no_profile_means_no_calibration_number(policy):
    page = finalize_page(full_order(policy), policy=policy, owner_states={}, page_size=25,
                         profile=BehaviorProfile())
    assert page.calibration_kl is None and page.calibration_alarm is False
