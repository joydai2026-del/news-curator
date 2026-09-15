from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_cli", ROOT / "scripts/m2_cli.py")
assert SPEC and SPEC.loader
m2_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m2_cli)


class Session:
    access_token = "test-access-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append((method, url, headers, body, timeout))
        return self.responses.pop(0)


def config():
    return type("Config", (), {"supabase_url": "https://project.example", "publishable_key": "public"})()


def test_history_writes_owner_only_file_and_no_payload_to_stdout(tmp_path, monkeypatch, capsys):
    transport = Transport([(200, {"history_revision": 3})])
    monkeypatch.setattr(m2_cli, "_auth_config", config)
    monkeypatch.setattr(m2_cli, "_session", lambda value, email: Session())
    monkeypatch.setattr(m2_cli, "JsonRestTransport", lambda: transport)
    output = tmp_path / "receipt.json"

    assert m2_cli.main(["history", "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "M2 history completed. Private output written.\n"
    assert "history_revision" not in captured.out + captured.err
    assert json.loads(output.read_text()) == {"history_revision": 3}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert transport.calls[0][1].endswith("/rest/v1/rpc/m2_history_snapshot")


def test_invalid_output_preflight_makes_no_remote_request(tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_text("existing")
    called = []
    monkeypatch.setattr(m2_cli, "_auth_config", lambda: called.append("config") or config())
    monkeypatch.setattr(m2_cli, "_session", lambda value, email: called.append("session") or Session())

    assert m2_cli.main(["history", "--output", str(output)]) == 1
    assert called == []


def test_rank_reuses_current_history_bindings_and_fixed_origin(tmp_path, monkeypatch):
    transport = Transport([
        (200, {"included_history_revision": 2, "history_revision": 3, "history_generation": 4, "consent_revision": 5}),
        (200, {"result_mode": "fallback", "cards": []}),
    ])
    monkeypatch.setattr(m2_cli, "JsonRestTransport", lambda: transport)
    args = m2_cli._parser().parse_args([
        "rank", "--output", str(tmp_path / "receipt.json"), "--ranker-origin", "https://rank.example",
        "--policy-version", "policy-r1", "--model-version", "model-r1", "--category", "ai", "--query", "test", "--timeout", "7",
    ])

    result = m2_cli._rank(config(), Session(), args)
    assert result["ranking"]["result_mode"] == "fallback"
    request = transport.calls[1]
    assert request[1] == "https://rank.example/rank"
    assert request[3]["history_revision"] == 2
    assert request[3]["server_commit_revision"] == 3
    assert request[3]["history_generation"] == 4
    assert request[3]["consent_revision"] == 5
    assert request[3]["eligibility"] == {"category": "ai", "query": "test"}
    assert all(call[4] == 7 for call in transport.calls)


def test_page_encodes_opaque_cursor_and_uses_get(tmp_path, monkeypatch):
    transport = Transport([(200, {"cards": []})])
    input_path = tmp_path / "page.json"
    input_path.write_text(json.dumps({"cursor": "a+/= cursor"}))
    monkeypatch.setattr(m2_cli, "JsonRestTransport", lambda: transport)
    args = m2_cli._parser().parse_args(["page", "--output", str(tmp_path / "receipt.json"), "--input", str(input_path), "--ranker-origin", "https://rank.example", "--timeout", "6"])

    assert m2_cli._page(config(), Session(), args) == {"cards": []}
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1] == "https://rank.example/page?cursor=a%2B%2F%3D+cursor"
    assert transport.calls[0][4] == 6


def test_export_continuation_uses_exact_structured_rpc_body(tmp_path):
    value = {"p_cursor": "opaque-cursor", "p_expected_fence": "a" * 64}
    source = tmp_path / "export.json"
    source.write_text(json.dumps(value))
    args = m2_cli._parser().parse_args(["export", "--output", str(tmp_path / "receipt.json"), "--input", str(source)])
    assert m2_cli._RPC_BUILDERS["export"][1](args) == value


def test_failures_do_not_create_output_or_emit_private_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m2_cli, "_auth_config", config)
    monkeypatch.setattr(m2_cli, "_session", lambda value, email: (_ for _ in ()).throw(m2_cli.AuthError("secret")))
    output = tmp_path / "receipt.json"

    assert m2_cli.main(["history", "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.out + captured.err
    assert not output.exists()


@pytest.mark.parametrize("value", ["not-json", "[]"])
def test_mutation_input_rejects_invalid_json_without_request(tmp_path, value):
    payload = tmp_path / "input.json"
    payload.write_text(value)
    with pytest.raises(ValueError):
        m2_cli._input_object(str(payload))


def test_private_output_rejects_existing_symlink_and_parent_traversal(tmp_path):
    target = tmp_path / "target"
    target.write_text("existing")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        m2_cli._private_output(str(link), {"ok": True})
    with pytest.raises(ValueError):
        m2_cli._private_output(str(tmp_path / "subdir" / ".." / "receipt.json"), {"ok": True})


def test_bounds_reject_large_or_inconsistent_request_values(tmp_path):
    parser = m2_cli._parser()
    with pytest.raises(ValueError):
        m2_cli._validate_args(parser.parse_args(["history", "--output", str(tmp_path / "r"), "--limit", "101"]))
    with pytest.raises(ValueError):
        m2_cli._validate_args(parser.parse_args(["consent", "--output", str(tmp_path / "r"), "--learning", "false", "--provider-processing", "true"]))
    with pytest.raises(ValueError):
        m2_cli._validate_args(parser.parse_args(["rank", "--output", str(tmp_path / "r"), "--exclude-story-id", "x" * 257]))


def test_fixed_command_allowlist_and_no_redirect_transport():
    assert set(m2_cli._RPC_BUILDERS) == {"history", "consent", "clear-history", "export", "event", "state-event", "interest-event"}
    assert any(type(handler).__name__ == "_NoRedirect" for handler in m2_cli.JsonRestTransport()._opener.handlers)
