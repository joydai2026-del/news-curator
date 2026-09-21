"""Protocol tests for the local-only M2 activation helper."""
from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import urllib.error
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_m2_activation", ROOT / "scripts/prepare_m2_activation.py")
activation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(activation)


def binding(path: Path, ref: str) -> None:
    path.write_text(json.dumps({"project_ref": ref, "owners": [{"owner_id": "00000000-0000-0000-0000-000000000001"}]}))
    path.chmod(0o600)


def secret_args(tmp_path: Path, binding_path: Path, output: Path) -> Namespace:
    model_env = tmp_path / "model.env"
    model_env.write_text("NEWS_CURATOR_MODEL_API_KEY=protocol-model-value\n")
    model_env.chmod(0o600)
    return Namespace(
        binding=binding_path,
        expected_project_ref="odurwknvigshekaprjvj",
        management_token_helper=tmp_path / "token.py",
        model_env=model_env,
        reader_origin="https://reader.example.test",
        template="/opt/news-curator/config/rankllm-news-curator-json.yaml",
        service_key_name="news_curator_github",
        output=output,
    )


def test_wrong_project_ref_rejects_before_management_token_read(tmp_path, monkeypatch):
    private = tmp_path / "binding.json"
    binding(private, "aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setattr(activation, "token", lambda _: pytest.fail("token must not be read"))
    args = Namespace(binding=private, expected_project_ref="bbbbbbbbbbbbbbbbbbbb", management_token_helper=tmp_path / "token.py", output=tmp_path / "receipt.json")
    with pytest.raises(ValueError, match="project reference"):
        activation.baseline(args)
    assert not args.output.exists()


def test_redirect_response_cannot_trigger_a_second_authorized_request(monkeypatch):
    captured, handlers = [], []

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

    def build(*values):
        handlers.extend(values)
        return Opener()

    monkeypatch.setattr(activation.urllib.request, "build_opener", build)
    with pytest.raises(urllib.error.HTTPError) as error:
        activation.request_json("odurwknvigshekaprjvj", "protocol-management-token", "/api-keys")
    assert error.value.code == 302
    assert len(captured) == 1
    assert captured[0].full_url == "https://api.supabase.com/v1/projects/odurwknvigshekaprjvj/api-keys"
    assert captured[0].get_header("Authorization") == "Bearer protocol-management-token"
    assert handlers == [activation._NoRedirect]


def test_secure_write_is_mode_0600_and_refuses_existing_output(tmp_path):
    output = tmp_path / "private.json"
    activation.secure_write(output, {"protocol": True})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        activation.secure_write(output, {"protocol": False})
    assert json.loads(output.read_text()) == {"protocol": True}


def test_group_readable_private_input_is_rejected(tmp_path):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    private.chmod(0o640)
    with pytest.raises(ValueError, match="private input"):
        activation.binding(private, "odurwknvigshekaprjvj")


def test_private_symlink_is_rejected(tmp_path):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    link = tmp_path / "binding-link.json"
    link.symlink_to(private)
    with pytest.raises(OSError):
        activation.binding(link, "odurwknvigshekaprjvj")


def test_keychain_failure_is_sanitized_at_cli_boundary(tmp_path, monkeypatch, capsys):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    monkeypatch.setattr(activation, "token", lambda _: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["security", "find-generic-password", "secret-value"])))
    monkeypatch.setattr(sys, "argv", ["prepare_m2_activation.py", "--binding", str(private), "--expected-project-ref", "odurwknvigshekaprjvj", "--management-token-helper", str(tmp_path / "helper.py"), "baseline", "--output", str(tmp_path / "receipt.json")])
    assert activation.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "activation preparation failed: CalledProcessError\n"


def test_stage_secret_uses_one_scoped_key_response_and_writes_private_output(tmp_path, monkeypatch, capsys):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    args = secret_args(tmp_path, private, tmp_path / "secret.json")
    calls = []
    monkeypatch.setattr(activation, "token", lambda _: "protocol-management-token")

    def controlled_request(ref, access, path, *, payload=None):
        calls.append((ref, access, path, payload))
        return [
            {"type": "publishable", "name": "default", "api_key": "sb_publishable_protocol"},
            {"type": "secret", "name": "news_curator_github", "api_key": "sb_secret_protocol"},
        ]

    monkeypatch.setattr(activation, "request_json", controlled_request)
    activation.stage_secret(args)
    receipt = capsys.readouterr()
    secret = json.loads(args.output.read_text())
    assert calls == [("odurwknvigshekaprjvj", "protocol-management-token", "/api-keys?reveal=true", None)]
    assert stat.S_IMODE(args.output.stat().st_mode) == 0o600
    assert secret["NEWS_CURATOR_PREVIEW_OWNER_IDS"] == '["00000000-0000-0000-0000-000000000001"]'
    assert secret["NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY"] == "sb_publishable_protocol"
    assert secret["NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY"] == "sb_secret_protocol"
    assert "protocol-model-value" not in receipt.out
    assert "sb_publishable_protocol" not in receipt.out
    assert "sb_secret_protocol" not in receipt.out


@pytest.mark.parametrize("rows", [
    [{"type": "publishable", "name": "default", "api_key": "sb_publishable_...censored"},
     {"type": "secret", "name": "news_curator_github", "api_key": "sb_secret_...censored"}],
    [{"type": "publishable", "name": "default", "api_key": "sb_secret_wrong_type"},
     {"type": "secret", "name": "news_curator_github", "api_key": "sb_publishable_wrong_type"}],
])
def test_stage_secret_rejects_redacted_or_wrongly_typed_keys(tmp_path, monkeypatch, rows):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    args = secret_args(tmp_path, private, tmp_path / "secret.json")
    monkeypatch.setattr(activation, "token", lambda _: "protocol-management-token")
    monkeypatch.setattr(activation, "request_json", lambda *_args, **_kwargs: rows)
    with pytest.raises(ValueError, match="scoped API keys"):
        activation.stage_secret(args)
    assert not args.output.exists()


@pytest.mark.parametrize("owners", [[], [{"owner_id": "00000000-0000-0000-0000-000000000001"}, {"owner_id": "00000000-0000-0000-0000-000000000002"}]])
def test_stage_secret_rejects_non_single_owner_before_token_read(tmp_path, monkeypatch, owners):
    private = tmp_path / "binding.json"
    private.write_text(json.dumps({"project_ref": "odurwknvigshekaprjvj", "owners": owners}))
    private.chmod(0o600)
    args = secret_args(tmp_path, private, tmp_path / "secret.json")
    monkeypatch.setattr(activation, "token", lambda _: pytest.fail("token must not be read"))
    with pytest.raises(ValueError, match="exactly one owner"):
        activation.stage_secret(args)
    assert not args.output.exists()


@pytest.mark.parametrize("rows", [
    [{"type": "publishable", "name": "default", "api_key": "one"}],
    [{"type": "publishable", "name": "default", "api_key": "one"}, {"type": "secret", "name": "news_curator_github", "api_key": "two"}, {"type": "secret", "name": "news_curator_github", "api_key": "three"}],
])
def test_stage_secret_rejects_missing_or_ambiguous_scoped_keys(tmp_path, monkeypatch, rows):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    args = secret_args(tmp_path, private, tmp_path / "secret.json")
    monkeypatch.setattr(activation, "token", lambda _: "protocol-management-token")
    monkeypatch.setattr(activation, "request_json", lambda *_args, **_kwargs: rows)
    with pytest.raises(ValueError, match="scoped API keys"):
        activation.stage_secret(args)
    assert not args.output.exists()


def test_cli_help_runs_from_an_arbitrary_current_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_m2_activation.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Prepare bounded M2 deployment artifacts" in result.stdout
    assert result.stderr == ""


def test_the_template_field_is_optional_and_absent_from_the_secret_when_omitted(tmp_path, monkeypatch):
    """The prompt template is the ranker policy's job in production. Listing it as
    REQUIRED here is what wrote the Phase 1 path into the live secret."""
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    output = tmp_path / "secret.json"
    args = secret_args(tmp_path, private, output)
    args.template = None
    monkeypatch.setattr(activation, "token", lambda _: "protocol-management-token")
    monkeypatch.setattr(activation, "api_keys", lambda *values: ("sb_publishable_x", "sb_secret_y"))

    activation.stage_secret(args)
    secret = json.loads(output.read_text())
    assert "NEWS_CURATOR_RANKLLM_TEMPLATE" not in secret
    assert set(secret) == set(activation.REQUIRED_SECRET_FIELDS)


def test_a_template_that_is_not_in_this_checkout_is_refused(tmp_path, monkeypatch):
    private = tmp_path / "binding.json"
    binding(private, "odurwknvigshekaprjvj")
    args = secret_args(tmp_path, private, tmp_path / "secret.json")
    args.template = "/opt/news-curator/config/absent-template.yaml"
    monkeypatch.setattr(activation, "token", lambda _: "protocol-management-token")
    monkeypatch.setattr(activation, "api_keys", lambda *values: ("sb_publishable_x", "sb_secret_y"))

    with pytest.raises(ValueError, match="not present in this checkout"):
        activation.stage_secret(args)
    assert not (tmp_path / "secret.json").exists()


@pytest.mark.parametrize("value", [
    "config/rankllm-news-curator-json.yaml",
    "/opt/news-curator/curator/recommendation/runtime.py",
    "/opt/news-curator/config/../curator/runtime.py",
    "/etc/passwd",
])
def test_only_a_staged_config_path_is_accepted(value):
    with pytest.raises(ValueError):
        activation.image_template_repo_path(value)


def test_a_template_that_exists_resolves_to_its_checkout_file():
    assert activation.image_template_repo_path(
        "/opt/news-curator/config/rankllm-news-curator-json.yaml"
    ) == ROOT / "config/rankllm-news-curator-json.yaml"
