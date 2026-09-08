from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests/browser_auth_contract_runner.js"


@pytest.mark.parametrize("cross_second_boundary", [False, True], ids=["real-clock", "cross-second"])
def test_browser_auth_and_preference_contract_executes(cross_second_boundary: bool) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable, so the executable browser contract cannot run.")
    command = [node, str(RUNNER)]
    if cross_second_boundary:
        # Async PKCE may finish in the next second. Exercise that boundary
        # without sleeping or changing the product's clock implementation.
        command = [node, "-e", (
            "const started=Date.now(); let calls=0;"
            "Date.now=()=>started+(calls++ ? 1000 : 0);"
            "require(process.argv[1]);"
        ), str(RUNNER)]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "browser personalization contract: PASS"
