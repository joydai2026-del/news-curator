"""Startup validation for the M2.1 composition policy.

Every check asserts the BOOT FAILS rather than clamping. A clamped value is a
feed nobody configured, discovered weeks later from the output.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from curator.recommendation.composition import (
    CAPTURED_ACTIONS,
    CompositionPolicyError,
    load_composition_policy,
    parse_composition_policy,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"


@pytest.fixture()
def document():
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_shipped_policy_loads_and_carries_the_ratified_values():
    policy = load_composition_policy(POLICY_PATH)
    # JJ ratified 3 surprise cards of 25 and a 60 minute reading run.
    assert round(policy.lane_ratios["surprise"] * policy.page_size) == 3
    assert policy.idle_minutes == 60
    assert policy.exclusive_promote_to_all_max == 2
    assert policy.surprise_label_enabled is True
    assert policy.lane_quota("interested", 25) == 11
    assert policy.lane_quota("updates", 25) == 7
    assert policy.lane_quota("hot", 25) == 4


def test_every_lane_carries_a_reader_visible_label(document):
    policy = parse_composition_policy(document)
    assert {policy.label_for(lane) for lane in ("updates", "hot", "interested", "surprise")} == {
        "fresh", "hot", "for you", "surprise"}
    # The exclusive label is derived from the reader's language, never stored.
    assert policy.exclusive_label("en") == "only in Chinese press"
    assert policy.exclusive_label("zh") == "only in English press"


def test_check_1_lane_ratios_must_sum_to_one(document):
    document["composition"]["lane_ratios"]["surprise"] = 0.30
    with pytest.raises(CompositionPolicyError, match="sum to 1.0"):
        parse_composition_policy(document)


@pytest.mark.parametrize("path,value", [
    (("composition", "page_size"), 26),
    (("composition", "candidate_window_size"), 5),
    (("trend", "min_independent_sources"), 0),
    (("diversity", "calibration_alarm_kl"), 3.0),
    (("run", "idle_minutes"), 1),
    (("lane", "exclusive_promote_to_all_max"), 9),
])
def test_check_2_out_of_range_values_fail_the_boot(document, path, value):
    node = document
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(CompositionPolicyError):
        parse_composition_policy(document)


def test_check_2_a_truthy_string_is_not_a_boolean(document):
    document["run"]["immediate_negative_filter"] = "true"
    with pytest.raises(CompositionPolicyError, match="must be a boolean"):
        parse_composition_policy(document)


def test_check_2_a_boolean_is_not_a_count(document):
    document["trend"]["window_hours"] = True
    with pytest.raises(CompositionPolicyError):
        parse_composition_policy(document)


def test_check_3_gate_action_must_be_a_predicted_action(document):
    document["scoring"]["gate_action"] = "dwell"
    with pytest.raises(CompositionPolicyError, match="predicted action"):
        parse_composition_policy(document)
    assert document["scoring"]["gate_action"] not in CAPTURED_ACTIONS


def test_check_4_window_must_be_at_least_one_page(document):
    document["composition"]["candidate_window_size"] = 20
    document["composition"]["page_size"] = 25
    document["run"]["max_pages_per_run"] = 1
    with pytest.raises(CompositionPolicyError, match="at least"):
        parse_composition_policy(document)


def test_check_5_max_pages_must_match_the_window_arithmetic(document):
    document["run"]["max_pages_per_run"] = 4
    with pytest.raises(CompositionPolicyError, match="max_pages_per_run"):
        parse_composition_policy(document)


def test_check_5_raising_the_window_raises_the_reachable_page_count(document):
    document["composition"]["candidate_window_size"] = 75
    document["run"]["max_pages_per_run"] = 3
    assert parse_composition_policy(document).max_pages_per_run == 3


def test_check_7_a_weight_for_an_uncaptured_action_fails(document):
    for uncaptured in ("ask_question", "dwell", "dismiss"):
        candidate = copy.deepcopy(document)
        candidate["engagement_weights"][uncaptured] = 1.0
        with pytest.raises(CompositionPolicyError, match="does not capture"):
            parse_composition_policy(candidate)


def test_missing_key_fails_rather_than_defaulting(document):
    del document["diversity"]["topic_window_k"]
    with pytest.raises(CompositionPolicyError, match="must be configured"):
        parse_composition_policy(document)


def test_lane_priority_must_order_every_lane_once(document):
    document["lane_priority"] = ["updates", "updates", "hot", "interested"]
    with pytest.raises(CompositionPolicyError, match="lane_priority"):
        parse_composition_policy(document)
