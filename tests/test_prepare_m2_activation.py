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
