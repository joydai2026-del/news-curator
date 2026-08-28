"""The PRIVACY RULE, asserted rather than promised.

The design doc's hard requirement: no newsletter-derived URL containing a token
pattern reaches the rendered page, the cache, or the logs. This file is that
test. It runs the whole lane over all five synthetic fixtures with the Gmail
HTTP layer faked out, then goes looking for leaks in the three places a leak
could land:

    1. the items handed to the renderer
    2. the committed state file
    3. every log record the lane emitted

Each fixture carries a distinct fake subscriber token and a fake reader
address, both of which appear in that fixture's tracking and unsubscribe links.
The first test below proves the fixtures really do carry them, so a green run
here means the sanitizer removed something rather than that there was nothing
to remove.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from curator.newsletter import adapters, lane, sanitize, state as state_module
from tests.test_newsletter_fixtures import (
    FAKE_READER,
    FAKE_TOKENS,
    SENDERS,
    SUBJECTS,
    field,
    load_html,
    parsed,
)
from tests.test_newsletter_lane import ENV, FakeGmail

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

CFG = {"enabled": True, "max_items": 50, "max_age_hours": 48}

# A long opaque run of base64/hex characters: the shape a subscriber id takes.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{20,}")

LEAK_STRINGS = tuple(FAKE_TOKENS.values()) + (FAKE_READER,) + tuple(SUBJECTS.values())


class Run:
    """One full lane run: its result, its starting cursor, and its log text.

    The log text is captured with a handler owned by this fixture rather than
    by `caplog`, because pytest clears caplog's records between the setup and
    call phases and an empty log would make the leak assertions pass for the
    wrong reason.
    """

    def __init__(self, result, state, logs):
        self.result = result
        self.state = state
        self.logs = logs


@pytest.fixture
def run():
    messages = [parsed(name, sent=NOW - timedelta(hours=2)) for name in SENDERS]
    st = state_module.NewsletterState(watermark=NOW - timedelta(hours=6), salt="fixture-salt")

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("curator")
    handler = Capture(level=logging.DEBUG)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        result = lane.fetch(CFG, st, NOW, env=ENV, client=FakeGmail(messages))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    return Run(result, st, "\n".join(r.getMessage() for r in records))


# --------------------------------------------------------------------------
# the test is not vacuous
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SENDERS))
def test_the_fixture_really_does_contain_something_to_leak(name):
    html = load_html(name)
    assert FAKE_TOKENS[name] in html
    assert FAKE_READER in html or "unsub" in html.lower()


def test_the_run_actually_produced_items(run):
    result = run.result
    assert result.ok and len(result.items) >= 10


# --------------------------------------------------------------------------
# 1. the items
# --------------------------------------------------------------------------

def test_no_item_field_carries_a_token_or_an_address(run):
    result = run.result
    for item in result.items:
        blob = " ".join(
            str(field(item, name))
            for name in ("title", "url", "canonical_url", "source_id", "source_name",
                         "description", "newsletter_sender")
        )
        for leak in LEAK_STRINGS:
            assert leak not in blob, f"item leaked {leak!r}"


def test_every_item_url_is_either_empty_or_provably_clean(run):
    result = run.result
    for item in result.items:
        url = field(item, "url")
        if not url:
            continue
        assert not sanitize.is_tracker_host(url), "a tracker host reached the page"
        assert not sanitize.is_suspect(url), "a token-shaped URL reached the page"
        assert sanitize.sanitize(url) == url, "the URL is not a sanitizer fixed point"


def test_no_item_url_contains_a_token_shaped_segment(run):
    result = run.result
    for item in result.items:
        url = field(item, "url")
        for match in TOKEN_PATTERN.findall(url or ""):
            assert not sanitize.is_token_like(match), f"opaque segment {match!r} survived"


def test_newsletter_items_never_carry_an_image(run):
    """No og:image fetch, no image-cache entry. The rule starts here."""
    result = run.result
    assert result.items
    for item in result.items:
        assert field(item, "image_url") == ""


def test_a_dropped_link_is_dropped_not_downgraded(run):
    """The give-up path renders no link at all, never a partial tracker URL."""
    result = run.result
    linkless = [i for i in result.items if not field(i, "url")]
    assert linkless, "the fixtures contain unresolvable tracker links"
    for item in linkless:
        assert field(item, "url") == ""


# --------------------------------------------------------------------------
# 2. the state file
# --------------------------------------------------------------------------

def test_the_state_file_round_trips_with_only_the_four_allowed_keys(run, tmp_path):
    result, st = run.result, run.state
    path = tmp_path / "newsletter_state.json"
    written = state_module.advance(path, st, watermark=result.watermark, new_hashes=result.hashes)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "watermark", "salt", "hashes"}
    assert payload["hashes"] == written.hashes
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in payload["hashes"])

    reloaded = state_module.load(path, now=NOW)
    assert reloaded.salt == written.salt and reloaded.hashes == written.hashes


def test_the_state_file_contains_no_token_address_url_or_headline(run, tmp_path):
    result, st = run.result, run.state
    path = tmp_path / "newsletter_state.json"
    state_module.advance(path, st, watermark=result.watermark, new_hashes=result.hashes)
    raw = path.read_text(encoding="utf-8")

    for leak in LEAK_STRINGS:
        assert leak not in raw
    assert "http" not in raw, "no URL, tracker or publisher, belongs in a public cursor"
    assert "@" not in raw
    for item in result.items:
        assert field(item, "title") not in raw


# --------------------------------------------------------------------------
# 3. the logs
# --------------------------------------------------------------------------

def test_no_log_record_carries_a_token_address_subject_or_url(run):
    result = run.result
    blob = run.logs
    assert blob.strip(), "the lane must log something, or this test proves nothing"
    for leak in LEAK_STRINGS:
        assert leak not in blob, f"log leaked {leak!r}"
    assert "http" not in blob, "no URL may be logged"
    assert "@" not in blob, "no address may be logged"
    for item in result.items:
        assert field(item, "title") not in blob


def test_logs_do_carry_the_adapter_slugs_and_counts(run):
    """The rule permits exactly this, and the lane has to stay diagnosable."""
    blob = run.logs
    for adapter in adapters.ADAPTERS:
        assert adapter.id in blob
    assert "extracted=" in blob and "dropped_links=" in blob
