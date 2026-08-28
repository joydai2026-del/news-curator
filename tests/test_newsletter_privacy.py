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
from pathlib import Path

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


# --------------------------------------------------------------------------
# 4. every boundary at once (round 1, S5)
# --------------------------------------------------------------------------
"""Why the section below exists.

The three sections above stop at `lane.fetch`, and their "provably clean"
assertions ask the sanitizer whether the sanitizer's own output is clean. Both
of round 1's leaks passed all of them. So this section crosses every boundary a
URL actually travels, with fixtures chosen to carry the exact shapes that
leaked, and asks the LAST artifact in the chain, the rendered HTML, whether the
strings are there. It knows nothing about the sanitizer's definition of clean.

The chain: Gmail's own base64/MIME decode -> lane.fetch -> serialize ->
JSON file -> load_newsletter_artifact -> dedupe against a publisher decoy ->
render_html.
"""

LEAK_HTML = load_html("leakshapes")

# The literal substrings that must not survive to the page. Each one is a
# distinct M2 shape, and the first test below proves the fixture carries them.
E2E_LEAKS = (
    "fixture-reader@example.invalid",
    "fixture-reader%40example.invalid",
    "fixture-reader%2540example.invalid",
    "SUBleak7f3a9c2b4d6e8f0a",
    "SUBleak93d17ea4b8f2605c1d9e7a3",
    "subid=JJ7742",
    "JJ7742",
    "token=aBcDeFgHiJkLmNoP",
    "aBcDeFgHiJkLmNoP",
    "ref=jj7742",
    "jj7742",
    "tracking.tldrnewsletter.com",
    "link.mail.beehiiv.com",
)

# Links that SHOULD survive. Without these, a renderer that dropped every card
# would pass every leak assertion above.
#
# `E2E_STRIPPED` is the `?subid=JJ7742` story with its identifier removed: it
# proves the sanitizer STRIPPED the parameter rather than throwing the link
# away, which is the difference between a working lane and an empty one.
E2E_STRIPPED = "https://example.com/inference-chip-story"
# `E2E_CLEAN` needs no cleaning at all. It is asserted on the ITEMS rather than
# the page because the publisher decoy wins the fuzzy merge and displays its
# own URL; what matters here is that the lane passed it through untouched.
E2E_CLEAN = "https://publisher.example/quantum-chip-story"


def e2e_render():
    """The whole chain, run once, returning the final HTML and the survivors."""
    import json as json_mod
    from tempfile import TemporaryDirectory

    from curator.dedup import dedupe
    from curator.models import Item
    from curator.newsletter import gmail as gmail_module
    from curator.newsletter.__main__ import serialize
    from curator.pipeline import NEWSLETTER_CATEGORY_NAME, load_newsletter_artifact
    from curator.render import render_html
    from tests.test_newsletter_fixtures import as_raw, build_message
    from tests.test_newsletter_gmail import FakeResponse, FakeSession

    # 1. through the REAL Gmail client, so the base64 + MIME decode is in the
    #    chain too. Only the HTTP session is faked.
    message = build_message("tldr", html=LEAK_HTML, sent=NOW - timedelta(hours=2))
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}]}),
        messages={"m1": FakeResponse(200, {"raw": as_raw(message)})},
    )

    class RealClientOverFakeSession:
        """`lane.fetch`'s `client` seam, wired to the real gmail module."""

        def has_credentials(self, env):
            return True

        def fetch(self, senders, after, *, env=None, limit=30, timeout=20.0):
            return gmail_module.fetch(
                senders, after, env=ENV, session=session, limit=limit, timeout=timeout
            )

    st = state_module.NewsletterState(watermark=NOW - timedelta(hours=6), salt="fixture-salt")
    result = lane.fetch(CFG, st, NOW, env=ENV, client=RealClientOverFakeSession())

    # 2. serialize -> a real JSON file -> reconstruct
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "artifact.json"
        path.write_text(json_mod.dumps(serialize(result)), encoding="utf-8")
        items, _tier, _meta = load_newsletter_artifact(path)

    # 3. dedupe against a publisher decoy carrying a near-identical headline,
    #    which is the fuzzy merge that put a newsletter URL on a publisher card.
    #    Its URL differs from the newsletter copy's on purpose: an identical
    #    URL merges in pass 1 and proves nothing, while a near-identical TITLE
    #    with a different URL is the pass-2 merge that put a newsletter link
    #    into a publisher card's cluster.
    decoy = Item(
        title="Apple ships the quantum chip",
        url="https://publisher.example/2026/08/quantum-chip",
        canonical_url="https://publisher.example/2026/08/quantum-chip",
        source_id="verge",
        source_name="The Verge",
        published_at=NOW - timedelta(hours=3),
    )
    survivors = dedupe(list(items) + [decoy])

    # 4. render
    html = render_html({NEWSLETTER_CATEGORY_NAME: survivors}, [], NOW)
    return html, survivors, items


def test_the_leak_fixture_really_carries_every_shape_that_leaked():
    """Non-vacuity, asserted before anything is asserted to be absent."""
    assert "SYNTHETIC FIXTURE" in LEAK_HTML
    for leak in E2E_LEAKS:
        if leak in ("fixture-reader%2540example.invalid",):
            continue  # the doubly-encoded form is a sanitizer test, not a fixture shape
        assert leak in LEAK_HTML, f"the fixture no longer carries {leak!r}"
    assert E2E_CLEAN in LEAK_HTML


def test_the_end_to_end_chain_actually_produced_a_page():
    html, survivors, items = e2e_render()
    assert items, "the lane produced no items, so the leak assertions prove nothing"
    assert survivors and "<article" in html
    assert E2E_STRIPPED in html, "a stripped link must still reach the page as a link"
    assert E2E_CLEAN in {i.url for i in items}, "a clean link must pass through untouched"


def test_the_publisher_decoy_wins_the_fuzzy_merge_without_taking_the_newsletter_url():
    """The pass-2 merge really happened, and the cluster stayed empty."""
    _html, survivors, items = e2e_render()
    assert len(survivors) == len(items), "one row merged away, so the merge path ran"
    (merged,) = [s for s in survivors if s.source_name == "The Verge"]
    assert merged.cluster == [], "no newsletter URL rode the cluster onto a publisher card"


@pytest.mark.parametrize("leak", E2E_LEAKS)
def test_no_leak_shape_survives_to_the_rendered_page(leak):
    html, _survivors, _items = e2e_render()
    assert leak not in html, f"{leak!r} reached the rendered page"


def test_no_href_on_the_page_carries_an_address_shaped_query():
    """The output-boundary form of the rule, independent of the sanitizer."""
    import html as html_mod
    from urllib.parse import urlsplit

    page, _survivors, _items = e2e_render()
    hrefs = [html_mod.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]
    assert hrefs, "no links on the page means this test proves nothing"
    for href in hrefs:
        query = urlsplit(href).query
        assert "@" not in query and "%40" not in query.lower(), href


def test_the_unresolvable_tracker_story_renders_with_no_link_at_all():
    """The give-up path, end to end: a card, and no href pointing at a tracker."""
    _page, _survivors, items = e2e_render()
    linkless = [i for i in items if not i.url]
    assert linkless, "the fixture's opaque tracker link must have been dropped"
