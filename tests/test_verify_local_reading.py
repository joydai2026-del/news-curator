from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import verify_local_reading as verify


SAFE_STATUS = """API_URL=\"http://127.0.0.1:54321\"
ANON_KEY=\"fixture-anon\"
SERVICE_ROLE_KEY=\"fixture-service\"
JWT_SECRET=\"fixture-jwt\"
"""


def test_missing_status_binding_fails_before_pytest(monkeypatch, capsys) -> None:
    called = False

    def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("pytest must not run")

    monkeypatch.setattr(verify, "_status_output", lambda: "API_URL=http://127.0.0.1:54321\n")
    monkeypatch.setattr(verify, "_run_pytest", should_not_run)

    assert verify.main([]) == 2
    assert called is False
    output = capsys.readouterr()
    assert "missing required local Supabase bindings" in output.err
    assert "JWT_SECRET" in output.err


@pytest.mark.parametrize(
    "origin",
    [
        "https://project.supabase.co",
        "http://localhost:54321",
        "http://127.0.0.1",
        "http://user@127.0.0.1:54321",
        "http://127.0.0.1:54321/rest/v1",
    ],
)
def test_non_exact_loopback_origin_is_rejected_without_real_credentials(origin: str) -> None:
    status = SAFE_STATUS.replace("http://127.0.0.1:54321", origin)

    with pytest.raises(verify.VerificationError, match="refuses every non-loopback"):
        verify.bindings_from_status(status)


def test_skip_or_empty_matrix_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verify, "_status_output", lambda: SAFE_STATUS)
    monkeypatch.setattr(
        verify,
        "_run_pytest",
        lambda _env: verify.TestSummary(tests=9, failures=0, errors=0, skipped=1, returncode=0),
    )
    assert verify.main([]) == 1
    assert "FAIL: 9 tests; 1 skipped" in capsys.readouterr().err

    monkeypatch.setattr(
        verify,
        "_run_pytest",
        lambda _env: verify.TestSummary(tests=0, failures=0, errors=0, skipped=0, returncode=0),
    )
    assert verify.main([]) == 1
    assert "FAIL: no tests collected" in capsys.readouterr().err


def test_success_prints_only_safe_aggregate_counts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verify, "_status_output", lambda: SAFE_STATUS)
    monkeypatch.setattr(
        verify,
        "_run_pytest",
        lambda _env: verify.TestSummary(tests=9, failures=0, errors=0, skipped=0, returncode=0),
    )

    assert verify.main([]) == 0
    output = capsys.readouterr()
    assert output.out.strip() == "PASS: 9 tests passed; 0 skipped."
    assert "fixture-anon" not in output.out + output.err
    assert "fixture-service" not in output.out + output.err
    assert "fixture-jwt" not in output.out + output.err


def test_pytest_invocation_is_bounded_to_the_two_local_matrices(monkeypatch, tmp_path) -> None:
    observed: list[str] = []
    child_environment: dict[str, str] = {}

    def fake_run(command, **kwargs):
        observed.extend(command)
        child_environment.update(kwargs["env"])
        junit = next(part.split("=", 1)[1] for part in command if part.startswith("--junitxml="))
        Path(junit).write_text(
            '<testsuites tests="9" failures="0" errors="0" skipped="0"></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("UNRELATED_PRODUCTION_CREDENTIAL", "must-not-cross-boundary")
    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    summary = verify._run_pytest(verify.bindings_from_status(SAFE_STATUS))

    assert summary.tests == 9 and summary.returncode == 0
    assert "tests/test_reading_history_local_db.py" in observed
    assert "tests/test_personalization_local_db.py" in observed
    assert not any("test_personalization_postgres_runtime.py" in part for part in observed)
    assert "--maxfail=1" in observed
    assert "UNRELATED_PRODUCTION_CREDENTIAL" not in child_environment


def test_nested_pytest_junit_counts_are_aggregated(tmp_path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite tests="4" failures="0" errors="0" skipped="0" />'
        '<testsuite tests="5" failures="0" errors="0" skipped="0" /></testsuites>',
        encoding="utf-8",
    )

    assert verify._parse_junit(report, 0) == verify.TestSummary(9, 0, 0, 0, 0)
