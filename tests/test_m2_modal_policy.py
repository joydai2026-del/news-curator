import importlib
import hashlib
import os
import sys
import types
import ast
from pathlib import Path
import json

from scripts.prepare_ranker_image_context import copy_python_tree, validate_containerfile_sources

import pytest


MODULE = "curator.recommendation.modal_app"
HANDLERS_MODULE = "curator.recommendation.modal_handlers"


class _Resource:
    @classmethod
    def from_registry(cls, value):
        return ("image", value)

    @classmethod
    def from_name(cls, value):
        return ("secret", value)

    @classmethod
    def from_dockerfile(cls, value, **kwargs):
        return cls(value, kwargs)

    def __init__(self, value, kwargs):
        self.value, self.kwargs = value, kwargs

    def env(self, values):
        return ("image-with-env", self.value, values)


def _modal_double(captured):
    class App:
        def __init__(self, name):
            captured["app_name"] = name

        def function(self, **kwargs):
            captured.setdefault("functions", []).append(kwargs)
            return lambda fn: fn

    def concurrent(**kwargs):
        captured["concurrent"] = kwargs
        captured.setdefault("concurrent_calls", []).append(kwargs)
        return lambda fn: fn

    return types.SimpleNamespace(
        App=App,
        Image=_Resource,
        Secret=_Resource,
        concurrent=concurrent,
        asgi_app=lambda: (lambda fn: fn),
        Cron=lambda expression: ("cron", expression),
    )


def _load(monkeypatch, **values):
    captured = {}
    monkeypatch.setitem(sys.modules, "modal", _modal_double(captured))
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    for key in tuple(values):
        monkeypatch.setenv(key, values[key])
    return importlib.import_module(MODULE), captured


def _required(tmp_path, *, preparation_enabled=False):
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n")
    policy = context / "config" / "ranker-policy-r1.yaml"
    policy.parent.mkdir()
    policy.write_text("schema_version: 1\nnext_run_preparation:\n"
                      f"  enabled: {str(preparation_enabled).lower()}\n")
    manifest = context / "context-manifest.json"
    files = [containerfile, policy]
    manifest.write_text(json.dumps({"files": [
        {"path": item.relative_to(context).as_posix(), "size": len(item.read_bytes()),
         "sha256": hashlib.sha256(item.read_bytes()).hexdigest()} for item in files
    ]}) + "\n")
    return {
        "NEWS_CURATOR_MODAL_DEPLOYMENT_ENABLED": "true",
        "NEWS_CURATOR_RANKER_CONTEXT": str(context),
        "NEWS_CURATOR_RANKER_CONTEXT_SHA256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "NEWS_CURATOR_RANKER_SECRET_NAME": "ranker-secret",
    }


def test_modal_deployment_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("NEWS_CURATOR_MODAL_DEPLOYMENT_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="disabled by policy"):
        _load(monkeypatch)


def test_remote_handlers_import_without_deployment_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("NEWS_CURATOR_MODAL_") or name.startswith("NEWS_CURATOR_RANKER_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, HANDLERS_MODULE, raising=False)
    module = importlib.import_module(HANDLERS_MODULE)
    assert callable(module.endpoint)
    assert callable(module.smoke_rankllm_image)


def test_modal_policy_defaults_are_bounded_and_platform_access_is_restricted(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_CURATOR_MODAL_MIN_CONTAINERS", raising=False)
    _, captured = _load(monkeypatch, **_required(tmp_path))
    assert captured["app_name"] == "news-curator-m2-ranker"
    # 240: the container must outlive the service's own worst case (provider
    # deadline + settle window + request calls and state retries = 225s),
    # which curator.recommendation.runtime refuses to boot without.
    assert captured["functions"][0]["timeout"] == 240
    assert captured["functions"][0]["max_containers"] == 4
    assert captured["functions"][0]["min_containers"] == 0
    assert captured["functions"][0]["scaledown_window"] == 60
    assert captured["functions"][0]["enable_memory_snapshot"] is True
    assert captured["functions"][0]["restrict_modal_access"] is True
    assert captured["concurrent_calls"][0]["max_inputs"] == 8


def test_modal_policy_uses_validated_overrides(monkeypatch, tmp_path):
    values = _required(tmp_path) | {
        "NEWS_CURATOR_MODAL_APP_NAME": "news-curator-preview",
        "NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS": "30",
        "NEWS_CURATOR_MODAL_MAX_CONTAINERS": "7",
        "NEWS_CURATOR_MODAL_MAX_INPUTS_PER_CONTAINER": "12",
        "NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS": "300",
        "NEWS_CURATOR_MODAL_MEMORY_SNAPSHOT_ENABLED": "false",
    }
    _, captured = _load(monkeypatch, **values)
    assert captured["app_name"] == "news-curator-preview"
    assert captured["functions"][0]["timeout"] == 30
    assert captured["functions"][0]["max_containers"] == 7
    assert captured["functions"][0]["scaledown_window"] == 300
    assert captured["functions"][0]["enable_memory_snapshot"] is False
    assert captured["concurrent_calls"][0]["max_inputs"] == 12


@pytest.mark.parametrize("minimum", ["0", "1", "7"])
def test_modal_min_containers_accepts_zero_through_configured_maximum(monkeypatch, tmp_path, minimum):
    _, captured = _load(monkeypatch, **(_required(tmp_path) | {
        "NEWS_CURATOR_MODAL_MIN_CONTAINERS": minimum,
        "NEWS_CURATOR_MODAL_MAX_CONTAINERS": "7",
    }))
    assert captured["functions"][0]["min_containers"] == int(minimum)
    assert captured["functions"][0]["max_containers"] == 7


@pytest.mark.parametrize("minimum", ["-1", "3", "1.5", "many", ""])
def test_modal_min_containers_rejects_invalid_or_above_configured_maximum(monkeypatch, tmp_path, minimum):
    with pytest.raises(ValueError, match="NEWS_CURATOR_MODAL_MIN_CONTAINERS"):
        _load(monkeypatch, **(_required(tmp_path) | {
            "NEWS_CURATOR_MODAL_MIN_CONTAINERS": minimum,
            "NEWS_CURATOR_MODAL_MAX_CONTAINERS": "2",
        }))


def test_smoke_mode_does_not_require_or_resolve_a_secret(monkeypatch, tmp_path):
    values = _required(tmp_path) | {"NEWS_CURATOR_MODAL_MODE": "smoke"}
    values.pop("NEWS_CURATOR_RANKER_SECRET_NAME")
    module, captured = _load(monkeypatch, **values)
    assert hasattr(module, "smoke_rankllm_image")
    assert captured["functions"][0]["scaledown_window"] == 2
    assert captured["functions"][0]["block_network"] is True
    assert captured["functions"][0]["include_source"] is False
    assert "enable_memory_snapshot" not in captured["functions"][0]
    assert "min_containers" not in captured["functions"][0]
    assert all(call[0] != "secret" for call in captured.values() if isinstance(call, tuple))


def test_context_mutation_is_rejected(monkeypatch, tmp_path):
    values = _required(tmp_path)
    (Path(values["NEWS_CURATOR_RANKER_CONTEXT"]) / "Containerfile").write_text("FROM changed\n")
    with pytest.raises(ValueError, match="context file mismatch"):
        _load(monkeypatch, **values)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NEWS_CURATOR_MODAL_DEPLOYMENT_ENABLED", "yes"),
        ("NEWS_CURATOR_MODAL_MEMORY_SNAPSHOT_ENABLED", "yes"),
        ("NEWS_CURATOR_MODAL_APP_NAME", "News Curator"),
        ("NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS", "6"),
        ("NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS", "301"),
        ("NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS", "sixty"),
        ("NEWS_CURATOR_MODAL_MAX_CONTAINERS", "0"),
        ("NEWS_CURATOR_MODAL_MAX_INPUTS_PER_CONTAINER", "33"),
        ("NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS", "0"),
        ("NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS", "1"),
        ("NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS", "-1"),
    ],
)
def test_modal_policy_rejects_invalid_values(monkeypatch, tmp_path, name, value):
    values = _required(tmp_path) | {name: value}
    with pytest.raises((RuntimeError, ValueError)):
        _load(monkeypatch, **values)


def test_vendored_rankllm_subset_has_no_unreviewed_imports_or_symbols():
    vendor = Path(__file__).parents[1] / "deploy" / "ranker" / "vendor"
    forbidden = {
        "SafeOpenai", "SafeGenai", "SafeLiteLLM", "RankListwiseOSLLM",
        "VicunaReranker", "ZephyrReranker",
    }
    allowed_rankllm = {
        "rank_llm.data",
        "rank_llm.rerank.inference_handler",
        "rank_llm.rerank.listwise.listwise_inference_handler",
    }
    seen_symbols = set()
    seen_imports = set()
    for path in vendor.rglob("*.py"):
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        seen_symbols.update(name for name in forbidden if name in source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                seen_imports.update(alias.name for alias in node.names if alias.name.startswith("rank_llm"))
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("rank_llm"):
                seen_imports.add(node.module)
    assert seen_symbols == set()
    assert seen_imports <= allowed_rankllm


def test_vendor_bytes_match_pinned_archive_except_two_declared_shims():
    root = Path(__file__).parents[1]
    vendor = root / "deploy/ranker/vendor"
    manifest = json.loads((root / "deploy/ranker/rankllm-vendor-manifest.json").read_text())
    pinned = {
        "RANKLLM-LICENSE": "e712c10d43239eb2b5493a60d82bd385fb8750a082171a12a33088180c79e8f9",
        "rank_llm/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "rank_llm/data.py": "313fcf6b29c82e8511a7ff1cab2714d050d54ca81df4a2eeca45a271deae932d",
        "rank_llm/rerank/inference_handler.py": "dc93472146044e9df4c207390eff0d7d2468be091ba787963687478a96743c60",
        "rank_llm/rerank/listwise/listwise_inference_handler.py": "f46fe1718346a17ff846e777b7086a621a479b0207363617162933228c1b5a0a",
        "rank_llm/rerank/listwise/multiturn_listwise_inference_handler.py": "6dc14a938d72843e99385114060cce78e795d1d50c5c776c571fb2fd41558a86",
        "rank_llm/rerank/prompt_templates/rank_gpt_template.yaml": "14ac512117ffe91987e708d6c1531203737c3a379d96110c825e8f8dc07297fe",
    }
    entries = {entry["path"]: entry for entry in manifest["files"]}
    assert {path for path, entry in entries.items() if entry["source_status"] == "upstream_exact"} == set(pinned)
    assert {path for path, entry in entries.items() if entry["source_status"] == "owned_packaging_shim"} == {
        "rank_llm/rerank/__init__.py", "rank_llm/rerank/listwise/__init__.py"}
    assert not list(vendor.rglob("*.pyc"))
    assert not list(vendor.rglob("__pycache__"))
    for relative, expected in pinned.items():
        assert hashlib.sha256((vendor / relative).read_bytes()).hexdigest() == expected
        assert entries[relative]["sha256"] == expected


def test_image_source_copy_excludes_secret_canary_and_non_python_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.py").write_text("VALUE = 1\n")
    (source / ".env").write_text("CANARY_SECRET=must-not-copy\n")
    (source / "capture.json").write_text('{"private":true}\n')
    destination = tmp_path / "destination"
    copy_python_tree(source, destination)
    assert [path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()] == ["package.py"]
    assert "CANARY_SECRET" not in (destination / "package.py").read_text()


def test_containerfile_copy_sources_must_all_exist(tmp_path):
    (tmp_path / "Containerfile").write_text("COPY vendor-manifest.json /opt/vendor-manifest.json\n")
    with pytest.raises(SystemExit, match="COPY source missing"):
        validate_containerfile_sources(tmp_path)
    (tmp_path / "vendor-manifest.json").write_text("{}\n")
    validate_containerfile_sources(tmp_path)
