"""The owner CLI's private-output cap is programmable, and failures name a class.

Red before the fix, both observed live on 2026-09-21:
  1. `MAX_OUTPUT_BYTES` was a hardcoded 64 KiB. A real Phase 2 rank response is
     77,745 bytes, so `rank` succeeded remotely and then failed locally, and the
     only way to raise the ceiling was to edit source.
  2. Every failure printed one identical line, so that 78 KB success was
     indistinguishable from a denied token or a dead network.
"""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_cli_cap", ROOT / "scripts/m2_cli.py")
assert SPEC and SPEC.loader
m2_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m2_cli)


class Session:
    access_token = "test-access-token"


def config():
    return type("Config", (), {"supabase_url": "https://project.example", "publishable_key": "public"})()


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        return self.responses.pop(0)


def owner(monkeypatch, payload):
    monkeypatch.setattr(m2_cli, "_auth_config", config)
    monkeypatch.setattr(
        m2_cli,
        "_session",
        lambda value, email, *, minimum_validity: Session(),
    )
    monkeypatch.setattr(m2_cli, "JsonRestTransport", lambda: Transport([(200, payload)]))


def big_payload(byte_target: int) -> dict:
    return {"rows": ["x" * 64 for _ in range(byte_target // 68)]}


def test_default_cap_is_one_mebibyte_so_a_real_phase_two_rank_response_fits():
    assert m2_cli.max_output_bytes(None, {}) == 1024 * 1024
    assert m2_cli.DEFAULT_MAX_OUTPUT_BYTES > 77_745


@pytest.mark.parametrize("value", [65536, 100_000, 16 * 1024 * 1024])
def test_in_range_values_are_accepted_from_flag_and_environment(value):
    assert m2_cli.max_output_bytes(value, {}) == value
    assert m2_cli.max_output_bytes(None, {m2_cli.MAX_OUTPUT_BYTES_ENV: str(value)}) == value


@pytest.mark.parametrize("value", [0, 1, 65535, 16 * 1024 * 1024 + 1, -1])
def test_out_of_range_flag_values_refuse_the_command(value):
    with pytest.raises(ValueError, match="max output bytes"):
        m2_cli.max_output_bytes(value, {})


@pytest.mark.parametrize("value", [0, 1, 65535, 16 * 1024 * 1024 + 1])
def test_out_of_range_environment_values_refuse_the_command(value):
    with pytest.raises(ValueError, match="max output bytes"):
        m2_cli.max_output_bytes(None, {m2_cli.MAX_OUTPUT_BYTES_ENV: str(value)})


@pytest.mark.parametrize("raw", ["abc", "1e6", " 100000", "100000.0", "0x10000", "-1"])
def test_a_non_numeric_environment_value_is_refused_rather_than_ignored(raw):
    with pytest.raises(ValueError, match="decimal byte count"):
        m2_cli.max_output_bytes(None, {m2_cli.MAX_OUTPUT_BYTES_ENV: raw})


def test_the_flag_wins_over_the_environment():
    assert m2_cli.max_output_bytes(200_000, {m2_cli.MAX_OUTPUT_BYTES_ENV: "65536"}) == 200_000


def test_a_response_larger_than_the_old_hardcoded_limit_now_writes(tmp_path, monkeypatch, capsys):
    payload = big_payload(80_000)
    assert len(json.dumps(payload, separators=(",", ":")).encode()) > 64 * 1024
    owner(monkeypatch, payload)
    output = tmp_path / "receipt.json"

    assert m2_cli.main(["history", "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == payload
    assert capsys.readouterr().out == "M2 history completed. Private output written.\n"


def test_a_response_over_the_configured_cap_names_the_sizes_and_writes_nothing(tmp_path, monkeypatch, capsys):
    owner(monkeypatch, big_payload(80_000))
    output = tmp_path / "receipt.json"

    assert m2_cli.main(["history", "--output", str(output), "--max-output-bytes", "65536"]) == 1
    error = capsys.readouterr().err
    assert "output too large" in error and "cap 65536 bytes" in error
    assert not output.exists()


def test_the_environment_raises_the_cap_without_a_flag(tmp_path, monkeypatch):
    owner(monkeypatch, big_payload(80_000))
    monkeypatch.setenv(m2_cli.MAX_OUTPUT_BYTES_ENV, "150000")
    output = tmp_path / "receipt.json"
    assert m2_cli.main(["history", "--output", str(output)]) == 0


def test_an_invalid_cap_is_refused_before_any_remote_request(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(m2_cli, "_auth_config", lambda: called.append("config") or config())
    monkeypatch.setattr(
        m2_cli,
        "_session",
        lambda value, email, *, minimum_validity: called.append("session") or Session(),
    )

    assert m2_cli.main(["history", "--output", str(tmp_path / "r.json"), "--max-output-bytes", "10"]) == 1
    assert called == []
    assert "invalid input" in capsys.readouterr().err


@pytest.mark.parametrize("error,expected", [
    (m2_cli.OutputTooLarge("output too large (9 bytes > cap 8 bytes)"), "output too large (9 bytes > cap 8 bytes)"),
    (m2_cli.AuthError("denied"), "auth or service unavailable"),
    (socket.timeout("timed out"), "network"),
    (ConnectionResetError("reset"), "network"),
    (ValueError("input file is invalid"), "invalid input"),
    (PermissionError("denied"), "local file or environment error"),
])
def test_each_failure_class_gets_its_own_label(error, expected):
    assert m2_cli._failure_class(error) == expected


def test_an_auth_failure_reads_differently_from_an_oversized_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m2_cli, "_auth_config", config)

    def deny(value, email, *, minimum_validity):
        raise m2_cli.AuthError("denied")

    monkeypatch.setattr(m2_cli, "_session", deny)
    assert m2_cli.main(["history", "--output", str(tmp_path / "a.json")]) == 1
    auth_line = capsys.readouterr().err

    owner(monkeypatch, big_payload(80_000))
    assert m2_cli.main(["history", "--output", str(tmp_path / "b.json"), "--max-output-bytes", "65536"]) == 1
    size_line = capsys.readouterr().err
    assert auth_line != size_line
    assert "auth" in auth_line and "output too large" in size_line


def test_no_failure_line_prints_the_private_payload_or_a_token(tmp_path, monkeypatch, capsys):
    owner(monkeypatch, {"secret_story": "x" * 80_000})
    assert m2_cli.main(["history", "--output", str(tmp_path / "r.json"), "--max-output-bytes", "65536"]) == 1
    captured = capsys.readouterr()
    assert "secret_story" not in captured.out + captured.err
    assert "test-access-token" not in captured.out + captured.err
