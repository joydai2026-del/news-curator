"""Tier-1 cross-language grouping: precision first, recall second."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.grouping import GroupingCandidate, GroupingPolicy, assign_event_groups

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def candidate(story_id, language, title, summary="", offset_hours=0):
    return GroupingCandidate(story_id=story_id, language=language, title=title, summary=summary,
                             published_at=NOW - timedelta(hours=offset_hours))


def test_tier1_groups_an_en_zh_pair_sharing_tokens_and_numbers():
    rows = [
        candidate("story:a", "en", "Nvidia beats on earnings with 3 new Blackwell chips"),
        candidate("story:b", "zh", "英伟达财报超预期 Nvidia 发布 3 款 Blackwell 芯片", offset_hours=2),
    ]
    groups = assign_event_groups(rows)
    assert set(groups) == {"story:a", "story:b"}
    assert groups["story:a"] == groups["story:b"]
    assert groups["story:a"].startswith("group:")


def test_tier1_does_not_group_a_pair_differing_only_by_a_number():
    rows = [
        candidate("story:a", "en", "Apple ships iOS 18.6.1 security update"),
        candidate("story:b", "zh", "苹果发布 Apple iOS 18.6.2 security 更新"),
    ]
    assert assign_event_groups(rows) == {}


def test_tier1_never_groups_on_an_empty_number_set():
    rows = [
        candidate("story:a", "en", "Nvidia and Blackwell expand their partnership"),
        candidate("story:b", "zh", "Nvidia 与 Blackwell 扩大合作"),
    ]
    assert assign_event_groups(rows) == {}


def test_tier1_respects_the_window_and_the_language_guard():
    far = [
        candidate("story:a", "en", "Nvidia beats on earnings with 3 Blackwell chips"),
        candidate("story:b", "zh", "英伟达 Nvidia 3 款 Blackwell 芯片 earnings", offset_hours=40),
    ]
    assert assign_event_groups(far) == {}
    same_language = [
        candidate("story:a", "en", "Nvidia beats on earnings with 3 Blackwell chips"),
        candidate("story:c", "en", "Nvidia earnings: 3 Blackwell chips announced"),
    ]
    assert assign_event_groups(same_language) == {}


def test_single_member_groups_get_no_id_and_the_kill_switch_disables_grouping():
    rows = [candidate("story:a", "en", "Nvidia beats on earnings with 3 Blackwell chips")]
    assert assign_event_groups(rows) == {}
    pair = [
        candidate("story:a", "en", "Nvidia beats on earnings with 3 Blackwell chips"),
        candidate("story:b", "zh", "英伟达 Nvidia 3 款 Blackwell 芯片 earnings"),
    ]
    assert assign_event_groups(pair, policy=GroupingPolicy(cross_language_enabled=False)) == {}


def test_group_id_is_independent_of_input_order():
    rows = [
        candidate("story:b", "zh", "英伟达 Nvidia 3 款 Blackwell 芯片 earnings"),
        candidate("story:a", "en", "Nvidia beats on earnings with 3 Blackwell chips"),
    ]
    reversed_rows = list(reversed(rows))
    assert assign_event_groups(rows) == assign_event_groups(reversed_rows)


@pytest.mark.parametrize("kwargs", [
    {"min_shared_entity_tokens": 0},
    {"min_shared_entity_tokens": 11},
    {"window_hours": 0},
    {"window_hours": 73},
    {"cross_language_enabled": "yes"},
])
def test_policy_refuses_out_of_range_values(kwargs):
    with pytest.raises(ValueError):
        GroupingPolicy(**kwargs)
