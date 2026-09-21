"""The ranker image context must contain every file the ranker policy needs.

Red before the fix: `scripts/prepare_ranker_image_context.py` copied a HARDCODED
pair (`config/ranker-policy-r1.yaml`, `config/rankllm-news-curator-json.yaml`),
so when the M2.1 Phase 2 policy started naming
`config/rankllm-news-curator-predictions.yaml` (prompt_template) and
`config/ranking-policy-r2.yaml` (composition_policy), neither file reached the
image. The container died at boot with
`FileNotFoundError: config/ranking-policy-r2.yaml` inside
`load_composition_policy`. Nothing in CI compared the two lists.

Three further holes, from the Codex review of PR #51, are covered here too:
smoke mode naming a config file of its own, a path-shaped policy value being
skipped instead of refused, and the retention cross-check silently not running
in the image because `sources.yaml` is not there.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from curator.recommendation.composition import (
    RETENTION_INPUTS_FILE, CompositionPolicyError, boot_retention_days)
from curator.recommendation.modal_handlers import image_inputs
from curator.recommendation.runtime import build_application, load_ranker_policy, policy_reference
from scripts.prepare_ranker_image_context import (
    RETENTION_INPUTS, referenced_config_files, stage_config, stage_retention_inputs,
    validate_containerfile_sources)

REPO = Path(__file__).resolve().parents[1]
POLICY = Path("config/ranker-policy-r1.yaml")


def _boot_env(**overrides):
    env = {
        "NEWS_CURATOR_READER_ORIGIN": "https://reader.example",
        "NEWS_CURATOR_SUPABASE_URL": "https://project-ref.supabase.co",
        "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
        "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY": "service-role-test",
        "NEWS_CURATOR_CURSOR_SIGNING_KEY": "k" * 32,
        "NEWS_CURATOR_TENANT_ID": "tenant-test",
        "NEWS_CURATOR_MODEL_API_KEY": "provider-key-test",
        "NEWS_CURATOR_PREVIEW_OWNER_IDS": '["00000000-0000-0000-0000-000000000001"]',
    }
    env.update(overrides)
    return env


def _synthetic_repo(root: Path, policy_body: str, **files: str) -> Path:
    (root / "config").mkdir(parents=True)
    (root / POLICY).write_text(policy_body, encoding="utf-8")
    for name, body in files.items():
        (root / "config" / name.replace("__", ".")).write_text(body, encoding="utf-8")
    (root / "sources.yaml").write_text("coverage:\n  observations_retention_days: 14\n",
                                       encoding="utf-8")
    return root


def _staged(tmp_path: Path) -> Path:
    context = tmp_path / "context"
    context.mkdir()
    stage_config(REPO, context)
    return context


# --- Discovery follows the policy, never a second hardcoded list -------------

def test_discovery_follows_the_policy_instead_of_a_hardcoded_list(tmp_path):
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\nprompt_template: config/template.yaml\n"
        "composition_policy: config/composition.yaml\n",
        template__yaml="a: 1\n", composition__yaml="b: 2\n")
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/template.yaml", "config/composition.yaml"]


def test_a_url_is_not_a_file_reference(tmp_path):
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\nendpoint: https://api.openai.com/v1\n"
        "composition_policy: config/composition.yaml\n", composition__yaml="b: 2\n")
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/composition.yaml"]


def test_prose_inside_a_referenced_file_is_never_read_as_a_path(tmp_path):
    """Only the policy is walked, so a template that writes "A/B" is safe.

    The referenced files hold model prose and numbers. Scanning them would make
    an ordinary slash in a prompt fail a deploy, which is a worse failure than
    the one this module exists to prevent.
    """
    repo = _synthetic_repo(
        tmp_path / "repo", "schema_version: 1\nprompt_template: config/template.yaml\n",
        template__yaml='system_message: "Rank A/B and say why"\n')
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/template.yaml"]


def test_a_config_path_written_in_a_comment_is_not_a_reference(tmp_path):
    # config/ranking-policy-r2.yaml names revision 1 in its header prose.
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\n# rollback: config/ranker-policy-r0.yaml\n"
        "composition_policy: config/composition.yaml\n", composition__yaml="b: 2\n")
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/composition.yaml"]


# --- An escaping reference is REFUSED, never skipped (Codex finding 2) -------

@pytest.mark.parametrize("value, reason", [
    ("/etc/passwd", "repository-relative"),
    ("config/../../etc/passwd", "repository-relative"),
    ("secrets/keys.yaml", "must live inside config/"),
    ("config/absent.yaml", "is missing"),
])
def test_an_escaping_reference_fails_the_build(tmp_path, value, reason):
    repo = _synthetic_repo(tmp_path / "repo",
                           f"schema_version: 1\ncomposition_policy: {value}\n")
    with pytest.raises(SystemExit) as error:
        referenced_config_files(repo, POLICY)
    assert reason in str(error.value)
    # The key that carried the bad value is named, so the fix is obvious.
    assert "composition_policy" in str(error.value) and value in str(error.value)


def test_a_symlinked_parent_under_config_is_refused(tmp_path):
    """A final-path symlink check alone lets a symlinked DIRECTORY escape."""
    repo = _synthetic_repo(tmp_path / "repo",
                           "schema_version: 1\ncomposition_policy: config/linked/policy.yaml\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "policy.yaml").write_text("b: 2\n", encoding="utf-8")
    (repo / "config/linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match="passes through a symlink"):
        referenced_config_files(repo, POLICY)


def test_a_symlinked_file_is_refused(tmp_path):
    repo = _synthetic_repo(tmp_path / "repo",
                           "schema_version: 1\ncomposition_policy: config/policy.yaml\n")
    (tmp_path / "elsewhere.yaml").write_text("b: 2\n", encoding="utf-8")
    (repo / "config/policy.yaml").symlink_to(tmp_path / "elsewhere.yaml")
    with pytest.raises(SystemExit, match="passes through a symlink"):
        referenced_config_files(repo, POLICY)


# --- The staged context, against the real repository ------------------------

def test_the_staged_context_holds_every_file_the_real_policy_names(tmp_path):
    context = _staged(tmp_path)
    staged = {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}
    expected = {path.as_posix() for path in referenced_config_files(REPO, POLICY)}
    assert staged == expected | {RETENTION_INPUTS.as_posix()}
    # Named explicitly so the Phase 2 regression cannot come back quietly.
    assert {"config/ranker-policy-r1.yaml", "config/ranking-policy-r2.yaml",
            "config/rankllm-news-curator-predictions.yaml"} <= staged


def test_the_containerfile_copies_the_whole_staged_config_directory(tmp_path):
    context = _staged(tmp_path)
    containerfile = (REPO / "deploy/ranker/Containerfile").read_text()
    assert "COPY config/ /opt/news-curator/config/" in containerfile
    (context / "Containerfile").write_text(containerfile)
    for line in containerfile.splitlines():
        if not line.startswith("COPY ") or line.startswith("COPY config/"):
            continue
        declared = line.split()[1]  # the other COPY sources are staged by main(); stub them
        if declared.endswith("/"):
            (context / declared).mkdir(parents=True, exist_ok=True)
        else:
            (context / declared).write_text("")
    validate_containerfile_sources(context)


def test_the_runtime_boots_its_policy_loading_against_the_staged_context(tmp_path, monkeypatch):
    """The real crash, asserted where CI can see it.

    `build_application` loads the policy, the composition policy (with the
    retention input), then the prompt template path, then the reviewed tokenizer
    cache. That cache is a real reviewed artifact that is not in the repo, so
    reaching its error IS the marker that every file the boot opens was present.
    With the old hardcoded staging this raised FileNotFoundError instead.
    """
    context = _staged(tmp_path)
    monkeypatch.chdir(context)
    with pytest.raises(ValueError) as error:
        build_application(environ=_boot_env(), policy_path=POLICY.as_posix())
    assert "TIKTOKEN_CACHE_DIR" in str(error.value)


@pytest.mark.parametrize("removed", ["config/ranking-policy-r2.yaml", RETENTION_INPUTS.as_posix()])
def test_the_boot_refuses_when_a_needed_file_is_absent(tmp_path, monkeypatch, removed):
    """Red proof: the staged context minus one file is the outage."""
    context = _staged(tmp_path)
    (context / removed).unlink()
    monkeypatch.chdir(context)
    with pytest.raises((FileNotFoundError, CompositionPolicyError)):
        build_application(environ=_boot_env(), policy_path=POLICY.as_posix())


def test_every_config_path_the_policy_names_resolves_inside_the_context(tmp_path):
    """Covers the lazily read ones too.

    `ReviewedRankLLMPromptBuilder` opens prompt_template at the FIRST ranking
    call, not at boot, so a missing template would have survived the boot test
    above and failed in front of a reader instead.
    """
    context = _staged(tmp_path)
    path, policy = load_ranker_policy({}, context / POLICY)
    named = [policy.get("prompt_template"), policy.get("composition_policy")]
    assert all(named)
    for value in named:
        assert policy_reference(path, value).is_file(), f"{value} is missing from the context"


# --- Smoke mode uses the policy's template too (Codex finding 1) -------------

def test_smoke_mode_reads_its_prompt_template_from_the_policy(tmp_path):
    context = _staged(tmp_path)
    policy, prompt_template = image_inputs(context)
    assert prompt_template == context / policy["prompt_template"]
    assert prompt_template.is_file()
    assert prompt_template.name == "rankllm-news-curator-predictions.yaml"


def test_no_handler_names_a_config_file_of_its_own():
    """Every config path in the handlers must come from the policy, not a literal.

    Comments and docstrings may still name the old file to explain the bug;
    executable string constants may not.
    """
    tree = ast.parse((REPO / "curator/recommendation/modal_handlers.py").read_text())
    documented = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node) is not None:
                documented.add(id(node.body[0].value))
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in documented and "config/" in node.value]
    assert literals == [], f"handlers must discover config paths, not name them: {literals}"


# --- Retention is a boot contract in the image too (Codex finding 3) ---------

def test_the_generated_retention_input_carries_the_value_from_sources_yaml(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    days = stage_retention_inputs(REPO, context)
    staged = yaml.safe_load((context / RETENTION_INPUTS).read_text(encoding="utf-8"))
    corpus = yaml.safe_load((REPO / "sources.yaml").read_text(encoding="utf-8"))
    assert days == corpus["coverage"]["observations_retention_days"]
    assert staged["coverage"]["observations_retention_days"] == days


def test_the_script_and_the_runtime_agree_on_the_generated_filename():
    assert RETENTION_INPUTS.as_posix() == RETENTION_INPUTS_FILE


def test_the_retention_check_reads_the_local_corpus_file_first(tmp_path):
    local = tmp_path / "sources.yaml"
    local.write_text("coverage:\n  observations_retention_days: 21\n", encoding="utf-8")
    staged = tmp_path / RETENTION_INPUTS
    staged.parent.mkdir(parents=True)
    staged.write_text("coverage:\n  observations_retention_days: 3\n", encoding="utf-8")
    assert boot_retention_days(local, staged) == 21
    assert boot_retention_days(tmp_path / "absent.yaml", staged) == 3


def test_a_missing_retention_input_refuses_to_boot_instead_of_skipping_the_check(tmp_path):
    with pytest.raises(CompositionPolicyError, match="retention inputs are missing"):
        boot_retention_days(tmp_path / "sources.yaml", tmp_path / RETENTION_INPUTS)


def test_a_retention_file_without_the_key_refuses_to_boot(tmp_path):
    present = tmp_path / "sources.yaml"
    present.write_text("coverage:\n  something_else: 1\n", encoding="utf-8")
    with pytest.raises(CompositionPolicyError, match="observations_retention_days"):
        boot_retention_days(present, tmp_path / RETENTION_INPUTS)


def test_the_local_development_boot_still_uses_sources_yaml(monkeypatch):
    """A checkout has sources.yaml and no generated file; it must behave as before."""
    monkeypatch.chdir(REPO)
    assert not (REPO / RETENTION_INPUTS).exists()
    with pytest.raises(ValueError) as error:
        build_application(environ=_boot_env(), policy_path=POLICY.as_posix())
    assert "TIKTOKEN_CACHE_DIR" in str(error.value)
