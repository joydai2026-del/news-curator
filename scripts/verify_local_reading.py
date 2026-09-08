#!/usr/bin/env python3
"""Verify the two local Supabase reading and preference security matrices.

Prerequisite: start Supabase and apply the current migrations yourself with
``supabase start`` and ``supabase db reset``. This command never starts,
resets, installs, or repairs anything. It fails closed when the current local
status is incomplete, non-loopback, unreachable, skipped, or empty.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._personalization_local_harness import LocalSupabase  # noqa: E402


TEST_PATHS = (
    "tests/test_reading_history_local_db.py",
    "tests/test_personalization_local_db.py",
)
STATUS_BINDINGS = {
    "API_URL": "NEWS_CURATOR_LOCAL_SUPABASE_URL",
    "ANON_KEY": "NEWS_CURATOR_LOCAL_SUPABASE_ANON_KEY",
    "SERVICE_ROLE_KEY": "NEWS_CURATOR_LOCAL_SUPABASE_SERVICE_ROLE_KEY",
    "JWT_SECRET": "NEWS_CURATOR_LOCAL_SUPABASE_JWT_SECRET",
}


class VerificationError(RuntimeError):
    """Safe operator-facing failure with no credential material."""


@dataclass(frozen=True)
class TestSummary:
    tests: int
    failures: int
    errors: int
    skipped: int
    returncode: int


def _status_output() -> str:
    try:
        result = subprocess.run(
            ["supabase", "status", "-o", "env"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VerificationError("local Supabase status is unavailable") from exc
    if result.returncode != 0:
        raise VerificationError("local Supabase status failed")
    return result.stdout


def bindings_from_status(status_output: str) -> dict[str, str]:
    """Extract required values in memory and validate the exact loopback origin."""
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    for line in status_output.splitlines():
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if name not in STATUS_BINDINGS:
            continue
        try:
            parsed = shlex.split(raw_value)
        except ValueError:
            parsed = []
        if name in values:
            duplicates.add(name)
        if len(parsed) == 1 and parsed[0]:
            values[name] = parsed[0]

    missing = sorted(name for name in STATUS_BINDINGS if not values.get(name))
    if missing or duplicates:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if duplicates:
            details.append("duplicate " + ", ".join(sorted(duplicates)))
        raise VerificationError("missing required local Supabase bindings: " + "; ".join(details))

    # Reuse the test harness's exact origin and credential-presence boundary.
    # This performs no request and deliberately accepts only http://127.0.0.1:<port>.
    try:
        LocalSupabase(values["API_URL"], values["ANON_KEY"], values["SERVICE_ROLE_KEY"])
    except AssertionError as exc:
        raise VerificationError(str(exc)) from exc
    return {target: values[source] for source, target in STATUS_BINDINGS.items()}


def _parse_junit(path: Path, returncode: int) -> TestSummary:
    try:
        root = ET.parse(path).getroot()
        suites = [root] if "tests" in root.attrib else root.findall("./testsuite")
        tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
        failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
        errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    except (OSError, ET.ParseError, TypeError, ValueError) as exc:
        raise VerificationError("pytest did not produce a readable aggregate result") from exc
    return TestSummary(tests, failures, errors, skipped, returncode)


def _run_pytest(bindings: dict[str, str]) -> TestSummary:
    with tempfile.TemporaryDirectory(prefix="news-curator-local-reading-") as temp_dir:
        report = Path(temp_dir) / "pytest.xml"
        # Do not forward ambient credentials into socket-enabled integration tests.
        environment = dict(bindings)
        environment["PYTHONPYCACHEPREFIX"] = str(Path(temp_dir) / "pycache")
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
            "--maxfail=1",
            f"--junitxml={report}",
            *TEST_PATHS,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError("local database verification timed out") from exc
        return _parse_junit(report, result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Prerequisite: run `supabase start` and `supabase db reset` first.",
    )
    parser.parse_args(argv)
    try:
        bindings = bindings_from_status(_status_output())
        summary = _run_pytest(bindings)
    except (AssertionError, VerificationError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if summary.tests == 0:
        print("FAIL: no tests collected.", file=sys.stderr)
        return 1
    if summary.returncode or summary.failures or summary.errors or summary.skipped:
        print(
            f"FAIL: {summary.tests} tests; {summary.skipped} skipped, "
            f"{summary.failures} failed, {summary.errors} errors; "
            f"pytest exit {summary.returncode}.",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: {summary.tests} tests passed; 0 skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
