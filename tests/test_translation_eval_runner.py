from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


RUNNER_PATH = Path(__file__).parents[1] / "evals" / "run-translation-bridge.py"
SPEC = importlib.util.spec_from_file_location("translation_eval_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def labeled_case(case_id, accepted=True, direction=("en", "zh"), category="technology"):
    return {
        "id": case_id,
        "source_fixture": "tests/fixtures/feeds/buzzing.xml",
        "category": category,
        "source_language": direction[0],
        "target_language": direction[1],
        "source_text": "Sanitized source text",
        "labels": {
            "expected_target_language": accepted,
            "meaning_preserved": accepted,
            "proper_names_preserved": accepted,
            "headline_clear": accepted,
            "critical_terminology_violation": not accepted,
        },
    }


def test_repository_eval_is_real_but_unlabeled_and_makes_no_quality_claim():
    dataset = runner.load_dataset(Path(__file__).parents[1] / "evals" / "translation-bridge.yaml")
    result = runner.score_dataset(dataset)
    assert result["total_cases"] == 5
    assert result["labeled_cases"] == 0
    assert result["quality_percentage"] is None
    assert result["wilson_95"] is None
    assert result["calibration_status"] == "not_claimed"
    for case in dataset["cases"]:
        fixture = Path(__file__).parents[1] / case["source_fixture"]
        assert case["source_text"] in fixture.read_text(encoding="utf-8")


def test_runner_scores_at_least_100_pre_labeled_cases_with_wilson_interval(tmp_path):
    cases = [labeled_case(f"case-{index}", accepted=index < 90) for index in range(100)]
    path = tmp_path / "labeled.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "dataset_name": "human-labeled-provider-run",
                "minimum_reportable_cases": 100,
                "calibration_claim": "prohibited",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    result = runner.score_dataset(runner.load_dataset(path))
    assert result["quality_percentage"] == 90.0
    assert result["wilson_95"]["low"] < 0.9 < result["wilson_95"]["high"]
    assert result["calibration_status"] == "not_claimed"


def test_runner_hides_percentage_for_small_smoke_set():
    dataset = {
        "minimum_reportable_cases": 100,
        "dataset_name": "smoke",
        "cases": [labeled_case("one")],
    }
    result = runner.score_dataset(dataset)
    assert result["status"] == "smoke_only_no_percentage"
    assert result["quality_percentage"] is None
    assert result["wilson_95"] is None


def test_wilson_interval_known_boundary():
    low, high = runner.wilson_interval(90, 100)
    assert round(low, 3) == 0.826
    assert round(high, 3) == 0.945
