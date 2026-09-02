"""The checked-in kill switch must make zero provider calls."""

from __future__ import annotations

import json

import pytest

from scripts.run_translation_job import main


@pytest.mark.parametrize("target_language", ("en", "zh"))
def test_disabled_translation_writes_empty_artifact_before_any_secret_or_provider_access(
    tmp_path, target_language
) -> None:
    (tmp_path / "topics.yaml").write_text(
        "categories:\n  - {id: ai, name: AI, keywords: [AI]}\n", encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text(
        "hackernews: {enabled: false}\n"
        f"translation: {{enabled: false, targets: [{target_language}]}}\n"
        "rss: []\n",
        encoding="utf-8",
    )
    output = tmp_path / "translations.json"

    assert main(
        [
            "--root",
            str(tmp_path),
            "--output",
            str(output),
            "--google-access-token-file",
            str(tmp_path / "must-not-be-read"),
        ]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["translations"] == []
