from __future__ import annotations

import subprocess
from pathlib import Path


def test_m2_reader_service_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", "tests/m2_reader_service_runner.js"], cwd=root, check=True)


def test_m2_controls_are_accessible_and_disabled_by_default() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "curator/render.py").read_text()
    reader = (root / "static/reader.js").read_text()
    assert '<meta name="news-curator-m2-enabled" content="false">' in source
    assert '<meta name="news-curator-m2-provider-policy-id" content="">' in source
    assert 'aria-label="Personalized feed controls" hidden' in source
    assert "Learn from my reading" in source
    assert "Use my history for model ranking" in source
    assert "Provider data policy" in source
    assert 'document.getElementById("discovery-controls")?.setAttribute("hidden", "")' in reader
