"""Fail-closed reducer for bound M2 evaluation receipts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import log2
import re
from typing import Any, Iterable


_GIT_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_K = 20  # The policy defines these metrics as @20; this is not a threshold.
_VALID_GRADES = {0, 1, 2, 3}


def _at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _p95(values: list[float]) -> float:
    if not values:
        raise ValueError("p95 requires observations")
    return sorted(values)[max(0, int(len(values) * .95 + .999999) - 1)]


def _days(rows: Iterable[dict[str, Any]], key: str) -> int:
    return len({_at(row[key]).date() for row in rows})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_ids(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value) and len(value) == len(set(value))


def _valid_grades(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(item, str) and item and _is_int(grade) and grade in _VALID_GRADES
        for item, grade in value.items()
    )


def _quality(policy: dict[str, Any], metric_id: str) -> dict[str, Any] | None:
    metric = policy.get("metrics", {}).get(metric_id)
    value = metric.get("quality_target") if isinstance(metric, dict) else None
    return value if isinstance(value, dict) else None


def _shared_window(policy: dict[str, Any]) -> dict[str, Any] | None:
    value = policy.get("evaluation", {}).get("shared_M2_evidence_window")
    return value if isinstance(value, dict) else None


def _minimum_days(policy: dict[str, Any], metric_id: str) -> int | None:
    value = (_quality(policy, metric_id) or {}).get("minimum_days", (_shared_window(policy) or {}).get("minimum_independent_days"))
    return value if _is_int(value) and value > 0 else None


def _status(status: str, value: Any, reason: str) -> dict[str, Any]:
    return {"status": status, "value": value, "reason": reason}


def validate_bindings(
    policy: dict[str, Any],
    documents: list[dict[str, Any]],
    *,
    expected_policy_sha256: str | None = None,
    expected_checklist_sha256: str | None = None,
) -> str | None:
    required = ("schema_version", "git_commit_sha", "policy_sha256", "checklist_sha256", "observed_at_utc")
    expected_schema = policy.get("schema_version")
    if not _is_int(expected_schema):
        return "invalid policy schema version"
    if expected_policy_sha256 is not None and (not isinstance(expected_policy_sha256, str) or not _SHA256.fullmatch(expected_policy_sha256)):
        return "invalid loaded policy sha256"
    if expected_checklist_sha256 is not None and (not isinstance(expected_checklist_sha256, str) or not _SHA256.fullmatch(expected_checklist_sha256)):
        return "invalid loaded checklist sha256"
    if not documents or any(not isinstance(document, dict) or any(key not in document or not document[key] for key in required) for document in documents):
        return "missing receipt binding"
    reference = tuple(documents[0][key] for key in required[1:4])
    for document in documents:
        if document["schema_version"] != expected_schema:
            return "receipt schema version does not match policy"
        if not isinstance(document["git_commit_sha"], str) or not _GIT_SHA.fullmatch(document["git_commit_sha"]):
            return "invalid git commit hash"
        if any(not isinstance(document[key], str) or not _SHA256.fullmatch(document[key]) for key in ("policy_sha256", "checklist_sha256")):
            return "invalid receipt sha256 binding"
        if tuple(document[key] for key in required[1:4]) != reference:
            return "mixed receipt revisions"
        if expected_policy_sha256 is not None and document["policy_sha256"] != expected_policy_sha256:
            return "receipt policy sha256 does not match loaded policy"
        if expected_checklist_sha256 is not None and document["checklist_sha256"] != expected_checklist_sha256:
            return "receipt checklist sha256 does not match loaded checklist"
        try:
            _at(document["observed_at_utc"])
        except ValueError:
            return "invalid receipt timestamp"
    return None


def _validate_ndcg(returned_ids: Any, grades: Any, ideal_ids: Any, candidate_ids: Any | None = None) -> None:
    if not _valid_ids(returned_ids) or not _valid_ids(ideal_ids) or not _valid_grades(grades):
        raise ValueError("invalid judged identifiers or grades")
    if not set(returned_ids).issubset(grades) or not set(ideal_ids).issubset(grades):
        raise ValueError("unknown returned or ideal identifier")
    if any(grades[ideal_ids[index]] < grades[ideal_ids[index + 1]] for index in range(len(ideal_ids) - 1)):
        raise ValueError("ideal ids are not grade ordered")
    if candidate_ids is not None and (not _valid_ids(candidate_ids) or set(candidate_ids) != set(grades) or ideal_ids != candidate_ids):
        raise ValueError("candidate pool, judgments, and ideal order do not match")


def ndcg(returned_ids: list[str], grades: dict[str, int], ideal_ids: list[str]) -> float:
    _validate_ndcg(returned_ids, grades, ideal_ids)
    def dcg(ids: list[str]) -> float:
        return sum((2 ** grades[item] - 1) / log2(rank + 2) for rank, item in enumerate(ids[:_TOP_K]))
    denominator = dcg(ideal_ids)
    return dcg(returned_ids) / denominator if denominator else 0.0


def _reduce_f1(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, source_classes = _quality(policy, "F1"), policy.get("source_classes")
    minimum_items, minimum_days = (quality or {}).get("minimum_items_per_class"), _minimum_days(policy, "F1")
    if not isinstance(source_classes, dict) or not (_is_int(minimum_items) and minimum_items > 0 and minimum_days):
        return _status("insufficient_evidence", None, "F1 policy thresholds are incomplete")
    required = {name: config["target_p95_independent_first_seen_to_ready_minutes"] for name, config in source_classes.items() if isinstance(config, dict) and isinstance(config.get("target_p95_independent_first_seen_to_ready_minutes"), (int, float))}
    if not required:
        return _status("insufficient_evidence", None, "F1 has no source-class targets")
    if any(not isinstance(row, dict) or not row.get("independent_first_seen_at") or not row.get("ready_at") for row in rows):
        return _status("fail", None, "eligible observations missing ready timestamps remain denominator failures")
    classes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        for row in rows:
            source_class = row.get("source_class")
            if source_class not in required:
                return _status("insufficient_evidence", None, "unknown or unsupported F1 source class")
            _at(row["independent_first_seen_at"])
            _at(row["ready_at"])
            classes[source_class].append(row)
        if any(len(classes[name]) < minimum_items or _days(classes[name], "independent_first_seen_at") < minimum_days for name in required):
            return _status("insufficient_evidence", None, "requires policy sample floor for every declared F1 source class")
        lags = {name: _p95([max(0.0, (_at(row["ready_at"]) - _at(row["independent_first_seen_at"])).total_seconds() / 60) for row in source_rows]) for name, source_rows in classes.items()}
    except (KeyError, ValueError):
        return _status("insufficient_evidence", None, "invalid F1 timestamp evidence")
    return _status("pass" if all(lags[name] <= required[name] for name in required) else "fail", lags, "policy-defined p95 observation-to-ready minutes by source class")


def _reduce_r1(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, minimum_days = _quality(policy, "R1"), _minimum_days(policy, "R1")
    minimum_slates, target = (quality or {}).get("minimum_judged_slates"), (quality or {}).get("minimum")
    if not (_is_int(minimum_slates) and minimum_slates > 0 and minimum_days and isinstance(target, (int, float))):
        return _status("insufficient_evidence", None, "R1 policy thresholds are incomplete")
    values: list[float] = []
    fill_failure = False
    try:
        for row in rows:
            returned, grades, eligible = row.get("returned_ids"), row.get("grades"), row.get("eligible_count")
            if not _valid_ids(returned) or not _valid_grades(grades) or not _is_int(eligible) or eligible < 0:
                return _status("insufficient_evidence", None, "malformed R1 slate evidence")
            _at(row["observed_at_utc"])
            if eligible == 0:
                return _status("insufficient_evidence", None, "no eligible candidates is not a pass")
            denominator = min(_TOP_K, eligible)
            if any(item not in grades for item in returned) or len(returned) > denominator:
                return _status("insufficient_evidence", None, "unknown or oversized R1 returned slate")
            fill_failure = fill_failure or len(returned) < denominator
            values.append(sum(grades[item] >= 2 for item in returned) / denominator)
        if len(rows) < minimum_slates or _days(rows, "observed_at_utc") < minimum_days:
            return _status("insufficient_evidence", None, "requires policy judged-slate floor across policy days")
    except (AttributeError, KeyError, ValueError):
        return _status("insufficient_evidence", None, "invalid R1 timestamp evidence")
    value = sum(values) / len(values)
    return _status("pass" if value >= target and not fill_failure else "fail", value, "P@20 with every missing required slot retained as zero gain")


def _reduce_r6(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, minimum_days = _quality(policy, "R6"), _minimum_days(policy, "R6")
    minimum_queries, target = (quality or {}).get("minimum_queries"), (quality or {}).get("minimum")
    if not (_is_int(minimum_queries) and minimum_queries > 0 and minimum_days and isinstance(target, (int, float))):
        return _status("insufficient_evidence", None, "R6 policy thresholds are incomplete")
    values: list[float] = []
    answerable: list[dict[str, Any]] = []
    try:
        for row in rows:
            if not isinstance(row.get("answerable"), bool):
                return _status("insufficient_evidence", None, "every query requires answerability classification")
            _at(row["observed_at_utc"])
            if not row["answerable"]:
                continue
            _validate_ndcg(row.get("returned_ids"), row.get("grades"), row.get("ideal_ids"), row.get("candidate_ids"))
            values.append(ndcg(row["returned_ids"], row["grades"], row["ideal_ids"]))
            answerable.append(row)
        if len(answerable) < minimum_queries or _days(answerable, "observed_at_utc") < minimum_days:
            return _status("insufficient_evidence", None, "requires policy answerable-query floor across policy days")
    except (KeyError, ValueError, AttributeError):
        return _status("insufficient_evidence", None, "unjudged, unknown, or malformed R6 query evidence")
    value = sum(values) / len(values)
    return _status("pass" if value >= target else "fail", value, "nDCG@20 with retrieval misses retained at zero gain")


def _reduce_l2(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, minimum_days = _quality(policy, "L2"), _minimum_days(policy, "L2")
    minimum_events, target = (quality or {}).get("minimum_events"), (quality or {}).get("p95_maximum")
    if not (_is_int(minimum_events) and minimum_events > 0 and minimum_days and isinstance(target, (int, float))):
        return _status("insufficient_evidence", None, "L2 policy thresholds are incomplete")
    latencies: list[float] = []
    try:
        for row in rows:
            used, newest = row.get("used_profile_version"), row.get("newest_committed_profile_version")
            if not isinstance(used, str) or not used or not isinstance(newest, str) or not newest:
                return _status("insufficient_evidence", None, "missing L2 profile-version evidence")
            if used != newest:
                return _status("fail", None, "first subsequent request did not use newest committed profile")
            request_at, completed_at = _at(row["ranking_request_received_at"]), _at(row["ranking_response_completed_at"])
            if completed_at < request_at:
                return _status("insufficient_evidence", None, "negative L2 response latency")
            latencies.append((completed_at - request_at).total_seconds())
        if len(rows) < minimum_events or _days(rows, "ranking_request_received_at") < minimum_days:
            return _status("insufficient_evidence", None, "requires policy profile-event floor across policy days")
    except (KeyError, ValueError, AttributeError):
        return _status("insufficient_evidence", None, "invalid L2 timestamp evidence")
    value = _p95(latencies)
    return _status("pass" if value <= target else "fail", value, "policy-defined p95 seconds plus newest-profile invariant")


def _freshness_limit_hours(policy: dict[str, Any]) -> int | None:
    definition = policy.get("metrics", {}).get("F3", {}).get("freshness_definition")
    if not isinstance(definition, str):
        return None
    match = re.fullmatch(r"trusted_publisher_published_at_with_age_at_decision_at_most_(\d+)_hours", definition)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _reduce_f3(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, minimum_days, freshness_hours = _quality(policy, "F3"), _minimum_days(policy, "F3"), _freshness_limit_hours(policy)
    minimum_items = (quality or {}).get("minimum_items")
    overall_target, must_target = (quality or {}).get("overall_min"), (quality or {}).get("must_surface_min")
    if not (_is_int(minimum_items) and minimum_items > 0 and minimum_days and freshness_hours and isinstance(overall_target, (int, float)) and isinstance(must_target, (int, float))):
        return _status("insufficient_evidence", None, "F3 policy thresholds or freshness definition are incomplete")
    eligible: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    try:
        for row in rows:
            identifier, grade = row.get("candidate_id"), row.get("relevance_grade")
            if not isinstance(identifier, str) or not identifier or identifier in identifiers or not isinstance(row.get("eligible"), bool) or not _is_int(grade) or grade not in _VALID_GRADES or not isinstance(row.get("present_in_corpus"), bool):
                return _status("insufficient_evidence", None, "malformed or duplicate F3 sentinel evidence")
            identifiers.add(identifier)
            published_at, decision_at = _at(row["trusted_publisher_published_at"]), _at(row["decision_at"])
            age_hours = (decision_at - published_at).total_seconds() / 3600
            if age_hours < 0:
                return _status("insufficient_evidence", None, "F3 sentinel has a future publisher timestamp")
            if row["eligible"] and grade >= 2 and age_hours <= freshness_hours:
                eligible.append(row)
        if len(eligible) < minimum_items or _days(eligible, "decision_at") < minimum_days:
            return _status("insufficient_evidence", None, "requires policy fresh eligible sentinel floor across policy days")
    except (AttributeError, KeyError, ValueError):
        return _status("insufficient_evidence", None, "invalid or missing F3 timestamp evidence")
    must_surface = [row for row in eligible if row["relevance_grade"] == 3]
    if not must_surface:
        return _status("insufficient_evidence", None, "F3 must-surface denominator is empty and cannot pass")
    overall = sum(row["present_in_corpus"] for row in eligible) / len(eligible)
    must = sum(row["present_in_corpus"] for row in must_surface) / len(must_surface)
    value = {"overall_recall": overall, "must_surface_recall": must, "eligible_fresh_items": len(eligible)}
    return _status("pass" if overall >= overall_target and must >= must_target else "fail", value, "policy-defined eligible fresh corpus recall and must-surface recall")


def _reduce_l1(policy: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    quality, minimum_days = _quality(policy, "L1"), _minimum_days(policy, "L1")
    minimum_events, target = (quality or {}).get("minimum_events"), (quality or {}).get("p95_maximum")
    if not (_is_int(minimum_events) and minimum_events > 0 and minimum_days and isinstance(target, (int, float))):
        return _status("insufficient_evidence", None, "L1 policy thresholds are incomplete")
    latencies: list[float] = []
    event_ids: set[str] = set()
    try:
        for row in rows:
            event_id, history, version = row.get("event_id"), row.get("ordered_history_event_ids"), row.get("committed_profile_version")
            if not isinstance(event_id, str) or not event_id or event_id in event_ids or not _valid_ids(history) or history[-1] != event_id or not isinstance(version, str) or not version:
                return _status("insufficient_evidence", None, "L1 requires ordered history and committed profile receipts")
            event_ids.add(event_id)
            settled_at, committed_at = _at(row["settled_interaction_at"]), _at(row["profile_version_committed_at"])
            if committed_at < settled_at:
                return _status("fail", None, "profile commit precedes its settled interaction")
            latencies.append((committed_at - settled_at).total_seconds())
        if len(rows) < minimum_events or _days(rows, "settled_interaction_at") < minimum_days:
            return _status("insufficient_evidence", None, "requires policy profile-event floor across policy days")
    except (AttributeError, KeyError, ValueError):
        return _status("insufficient_evidence", None, "invalid L1 event or timestamp evidence")
    value = _p95(latencies)
    return _status("pass" if value <= target else "fail", value, "policy-defined p95 settled-interaction-to-committed-profile seconds")


def _reduce_s1(policy: dict[str, Any], rows: list[dict[str, Any]], configured_categories: Any) -> dict[str, Any]:
    quality = _quality(policy, "S1")
    minimum_items, target = (quality or {}).get("minimum_items_per_slice"), (quality or {}).get("minimum")
    slices = policy.get("metrics", {}).get("S1", {}).get("slices")
    languages = [slice_id for slice_id in slices if isinstance(slice_id, str) and slice_id != "each_configured_category"] if isinstance(slices, list) else []
    if not (_is_int(minimum_items) and minimum_items > 0 and isinstance(target, (int, float)) and _valid_ids(configured_categories) and languages):
        return _status("insufficient_evidence", None, "S1 policy thresholds or configured marginal slices are incomplete")
    if any(not isinstance(row, dict) for row in rows):
        return _status("insufficient_evidence", None, "malformed S1 judgment evidence")
    categories = set(configured_categories)
    category_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    language_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    judgment_ids: set[str] = set()
    ranked_slots: set[tuple[str, int]] = set()
    try:
        for row in rows:
            judgment_id, slate_id, rank, grade = row.get("judgment_id"), row.get("slate_id"), row.get("rank"), row.get("relevance_grade")
            category, language = row.get("primary_category"), row.get("original_language")
            if not isinstance(judgment_id, str) or not judgment_id or judgment_id in judgment_ids or not isinstance(slate_id, str) or not slate_id or not _is_int(rank) or not 1 <= rank <= _TOP_K or not _is_int(grade) or grade not in _VALID_GRADES or category not in categories or language not in languages:
                return _status("insufficient_evidence", None, "S1 requires unique frozen marginal judgments in policy slices")
            slot = (slate_id, rank)
            if slot in ranked_slots:
                return _status("insufficient_evidence", None, "duplicate S1 slate rank can inflate a slice")
            judgment_ids.add(judgment_id)
            ranked_slots.add(slot)
            _at(row["observed_at_utc"])
            category_rows[category].append(row)
            language_rows[language].append(row)
    except (KeyError, ValueError):
        return _status("insufficient_evidence", None, "invalid S1 timestamp evidence")
    all_slices = {**category_rows, **language_rows}
    required = list(configured_categories) + languages
    if any(len(all_slices.get(slice_id, [])) < minimum_items for slice_id in required):
        return _status("insufficient_evidence", None, "requires policy sample floor for every configured category and language slice")
    values = {slice_id: sum(row["relevance_grade"] >= 2 for row in all_slices[slice_id]) / len(all_slices[slice_id]) for slice_id in required}
    return _status("pass" if min(values.values()) >= target else "fail", values, "policy-defined worst marginal category or language precision@20")


def _required_m2_metrics(policy: dict[str, Any]) -> list[str]:
    roles, definitions = policy.get("M2_metric_roles"), policy.get("metrics")
    if not isinstance(roles, dict) or not isinstance(definitions, dict):
        return []
    declared = {metric_id for role in ("headline", "guardrail", "diagnostic") for metric_id in roles.get(role, []) if isinstance(metric_id, str)}
    required = {metric_id for role in ("headline", "guardrail") for metric_id in roles.get(role, []) if isinstance(metric_id, str)}
    required.update(metric_id for metric_id in declared if isinstance(definitions.get(metric_id), dict) and "hard_gate" in definitions[metric_id])
    return sorted(required)


def reduce(
    policy: dict[str, Any],
    documents: list[dict[str, Any]],
    *,
    expected_policy_sha256: str | None = None,
    expected_checklist_sha256: str | None = None,
) -> dict[str, Any]:
    definitions = policy.get("metrics") if isinstance(policy, dict) else None
    if not isinstance(definitions, dict):
        return {"status": "insufficient_evidence", "binding_error": "invalid metrics policy", "metrics": {}}
    metrics = {metric_id: _status("pending", None, "no implemented evaluator evidence") for metric_id in definitions}
    required = _required_m2_metrics(policy)
    binding_error = validate_bindings(
        policy,
        documents,
        expected_policy_sha256=expected_policy_sha256,
        expected_checklist_sha256=expected_checklist_sha256,
    )
    if binding_error:
        return {"status": "insufficient_evidence", "binding_error": binding_error, "required_metrics": required, "metrics": metrics}
    configured_categories = None
    category_registries = [document.get("configured_category_ids") for document in documents if "configured_category_ids" in document]
    if category_registries:
        configured_categories = category_registries[0]
        if any(registry != configured_categories for registry in category_registries) or len(category_registries) != len(documents):
            configured_categories = None
    evaluators = {
        "F1": ("freshness", _reduce_f1),
        "F3": ("coverage_sentinels", _reduce_f3),
        "R1": ("ranked_slates", _reduce_r1),
        "R6": ("search_queries", _reduce_r6),
        "S1": ("slice_judgments", _reduce_s1),
        "L1": ("profile_updates", _reduce_l1),
        "L2": ("profile_visibility", _reduce_l2),
    }
    for metric_id, (key, evaluator) in evaluators.items():
        if metric_id in metrics and (metric_id in required or any(key in document for document in documents)):
            rows = [row for document in documents for row in document.get(key, [])]
            metrics[metric_id] = evaluator(policy, rows, configured_categories) if metric_id == "S1" else evaluator(policy, rows)
    if not required:
        overall = "insufficient_evidence"
    elif any(metrics.get(metric_id, {}).get("status") == "fail" for metric_id in required):
        overall = "fail"
    elif all(metrics.get(metric_id, {}).get("status") == "pass" for metric_id in required):
        overall = "pass"
    else:
        overall = "insufficient_evidence"
    return {"status": overall, "binding_error": None, "required_metrics": required, "metrics": metrics}
