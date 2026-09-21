"""The ranker image context must contain every config file the policy names.

Red before the fix: `scripts/prepare_ranker_image_context.py` copied a HARDCODED
pair (`config/ranker-policy-r1.yaml`, `config/rankllm-news-curator-json.yaml`),
so when the M2.1 Phase 2 policy started naming `config/ranking-policy-r2.yaml`
(`composition_policy`) and `config/rankllm-news-curator-predictions.yaml`
(`prompt_template`), neither file reached the image. The container then died at
boot with `FileNotFoundError: config/ranking-policy-r2.yaml` inside
`load_composition_policy`, and the missing prompt template would have failed the
first ranking call. Nothing in CI asserted the two lists agreed.

The two tests that matter: the staged context holds exactly what the policy
names, and the runtime's own boot path gets through its file loading with that
directory as the working directory.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.prepare_ranker_image_context import (
    referenced_config_files, stage_referenced_config, validate_containerfile_sources)
from curator.recommendation.runtime import build_application

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
        (root / "config" / name).write_text(body, encoding="utf-8")
    return root


def test_discovery_follows_the_policy_instead_of_a_hardcoded_list(tmp_path):
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\nprompt_template: config/template.yaml\n"
        "composition_policy: config/composition.yaml\n",
        **{"template.yaml": "a: 1\n", "composition.yaml": "b: 2\n"})
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/template.yaml", "config/composition.yaml"]


def test_discovery_follows_a_reference_made_by_a_referenced_file(tmp_path):
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\ncomposition_policy: config/composition.yaml\n",
        **{"composition.yaml": "labels: config/labels.yaml\n", "labels.yaml": "c: 3\n"})
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/composition.yaml", "config/labels.yaml"]


def test_a_config_path_written_in_a_comment_is_not_a_reference(tmp_path):
    # config/ranking-policy-r2.yaml names revision 1 in its header prose. Comments
    # are documentation, not a file the runtime opens, so they stay out of the image.
    repo = _synthetic_repo(
        tmp_path / "repo",
        "schema_version: 1\n# rollback: config/ranker-policy-r0.yaml\n"
        "composition_policy: config/composition.yaml\n",
        **{"composition.yaml": "b: 2\n"})
    assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
        "config/ranker-policy-r1.yaml", "config/composition.yaml"]


def test_a_referenced_file_that_does_not_exist_fails_the_build(tmp_path):
    repo = _synthetic_repo(tmp_path / "repo",
                           "schema_version: 1\ncomposition_policy: config/absent.yaml\n")
    with pytest.raises(SystemExit, match="references a missing file: config/absent.yaml"):
        referenced_config_files(repo, POLICY)


@pytest.mark.parametrize("reference", ["config/../secrets.yaml", "/etc/passwd"])
def test_an_unsafe_reference_is_refused(tmp_path, reference):
    repo = _synthetic_repo(tmp_path / "repo",
                           f"schema_version: 1\ncomposition_policy: {reference}\n")
    if reference.startswith("config/"):
        with pytest.raises(SystemExit, match="unsafe config reference"):
            referenced_config_files(repo, POLICY)
    else:  # an absolute path is not under config/, so it is never followed at all
        assert [path.as_posix() for path in referenced_config_files(repo, POLICY)] == [
            "config/ranker-policy-r1.yaml"]


def _staged(tmp_path: Path) -> Path:
    context = tmp_path / "context"
    context.mkdir()
    stage_referenced_config(REPO, context)
    return context


def test_the_staged_context_holds_every_file_the_real_policy_names(tmp_path):
    context = _staged(tmp_path)
    staged = {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}
    expected = {path.as_posix() for path in referenced_config_files(REPO, POLICY)}
    assert staged == expected
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

    `build_application` loads the policy, then the composition policy, then the
    prompt template path, then the reviewed tokenizer cache. The tokenizer cache
    is a real 4 MB reviewed artifact that is not in the repo, so reaching its
    error IS the marker that every file the boot opens was present. With the old
    hardcoded staging this raised FileNotFoundError on config/ranking-policy-r2.yaml.
    """
    context = _staged(tmp_path)
    monkeypatch.chdir(context)
    with pytest.raises(ValueError) as error:
        build_application(environ=_boot_env(), policy_path=POLICY.as_posix())
    assert "TIKTOKEN_CACHE_DIR" in str(error.value)


def test_the_boot_fails_loudly_when_a_referenced_file_is_absent(tmp_path, monkeypatch):
    """Red proof: the staged context minus one referenced file is the outage."""
    context = _staged(tmp_path)
    (context / "config/ranking-policy-r2.yaml").unlink()
    monkeypatch.chdir(context)
    with pytest.raises(FileNotFoundError):
        build_application(environ=_boot_env(), policy_path=POLICY.as_posix())


def test_every_config_path_the_policy_names_resolves_inside_the_context(tmp_path, monkeypatch):
    """Covers the lazily read ones too.

    `ReviewedRankLLMPromptBuilder` opens prompt_template at the FIRST ranking
    call, not at boot, so a missing template would have survived the boot test
    above and failed in front of a reader instead.
    """
    context = _staged(tmp_path)
    monkeypatch.chdir(context)
    import yaml
    policy = yaml.safe_load(Path(POLICY.as_posix()).read_text(encoding="utf-8"))
    named = [value for value in (policy.get("prompt_template"), policy.get("composition_policy")) if value]
    assert len(named) == 2
    for value in named:
        assert Path(value).is_file(), f"{value} is missing from the image context"
    assert os.getcwd() == str(context.resolve())
