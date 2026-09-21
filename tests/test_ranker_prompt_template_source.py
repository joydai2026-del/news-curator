"""The ranker policy owns the prompt template in production; env is smoke-only.

Red before the fix, observed live on 2026-09-21: the production Modal secret,
written when `scripts/prepare_m2_activation.py` still listed
`NEWS_CURATOR_RANKLLM_TEMPLATE` as REQUIRED, carried the Phase 1 value
`config/rankllm-news-curator-json.yaml`. `runtime.build_application` let that
env value win over the policy unconditionally, and the image no longer stages
that file, so every POST /rank answered with `provider_preparation_failed`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from curator.recommendation.runtime import (PROMPT_TEMPLATE_ENV, PROMPT_TEMPLATE_OVERRIDE_ENV,
                                            RANKER_POLICY_DEFAULT, load_ranker_policy,
                                            resolve_prompt_template)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    """A policy file laid out like the real tree: <root>/config/<name>."""
    (tmp_path / "config").mkdir()
    for name in ("policy.yaml", "policy-template.yaml", "env-template.yaml"):
        (tmp_path / "config" / name).write_text("schema_version: 1\n")
    return tmp_path / "config" / "policy.yaml"


POLICY = {"prompt_template": "config/policy-template.yaml"}


def test_the_policy_value_is_used_when_no_override_is_set(policy_path, capsys):
    path, source = resolve_prompt_template({}, POLICY, policy_path)
    assert path == policy_path.parent / "policy-template.yaml"
    assert source == "policy"
    assert capsys.readouterr().out == ""


def test_service_mode_ignores_the_environment_override_and_says_so_once(policy_path, capsys):
    env = {PROMPT_TEMPLATE_ENV: "config/env-template.yaml", "NEWS_CURATOR_MODAL_MODE": "service"}
    path, source = resolve_prompt_template(env, POLICY, policy_path)

    assert source == "policy"
    assert path.name == "policy-template.yaml"
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "prompt_template_override_ignored"
    assert record["ignored_value"] == "config/env-template.yaml"
    assert record["using_policy_value"] == "config/policy-template.yaml"


def test_an_unset_modal_mode_is_service_and_still_ignores_the_override(policy_path):
    env = {PROMPT_TEMPLATE_ENV: "config/env-template.yaml"}
    assert resolve_prompt_template(env, POLICY, policy_path)[1] == "policy"


def test_smoke_mode_honours_the_override(policy_path, capsys):
    env = {PROMPT_TEMPLATE_ENV: "config/env-template.yaml", "NEWS_CURATOR_MODAL_MODE": "smoke"}
    path, source = resolve_prompt_template(env, POLICY, policy_path)

    assert source == "env"
    assert path == policy_path.parent / "env-template.yaml"
    assert capsys.readouterr().out == ""


def test_an_explicit_allow_flag_honours_the_override_in_service_mode(policy_path):
    env = {PROMPT_TEMPLATE_ENV: "config/env-template.yaml", "NEWS_CURATOR_MODAL_MODE": "service",
           PROMPT_TEMPLATE_OVERRIDE_ENV: "true"}
    assert resolve_prompt_template(env, POLICY, policy_path)[1] == "env"


@pytest.mark.parametrize("value", ["false", "TRUE", "1", "yes", ""])
def test_only_the_exact_true_string_opens_the_override(policy_path, value):
    env = {PROMPT_TEMPLATE_ENV: "config/env-template.yaml", PROMPT_TEMPLATE_OVERRIDE_ENV: value}
    assert resolve_prompt_template(env, POLICY, policy_path)[1] == "policy"


def test_a_missing_policy_template_refuses_the_boot_naming_path_and_source(policy_path):
    with pytest.raises(ValueError) as error:
        resolve_prompt_template({}, {"prompt_template": "config/absent.yaml"}, policy_path)
    assert "from policy" in str(error.value)
    assert str(policy_path.parent / "absent.yaml") in str(error.value)


def test_a_missing_override_template_refuses_the_boot_in_smoke_mode(policy_path):
    env = {PROMPT_TEMPLATE_ENV: "config/absent.yaml", "NEWS_CURATOR_MODAL_MODE": "smoke"}
    with pytest.raises(ValueError) as error:
        resolve_prompt_template(env, POLICY, policy_path)
    assert "from env" in str(error.value)
    assert str(policy_path.parent / "absent.yaml") in str(error.value)


def test_the_stale_production_value_no_longer_changes_the_running_template(policy_path, capsys):
    """The exact live failure: the secret's Phase 1 path is ignored, not honoured."""
    env = {PROMPT_TEMPLATE_ENV: "config/rankllm-news-curator-json.yaml"}
    path, source = resolve_prompt_template(env, POLICY, policy_path)
    assert (source, path.name) == ("policy", "policy-template.yaml")
    assert "prompt_template_override_ignored" in capsys.readouterr().out


def test_the_shipped_policy_names_a_template_that_exists_in_this_checkout():
    path, policy = load_ranker_policy({}, RANKER_POLICY_DEFAULT, root=ROOT)
    resolved, source = resolve_prompt_template({}, policy, path)
    assert source == "policy"
    assert resolved == ROOT / "config/rankllm-news-curator-predictions.yaml"
    assert resolved.is_file()
