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


def test_check_5_the_cap_may_not_be_below_what_the_window_already_holds(document):
    """The window holds 2 pages. A cap of 1 would mean the run promises fewer
    pages than it has already ranked and paid for."""
    document["run"]["max_pages_per_run"] = 1
    with pytest.raises(CompositionPolicyError, match="max_pages_per_run"):
        parse_composition_policy(document)


def test_check_5_a_cap_above_the_window_is_allowed_because_pages_continue(document):
    """Pages past the frozen order are continuations composed by the recipe with
    no model call, so the cap may exceed what the window itself holds."""
    document["run"]["max_pages_per_run"] = 6
    assert parse_composition_policy(document).max_pages_per_run == 6


def test_check_9_the_general_pool_must_fit_one_call(document):
    """The pool is fetched one page wider than the window, in one call that
    returns at most 100 rows. A pair that cannot be served is refused rather
    than clamped, which is how "wider" silently became "the same size"."""
    document["composition"]["candidate_window_size"] = 100
    document["composition"]["page_size"] = 25
    document["run"]["max_pages_per_run"] = 4
    with pytest.raises(CompositionPolicyError, match="must not exceed 100"):
        parse_composition_policy(document)
    # Exactly 100 is the boundary and is allowed.
    document["composition"]["candidate_window_size"] = 75
    assert parse_composition_policy(document).candidate_window_size == 75


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


def test_check_8_retention_must_cover_the_window_the_feed_reads_back(document):
    """The prune deletes by published_at. A retention shorter than the window the
    feed reads back would delete rows the hot lane is still counting, and hot
    would quietly read as zero."""
    document["trend"]["window_hours"] = 72
    document["exploration"]["max_age_hours"] = 48
    with pytest.raises(CompositionPolicyError, match="retention"):
        parse_composition_policy(document, retention_days=2)
    # Exactly enough is enough: 3 days is 72 hours.
    assert parse_composition_policy(document, retention_days=3).trend_window_hours == 72


def test_check_8_also_covers_the_exploration_window(document):
    document["trend"]["window_hours"] = 24
    document["exploration"]["max_age_hours"] = 168
    with pytest.raises(CompositionPolicyError, match="exploration.max_age_hours"):
        parse_composition_policy(document, retention_days=3)
    assert parse_composition_policy(document, retention_days=7).exploration_max_age_hours == 168


def test_the_shipped_pair_of_files_agrees():
    from curator.recommendation.composition import configured_retention_days
    retention = configured_retention_days(POLICY_PATH.parents[1] / "sources.yaml")
    assert retention == 14
    load_composition_policy(POLICY_PATH, retention_days=retention)


def test_a_deployment_without_the_key_still_boots(document):
    """None means "not configured", not "zero". A deployment that has not adopted
    the key yet must boot exactly as before."""
    assert parse_composition_policy(document, retention_days=None).trend_window_hours == 24
