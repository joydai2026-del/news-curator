from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests/browser_auth_contract_runner.js"


def test_browser_auth_and_preference_contract_executes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable, so the executable browser contract cannot run.")
    result = subprocess.run(
        [node, str(RUNNER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "browser personalization contract: PASS"
