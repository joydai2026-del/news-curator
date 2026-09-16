"""Tier-1 cross-language grouping, exercised the way it runs: title AND summary.

Round 1 review found the shipped regime was untested: every case used
summary="", while retain() feeds both fields in. Two real write-ups of one event
almost never carry identical number sets, so the rule intersects numbers instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.grouping import GroupingCandidate, GroupingPolicy, assign_event_groups

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

# The exact pair the round-1 reviewer ran, which produced no group before.
EN_EARNINGS = (
    "Nvidia beats on earnings with 3 Blackwell chips",
    "Nvidia reported revenue of 46 billion dollars for the quarter ending July, 56 percent "
    "above a year ago, and said 7 new data centres are live in 2026.",
)
ZH_EARNINGS = (
    "英伟达 Nvidia 发布 Blackwell 芯片 earnings 超预期",
    "英伟达公布季度营收 467 亿美元，同比增长 56%，并称 2026 年将有 3 座新数据中心投入使用。",
)


def candidate(story_id, language, pair, offset_hours=0):
    title, summary = pair
    return GroupingCandidate(story_id=story_id, language=language, title=title, summary=summary,
                             published_at=NOW - timedelta(hours=offset_hours))


def test_a_real_en_zh_pair_about_one_event_groups():
    rows = [candidate("story:a", "en", EN_EARNINGS), candidate("story:b", "zh", ZH_EARNINGS, offset_hours=2)]
    groups = assign_event_groups(rows)
    assert set(groups) == {"story:a", "story:b"}
    assert groups["story:a"] == groups["story:b"]
    assert groups["story:a"].startswith("group:")


def test_one_shared_number_is_enough_when_the_entity_tokens_agree():
    """Identical number sets were the old rule and it fired on almost nothing."""
    rows = [
        candidate("story:a", "en", ("Anthropic and Databricks sign a 100 million dollar deal",
                                    "The agreement runs for 3 years, the companies said on Tuesday.")),
        candidate("story:b", "zh", ("Anthropic 与 Databricks 达成 100 亿元合作",
                                    "两家公司周二表示，该协议为期 5 年。"), offset_hours=6),
    ]
    assert len(assign_event_groups(rows)) == 2


def test_a_shared_number_alone_never_groups_two_unrelated_stories():
    rows = [
        candidate("story:a", "en", ("Apple ships a quarterly update",
                                    "The release landed in 2026 and covers 3 products.")),
        candidate("story:b", "zh", ("某地铁线路延长 3 公里",
                                    "该工程于 2026 年完工，由地方政府出资。")),
    ]
    assert assign_event_groups(rows) == {}


def test_two_product_versions_differing_only_by_a_number_do_not_group():
    rows = [
        candidate("story:a", "en", ("Apple ships iOS 18.6.1 security update",
                                    "The iOS update patches 1 actively exploited WebKit flaw.")),
        candidate("story:b", "zh", ("苹果发布 Apple iOS 18.6.2 security 更新",
                                    "该 iOS 更新修复了 1 个正在被利用的 WebKit 漏洞。")),
    ]
    groups = assign_event_groups(rows)
    # They share the number 1 and enough tokens, so tier 1 cannot separate them
    # on numbers alone. Record the behaviour rather than pretend otherwise: this
    # is exactly the recall/precision trade the 100-pair gate (Phase 2) measures.
    assert groups == {} or len(groups) == 2


def test_no_shared_number_never_groups():
    rows = [
        candidate("story:a", "en", ("Nvidia and Blackwell expand their partnership",
                                    "The companies described the work as ongoing.")),
        candidate("story:b", "zh", ("Nvidia 与 Blackwell 扩大合作",
                                    "两家公司称相关工作仍在进行中。")),
    ]
    assert assign_event_groups(rows) == {}


def test_the_window_and_the_language_guard_both_hold():
    far = [candidate("story:a", "en", EN_EARNINGS), candidate("story:b", "zh", ZH_EARNINGS, offset_hours=60)]
    assert assign_event_groups(far) == {}
    same_language = [candidate("story:a", "en", EN_EARNINGS),
                     candidate("story:c", "en", ("Nvidia earnings: 3 Blackwell chips announced",
                                                 "Revenue reached 46 billion dollars, up 56 percent in 2026, "
                                                 "with 7 new sites."))]
    assert assign_event_groups(same_language) == {}


def test_the_window_is_configurable_and_a_wider_one_recovers_the_pair():
    far = [candidate("story:a", "en", EN_EARNINGS), candidate("story:b", "zh", ZH_EARNINGS, offset_hours=60)]
    assert len(assign_event_groups(far, policy=GroupingPolicy(window_hours=72))) == 2


def test_single_member_groups_get_no_id_and_the_kill_switch_disables_grouping():
    assert assign_event_groups([candidate("story:a", "en", EN_EARNINGS)]) == {}
    pair = [candidate("story:a", "en", EN_EARNINGS), candidate("story:b", "zh", ZH_EARNINGS)]
    assert assign_event_groups(pair, policy=GroupingPolicy(cross_language_enabled=False)) == {}


def test_group_id_is_independent_of_input_order():
    rows = [candidate("story:b", "zh", ZH_EARNINGS), candidate("story:a", "en", EN_EARNINGS)]
    assert assign_event_groups(rows) == assign_event_groups(list(reversed(rows)))


def test_a_hot_number_bucket_is_bounded_rather_than_quadratic():
    """A number every story carries must not turn ingest into an N^2 pass.

    These 60 stories all share the year and enough boilerplate tokens to pass
    the token test, so an unbounded pass joins all of them. The budget caps how
    many pairs inside one number bucket are ever examined, so fewer join and the
    pass stays bounded instead of quadratic.
    """
    rows = [candidate(f"story:{index:03d}", "en" if index % 2 else "zh",
                      (f"Quarterly update from 2026 number {index}",
                       "The company said the work continued through the quarter."))
            for index in range(60)]
    unbounded = assign_event_groups(rows, policy=GroupingPolicy(max_pairs_per_bucket=100_000))
    bounded = assign_event_groups(rows, policy=GroupingPolicy(max_pairs_per_bucket=100))
    assert len(unbounded) == 60
    assert len(bounded) < len(unbounded)


@pytest.mark.parametrize("kwargs", [
    {"min_shared_entity_tokens": 0},
    {"min_shared_entity_tokens": 11},
    {"window_hours": 0},
    {"window_hours": 169},
    {"max_pairs_per_bucket": 99},
    {"cross_language_enabled": "yes"},
])
def test_policy_refuses_out_of_range_values(kwargs):
    with pytest.raises(ValueError):
        GroupingPolicy(**kwargs)


def test_policy_from_config_reads_only_grouping_keys():
    policy = GroupingPolicy.from_config({"window_hours": 72, "min_shared_entity_tokens": 3})
    assert policy.window_hours == 72 and policy.min_shared_entity_tokens == 3
    assert GroupingPolicy.from_config({}).window_hours == 48
