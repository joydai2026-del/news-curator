#!/usr/bin/env python3
"""Score only complete human labels and report Wilson intervals when allowed."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import yaml


REQUIRED_LABELS = (
    "expected_target_language",
    "meaning_preserved",
    "proper_names_preserved",
    "headline_clear",
    "critical_terminology_violation",
)


class EvalSchemaError(ValueError):
    pass


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0 or successes < 0 or successes > total:
        raise ValueError("Wilson interval counts are invalid")
    proportion = successes / total
    denominator = 1.0 + (z * z / total)
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def load_dataset(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise EvalSchemaError("translation eval could not be loaded") from None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise EvalSchemaError("translation eval schema version is invalid")
    if raw.get("calibration_claim") != "prohibited":
        raise EvalSchemaError("translation eval must prohibit calibration claims")
    minimum = raw.get("minimum_reportable_cases")
    if not isinstance(minimum, int) or minimum < 100:
        raise EvalSchemaError("translation eval reporting minimum must be at least 100")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalSchemaError("translation eval must contain cases")
    ids: set[str] = set()
    for case in cases:
        _validate_case(case, ids)
    return raw


def score_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    labeled: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        labels = case.get("labels")
        if labels is not None:
            labeled.append(case)
    successes = sum(_acceptable(case["labels"]) for case in labeled)
    minimum = dataset["minimum_reportable_cases"]
    reportable = len(labeled) >= minimum
    result: dict[str, Any] = {
        "schema_version": 1,
        "dataset_name": dataset.get("dataset_name", "translation-bridge"),
        "total_cases": len(dataset["cases"]),
        "labeled_cases": len(labeled),
        "acceptable_cases": successes,
        "minimum_reportable_cases": minimum,
        "reportable": reportable,
        "quality_percentage": None,
        "wilson_95": None,
        "worst_slice": None,
        "calibration_status": "not_claimed",
    }
    if reportable:
        low, high = wilson_interval(successes, len(labeled))
        result["quality_percentage"] = round(100.0 * successes / len(labeled), 2)
        result["wilson_95"] = {"low": round(low, 6), "high": round(high, 6)}
        result["worst_slice"] = _worst_slice(labeled)
    elif labeled:
        result["status"] = "smoke_only_no_percentage"
    else:
        result["status"] = "unlabeled_no_score"
    return result


def _validate_case(case: object, ids: set[str]) -> None:
    if not isinstance(case, dict):
        raise EvalSchemaError("translation eval case must be a mapping")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id or case_id in ids:
        raise EvalSchemaError("translation eval case id is invalid or duplicated")
    ids.add(case_id)
    if case.get("source_language") not in {"en", "zh"}:
        raise EvalSchemaError("translation eval source language is invalid")
    if case.get("target_language") not in {"en", "zh"}:
        raise EvalSchemaError("translation eval target language is invalid")
    if case["source_language"] == case["target_language"]:
        raise EvalSchemaError("translation eval direction must cross languages")
    if not isinstance(case.get("source_text"), str) or not case["source_text"]:
        raise EvalSchemaError("translation eval source text is required")
    if not isinstance(case.get("source_fixture"), str) or not case["source_fixture"]:
        raise EvalSchemaError("translation eval source fixture is required")
    labels = case.get("labels")
    if labels is None:
        return
    if not isinstance(labels, dict) or set(labels) != set(REQUIRED_LABELS):
        raise EvalSchemaError("translation eval labels are incomplete")
    if any(not isinstance(labels[name], bool) for name in REQUIRED_LABELS):
        raise EvalSchemaError("translation eval labels must be booleans")


def _acceptable(labels: dict[str, bool]) -> bool:
    return bool(
        labels["expected_target_language"]
        and labels["meaning_preserved"]
        and labels["proper_names_preserved"]
        and labels["headline_clear"]
        and not labels["critical_terminology_violation"]
    )


def _worst_slice(cases: list[dict[str, Any]]) -> dict[str, Any]:
    slices: dict[str, list[bool]] = {}
    for case in cases:
        direction = f"{case['source_language']}-to-{case['target_language']}"
        category = str(case.get("category") or "uncategorized")
        for key in ("direction:" + direction, "category:" + category):
            slices.setdefault(key, []).append(_acceptable(case["labels"]))
    key, values = min(slices.items(), key=lambda pair: (sum(pair[1]) / len(pair[1]), pair[0]))
    return {"slice": key, "acceptable": sum(values), "total": len(values)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Score labeled translation bridge evaluation cases.")
    parser.add_argument("dataset", nargs="?", default=str(Path(__file__).with_name("translation-bridge.yaml")))
    args = parser.parse_args()
    try:
        result = score_dataset(load_dataset(Path(args.dataset)))
    except EvalSchemaError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
