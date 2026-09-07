from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests/reader_contract_runner.js"


def test_reader_contract_executes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable, so the reader contract cannot run.")
    result = subprocess.run(
        [node, str(RUNNER)], cwd=ROOT, capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reader contract: PASS"
