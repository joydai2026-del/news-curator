"""The fetch job's CLI: it always exits 0, and it always says why.

The whole point of this entry point is that a broken newsletter lane must not
fail an hourly build that has six healthy tabs. The risk that creates is
silence: an exit code of 0 with nothing in the run summary looks exactly like a
good run. So every degraded state has to leave a `::warning::` annotation, and
these tests are what stop one of them slipping back into quiet mode.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from curator.newsletter import lane, state as state_module
from curator.newsletter.__main__ import CONFIG_ERROR, main, serialize
from tests.test_newsletter_fixtures import SENDERS, parsed
from tests.test_newsletter_lane import ENV, FakeGmail

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_a_config_error_writes_a_dark_artifact_and_a_warning(tmp_path, capsys):
    """Round 1, L5: this path returned before the annotation ever ran."""
    out = tmp_path / "artifact.json"
    code = main(["--root", str(tmp_path), "--out", str(out)])  # no topics.yaml here

    assert code == 0, "the fetch job never fails the build"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["dark"] is True and payload["reason"] == CONFIG_ERROR
    assert f"::warning::newsletter lane is dark this run: {CONFIG_ERROR}" in capsys.readouterr().out


def lane_result(**kwargs):
    messages = [parsed(name, sent=NOW - timedelta(hours=2)) for name in SENDERS]
    st = state_module.NewsletterState(watermark=NOW - timedelta(hours=6), salt="fixture-salt")
    return lane.fetch(
        {"enabled": True}, st, NOW, env=ENV, client=FakeGmail(messages, **kwargs)
    )


def test_the_artifact_carries_what_the_run_did_not_see():
    """A short batch has to be visible in the artifact, not only in the fetch log."""
    payload = serialize(lane_result(truncated=True, fetch_failures=3))
    assert payload["truncated"] is True
    assert payload["unreadable_messages"] == 3
    assert payload["unauthenticated_messages"] == 0
    assert payload["unauthenticated_missing"] == 0


def test_a_clean_run_says_nothing_was_missed():
    payload = serialize(lane_result())
    assert payload["truncated"] is False and payload["unreadable_messages"] == 0


def test_the_artifact_still_carries_no_address_or_subject():
    blob = json.dumps(serialize(lane_result(truncated=True)))
    assert "@" not in blob
    assert "fixture-reader" not in blob
