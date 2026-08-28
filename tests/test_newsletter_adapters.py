"""The five-sender allowlist, against the synthetic fixtures.

What these tests prove: the parser handles this SHAPE, drops the furniture, and
routes every link through the sanitizer. What they cannot prove: the hit rate
against a real mailbox. The fixtures are a reconstruction, not a capture, so
the honest measurement is the per-run report the lane emits after OAuth.
"""

from __future__ import annotations

import pytest

from curator.newsletter import adapters
from tests.test_newsletter_fixtures import (
    FAKE_TOKENS,
    SENDERS,
    build_message,
    load_html,
    parsed,
)

ALL = sorted(SENDERS)


def extract(name: str, **kwargs):
    msg = parsed(name, **kwargs)
    adapter = adapters.for_sender(adapters.sender_address(msg))
    assert adapter is not None, f"no adapter matched the sender for {name}"
    return adapter, adapter.extract(msg)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ALL)
def test_each_fixture_routes_to_its_adapter(name):
    adapter, _ = extract(name)
    assert adapter.id == name


def test_an_unknown_sender_matches_nothing():
    assert adapters.for_sender("stranger@somewhere.example") is None
    assert adapters.for_sender("") is None


def test_subdomain_senders_match_the_registered_domain():
    adapter = adapters.for_sender("news@mail.therundown.ai")
    assert adapter is not None and adapter.id == "therundown"


def test_sender_queries_cover_only_the_enabled_adapters():
    terms = adapters.sender_queries(["tldr"])
    assert "tldrnewsletter.com" in terms
    assert not any("beehiiv" in t or "milkroad" in t for t in terms)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ALL)
def test_every_adapter_extracts_stories_from_its_fixture(name):
    _, result = extract(name)
    assert result.report.stories_found >= 3
    assert len(result.stories) == result.report.stories_found
    for story in result.stories:
        assert story.title.strip()
        assert len(story.title) <= adapters.MAX_TITLE_CHARS
        assert len(story.blurb) <= adapters.MAX_BLURB_CHARS


@pytest.mark.parametrize("name", ALL)
def test_boilerplate_never_becomes_a_story(name):
    _, result = extract(name)
    titles = " ".join(s.title for s in result.stories).casefold()
    for word in ("unsubscribe", "view in browser", "sponsor", "refer a friend", "manage your subscription"):
        assert word not in titles


@pytest.mark.parametrize("name", ALL)
def test_the_hit_report_adds_up(name):
    _, result = extract(name)
    report = result.report
    assert report.links_sanitized + report.links_dropped == report.stories_found
    kept = [s for s in result.stories if s.url]
    assert len(kept) == report.links_sanitized


@pytest.mark.parametrize("name", ALL)
def test_no_story_url_carries_the_fixture_token(name):
    _, result = extract(name)
    for story in result.stories:
        assert FAKE_TOKENS[name] not in (story.url or "")


def test_tldr_strips_the_read_time_suffix():
    _, result = extract("tldr")
    assert any(s.title == "Example Labs ships a 3nm inference chip" for s in result.stories)
    assert not any("minute read" in s.title for s in result.stories)


def test_tldr_recovers_publisher_urls_from_its_tracker():
    _, result = extract("tldr")
    assert result.report.links_dropped == 0
    assert all(s.url.endswith(("/", "chip/", "siting/", "small-models/")) for s in result.stories)


def test_beehiiv_opaque_links_drop_while_direct_links_survive():
    _, result = extract("therundown")
    assert result.report.links_dropped >= 1
    assert result.report.links_sanitized >= 1
    dropped = [s for s in result.stories if not s.url]
    assert dropped and dropped[0].blurb, "a link-less story still keeps its blurb"


def test_milkroad_strips_utm_and_subscriber_parameters():
    _, result = extract("milkroad")
    custody = [s for s in result.stories if "custody" in s.title.casefold()]
    assert custody and custody[0].url == "https://www.chainledger.example/2026/08/27/custody-rule"


def test_bensbites_reads_plain_link_lists():
    _, result = extract("bensbites")
    assert len(result.stories) == 3
    assert any("Long context did not kill retrieval" in s.title for s in result.stories)


# --------------------------------------------------------------------------
# MIME realities
# --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["alternative", "html_only", "base64", "nested"])
def test_every_mime_shape_yields_the_same_stories(shape):
    _, baseline = extract("tldr", shape="alternative")
    _, other = extract("tldr", shape=shape)
    assert [s.title for s in other.stories] == [s.title for s in baseline.stories]


def test_an_attachment_is_never_read_as_the_body():
    msg = parsed("theneuron", shape="nested")
    assert "NOT THE NEWSLETTER" not in adapters.html_body(msg)


def test_a_message_with_no_html_part_yields_nothing_rather_than_raising():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = SENDERS["tldr"]
    msg.set_content("plain text only, no HTML alternative")
    adapter = adapters.for_sender(adapters.sender_address(msg))
    result = adapter.extract(msg)
    assert result.stories == []
    assert result.report.stories_found == 0


def test_malformed_html_does_not_raise():
    broken = "<html><body><p><a href='https://lab.example/x'><strong>A headline that is long enough"
    msg = build_message("tldr", html=broken)
    adapter = adapters.by_id("tldr")
    result = adapter.extract(msg)
    assert isinstance(result.stories, list)


def test_script_content_is_never_extracted():
    html = load_html("tldr").replace(
        "<h2>Big Tech &amp; Startups</h2>",
        "<script>var x = 'alert-me';</script><h2>Big Tech &amp; Startups</h2>",
    )
    msg = build_message("tldr", html=html)
    result = adapters.by_id("tldr").extract(msg)
    blob = " ".join(s.title + s.blurb for s in result.stories)
    assert "alert-me" not in blob
