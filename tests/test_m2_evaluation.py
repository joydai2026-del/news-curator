import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from curator.m2_evaluation import ndcg, reduce


# All identifiers and timestamps below are synthetic protocol cases, not news captures.
ALL_METRICS = ("F1", "F2", "F3", "F4", "F5", "F6", "R1", "R2", "R3", "R4", "R5", "R6", "S1", "D1", "O1", "L1", "L2", "Q1", "Q2", "Q3", "Q4", "P1", "P2", "P3", "P4")
BIND={"schema_version":1,"git_commit_sha":"b" * 40,"policy_sha256":"a" * 64,"checklist_sha256":"c" * 64,"observed_at_utc":"2026-09-14T00:00:00Z"}


def policy(*, headline=("F1",), guardrail=(), metrics=ALL_METRICS):
    return {
        "schema_version": 1,
        "metrics": {
            **{metric_id: {} for metric_id in metrics},
            "F1": {"quality_target": {"minimum_items_per_class": 1, "minimum_days": 1}},
            "F3": {"quality_target": {"overall_min": .85, "must_surface_min": .95, "minimum_items": 1, "minimum_days": 1}, "freshness_definition": "trusted_publisher_published_at_with_age_at_decision_at_most_24_hours"},
            "R1": {"quality_target": {"minimum": .75, "minimum_judged_slates": 1, "minimum_days": 1}},
            "R6": {"quality_target": {"minimum": .80, "minimum_queries": 1}},
            "S1": {"slices": ["each_configured_category", "English", "Chinese"], "quality_target": {"minimum": .60, "minimum_items_per_slice": 1}},
            "L1": {"quality_target": {"p95_maximum": 60, "minimum_events": 1, "minimum_days": 1}},
            "L2": {"quality_target": {"p95_maximum": 60, "minimum_events": 1, "minimum_days": 1}},
        },
        "M2_metric_roles": {"headline": list(headline), "guardrail": list(guardrail), "diagnostic": []},
        "evaluation": {"shared_M2_evidence_window": {"minimum_independent_days": 1}},
        "source_classes": {"fast_stream": {"target_p95_independent_first_seen_to_ready_minutes": 15}},
    }


POLICY = policy()

def test_unbound_or_empty_evidence_never_passes_and_preserves_ids():
    result=reduce(POLICY, [])
    assert result["status"] == "insufficient_evidence"
    assert set(result["metrics"]) == set(ALL_METRICS)

def test_ndcg_keeps_missing_retrieval_as_zero_gain():
    assert ndcg(["low"], {"high":3,"low":1}, ["high","low"]) < 1

def test_l2_newest_profile_mismatch_is_hard_fail():
    doc={**BIND,"profile_visibility":[{"used_profile_version":"old","newest_committed_profile_version":"new","ranking_request_received_at":"2026-09-14T00:00:00Z","ranking_response_completed_at":"2026-09-14T00:00:01Z"}]}
    assert reduce(POLICY,[doc])["metrics"]["L2"]["status"] == "fail"


def test_bad_schema_and_hash_syntax_are_rejected_fail_closed():
    result = reduce(POLICY, [{**BIND, "schema_version": 2, "policy_sha256": "not-a-sha"}])
    assert result["status"] == "insufficient_evidence"
    assert result["binding_error"] == "receipt schema version does not match policy"


def test_f1_unknown_or_missing_declared_class_cannot_pass():
    unknown = {**BIND, "freshness": [{"source_class": "unregistered", "independent_first_seen_at": "2026-09-14T00:00:00Z", "ready_at": "2026-09-14T00:01:00Z"}]}
    assert reduce(POLICY, [unknown])["metrics"]["F1"]["status"] == "insufficient_evidence"
    two_classes = policy()
    two_classes["source_classes"]["scheduled_feed"] = {"target_p95_independent_first_seen_to_ready_minutes": 45}
    known_only = {**BIND, "freshness": [{"source_class": "fast_stream", "independent_first_seen_at": "2026-09-14T00:00:00Z", "ready_at": "2026-09-14T00:01:00Z"}]}
    assert reduce(two_classes, [known_only])["metrics"]["F1"]["status"] == "insufficient_evidence"


def test_r1_duplicate_and_short_slates_never_inflate_precision():
    duplicate = {**BIND, "ranked_slates": [{"observed_at_utc": "2026-09-14T00:00:00Z", "eligible_count": 2, "returned_ids": ["protocol-a", "protocol-a"], "grades": {"protocol-a": 3}}]}
    assert reduce(policy(headline=("R1",)), [duplicate])["metrics"]["R1"]["status"] == "insufficient_evidence"
    returned = [f"protocol-{index}" for index in range(15)]
    short = {**BIND, "ranked_slates": [{"observed_at_utc": "2026-09-14T00:00:00Z", "eligible_count": 20, "returned_ids": returned, "grades": {item: 2 for item in returned}}]}
    result = reduce(policy(headline=("R1",)), [short])["metrics"]["R1"]
    assert result["status"] == "fail"
    assert result["value"] == pytest.approx(.75)


def test_ndcg_rejects_unordered_or_unknown_judgments_and_keeps_misses_zero_gain():
    assert ndcg(["low"], {"high": 3, "low": 1}, ["high", "low"]) < 1
    with pytest.raises(ValueError):
        ndcg(["high"], {"high": 3, "low": 1}, ["low", "high"])
    malformed = {**BIND, "search_queries": [{"observed_at_utc": "2026-09-14T00:00:00Z", "answerable": True, "candidate_ids": ["high"], "returned_ids": ["unknown"], "ideal_ids": ["high"], "grades": {"high": 3}}]}
    assert reduce(policy(headline=("R6",)), [malformed])["metrics"]["R6"]["status"] == "insufficient_evidence"


def test_rollup_excludes_future_metric_ids_but_requires_declared_headlines_and_guardrails():
    document = {**BIND, "freshness": [{"source_class": "fast_stream", "independent_first_seen_at": "2026-09-14T00:00:00Z", "ready_at": "2026-09-14T00:01:00Z"}]}
    result = reduce(POLICY, [document])
    assert result["status"] == "pass"
    assert result["metrics"]["P2"]["status"] == "pending"
    required_but_unevaluated = reduce(policy(headline=("F1", "F3", "L1"), guardrail=("S1",)), [document])
    assert required_but_unevaluated["status"] == "insufficient_evidence"
    assert required_but_unevaluated["metrics"]["F3"]["status"] == "insufficient_evidence"
    assert required_but_unevaluated["metrics"]["L1"]["status"] == "insufficient_evidence"
    assert required_but_unevaluated["metrics"]["S1"]["status"] == "insufficient_evidence"


def test_f3_uses_frozen_fresh_sentinels_and_must_surface_floor():
    # Controlled protocol cases, not news captures or production measurements.
    row = {"candidate_id": "protocol-f3-must", "eligible": True, "relevance_grade": 3, "present_in_corpus": True, "trusted_publisher_published_at": "2026-09-14T00:00:00Z", "decision_at": "2026-09-14T01:00:00Z"}
    result = reduce(policy(headline=("F3",)), [{**BIND, "coverage_sentinels": [row]}])
    assert result["metrics"]["F3"]["status"] == "pass"
    stale = {**row, "candidate_id": "protocol-f3-stale", "decision_at": "2026-09-15T01:00:00Z"}
    assert reduce(policy(headline=("F3",)), [{**BIND, "coverage_sentinels": [stale]}])["metrics"]["F3"]["status"] == "insufficient_evidence"
    missed = {**row, "present_in_corpus": False}
    assert reduce(policy(headline=("F3",)), [{**BIND, "coverage_sentinels": [missed]}])["metrics"]["F3"]["status"] == "fail"


def test_l1_requires_ordered_history_receipt_and_measures_commit_latency():
    # Controlled protocol cases, not user interactions or production measurements.
    row = {"event_id": "protocol-l1-event", "ordered_history_event_ids": ["protocol-l1-earlier", "protocol-l1-event"], "committed_profile_version": "protocol-v2", "settled_interaction_at": "2026-09-14T00:00:00Z", "profile_version_committed_at": "2026-09-14T00:00:30Z"}
    result = reduce(policy(headline=("L1",)), [{**BIND, "profile_updates": [row]}])
    assert result["metrics"]["L1"] == {"status": "pass", "value": 30.0, "reason": "policy-defined p95 settled-interaction-to-committed-profile seconds"}
    unordered = {**row, "ordered_history_event_ids": ["protocol-l1-event", "protocol-l1-earlier"]}
    assert reduce(policy(headline=("L1",)), [{**BIND, "profile_updates": [unordered]}])["metrics"]["L1"]["status"] == "insufficient_evidence"


def test_s1_requires_every_frozen_marginal_category_and_language_slice():
    # Controlled protocol judgments, not measured relevance results.
    rows = [
        {"judgment_id": "protocol-s1-1", "slate_id": "protocol-slate", "rank": 1, "primary_category": "world", "original_language": "English", "relevance_grade": 3, "observed_at_utc": "2026-09-14T00:00:00Z"},
        {"judgment_id": "protocol-s1-2", "slate_id": "protocol-slate", "rank": 2, "primary_category": "science", "original_language": "Chinese", "relevance_grade": 3, "observed_at_utc": "2026-09-14T00:00:00Z"},
    ]
    document = {**BIND, "configured_category_ids": ["world", "science"], "slice_judgments": rows}
    assert reduce(policy(headline=("F1",), guardrail=("S1",)), [{**document, "freshness": [{"source_class": "fast_stream", "independent_first_seen_at": "2026-09-14T00:00:00Z", "ready_at": "2026-09-14T00:01:00Z"}]}])["metrics"]["S1"]["status"] == "pass"
    missing = {**document, "configured_category_ids": ["world"]}
    assert reduce(policy(guardrail=("S1",)), [missing])["metrics"]["S1"]["status"] == "insufficient_evidence"
    low = {**document, "slice_judgments": [{**rows[0], "relevance_grade": 0}, rows[1]]}
    assert reduce(policy(guardrail=("S1",)), [low])["metrics"]["S1"]["status"] == "fail"


def test_cli_binds_receipts_to_loaded_policy_and_checklist_bytes(tmp_path: Path):
    # Controlled protocol receipt, not a production evaluation result.
    policy_path, checklist_path, receipt_path, output_path = (tmp_path / name for name in ("policy.yaml", "checklist.yaml", "receipt.json", "result.json"))
    policy_bytes = yaml.safe_dump(policy(headline=("F1",)), sort_keys=True).encode()
    checklist_bytes = b"schema_version: 1\nprotocol: controlled\n"
    policy_path.write_bytes(policy_bytes)
    checklist_path.write_bytes(checklist_bytes)
    receipt = {**BIND, "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(), "checklist_sha256": hashlib.sha256(checklist_bytes).hexdigest(), "freshness": [{"source_class": "fast_stream", "independent_first_seen_at": "2026-09-14T00:00:00Z", "ready_at": "2026-09-14T00:01:00Z"}]}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    command = [sys.executable, "scripts/evaluate_m2.py", "--policy", str(policy_path), "--checklist", str(checklist_path), "--receipt", str(receipt_path), "--output", str(output_path)]
    assert subprocess.run(command, cwd=Path(__file__).parents[1], check=False).returncode == 0
    receipt["policy_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert subprocess.run(command, cwd=Path(__file__).parents[1], check=False).returncode == 2
    assert json.loads(output_path.read_text())["binding_error"] == "receipt policy sha256 does not match loaded policy"
