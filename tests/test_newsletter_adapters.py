"""The five-sender allowlist, against fixtures captured from the real mailbox.

Four of the five fixtures are now real messages with every identifier scrubbed
out, and that changes what these tests are worth. They used to prove that the
parser handled a shape someone had written down from memory. The first live run
showed the difference: 15 real TLDR messages produced 0 stories while every one
of these tests was green, because the reconstruction wrote `<meta ... />` and
real TLDR mail writes `<meta ...>`.

So the counts below are MEASURED, not chosen. `EXPECTED_STORIES` and
`EXPECTED_LINKED` are what a real issue from each sender yields today, and a
change to any of them is a real change in what the parser gets out of real
mail. What these tests still cannot prove is the hit rate over TIME: senders
redesign, and the lane's per-run report remains the honest measurement.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest

from curator.newsletter import adapters
from tests.test_newsletter_fixtures import (
    EXPECTED_LINKED,
    EXPECTED_STORIES,
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


@pytest.mark.parametrize(
    "address, expected",
    [
        # The two live senders in the surveyed inbox are both subdomains.
        ("team@newsletter.theneurondaily.com", "theneuron"),
        ("hello@mail.milkroad.com", "milkroad"),
        ("dan@tldrnewsletter.com", "tldr"),
        # A lookalike is not a subdomain: the match is at a dot boundary.
        ("hello@evilmilkroad.com", None),
        ("hello@milkroad.com.attacker.example", None),
        ("hello@notmilkroad.com", None),
        # A bare domain is not an address.
        ("milkroad.com", None),
    ],
)
def test_domain_matching_is_a_suffix_match_at_a_dot_boundary(address, expected):
    adapter = adapters.for_sender(address)
    assert (adapter.id if adapter else None) == expected


def test_an_exact_address_entry_does_not_widen_to_its_domain_twice():
    """`dan@tldrnewsletter.com` is listed exactly; the bare domain is too."""
    tldr = adapters.by_id("tldr")
    assert tldr.matches("someone-else@tldrnewsletter.com"), "the bare domain entry covers it"
    assert not tldr.matches("dan@tldrnewsletter.com.attacker.example")


# --------------------------------------------------------------------------
# sender authentication (round 1, S2)
# --------------------------------------------------------------------------

def test_a_real_shaped_header_reads_as_a_pass():
    msg = build_message("tldr")
    tldr = adapters.by_id("tldr")
    assert adapters.dkim_results(msg), "the fixture must carry a header to parse"
    assert adapters.authentication(msg, tldr) == adapters.AUTH_PASS


def test_a_signature_from_another_domain_is_a_fail():
    msg = build_message("tldr", dkim_domain="attacker.example")
    assert adapters.authentication(msg, adapters.by_id("tldr")) == adapters.AUTH_FAIL


def test_a_missing_header_is_its_own_verdict():
    msg = build_message("tldr", authenticated=False)
    assert adapters.authentication(msg, adapters.by_id("tldr")) == adapters.AUTH_MISSING


def test_a_pass_in_one_clause_is_not_paired_with_a_domain_from_another():
    """`spf=pass ... header.d` from a different method must not authorise DKIM."""
    msg = build_message("tldr", authenticated=False)
    msg["Authentication-Results"] = (
        "mx.google.com; dkim=fail header.d=attacker.example; "
        "spf=pass smtp.mailfrom=bounce@tldrnewsletter.com header.d=tldrnewsletter.com"
    )
    assert adapters.authentication(msg, adapters.by_id("tldr")) == adapters.AUTH_FAIL


def test_a_subdomain_signature_authenticates_the_parent_domain():
    msg = build_message("theneuron", dkim_domain="newsletter.theneurondaily.com")
    assert adapters.authentication(msg, adapters.by_id("theneuron")) == adapters.AUTH_PASS


def test_a_lookalike_signing_domain_does_not_authenticate():
    msg = build_message("milkroad", dkim_domain="evilmilkroad.com")
    assert adapters.authentication(msg, adapters.by_id("milkroad")) == adapters.AUTH_FAIL


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


@pytest.mark.parametrize("name", ALL)
def test_each_fixture_yields_the_number_of_stories_it_was_measured_to_yield(name):
    """Pin the real numbers, so a parser change has to justify itself.

    Not a target. These are what a real issue produces, and the point of
    writing them down is that the first live run found `tldr` producing zero
    while every test was green.
    """
    _, result = extract(name)
    assert len(result.stories) == EXPECTED_STORIES[name]
    assert result.report.links_sanitized == EXPECTED_LINKED[name]


# --------------------------------------------------------------------------
# the bug the first live run found
# --------------------------------------------------------------------------

def test_an_unclosed_void_tag_does_not_swallow_the_document():
    """The TLDR zero, as a test that fails on the old parser.

    Real TLDR mail writes `<meta ...>` with no closing slash, six times, in the
    head. `meta` used to be in `_SKIP_TAGS`, so each one opened a skip scope
    that nothing could ever close, and every block after the head was thrown
    away: 15 real messages, 0 stories, all tests green.
    """
    html = load_html("tldr")
    assert re.search(r"<meta\b[^>]*(?<!/)>", html), (
        "the captured fixture must still contain the unslashed meta tag"
    )
    _, result = extract("tldr")
    assert len(result.stories) >= 10

    # And the same document with the slash restored, which is what the
    # hand-written fixture used to say, parses to the same stories. The format
    # difference was never the point; the never-closing skip scope was.
    slashed = re.sub(r"<meta\b([^>]*)(?<!/)>", r"<meta\1/>", html)
    other = adapters.by_id("tldr").extract(build_message("tldr", html=slashed))
    assert [s.title for s in other.stories] == [s.title for s in result.stories]


@pytest.mark.parametrize("tag", sorted(adapters._VOID_TAGS))
def test_no_void_element_is_treated_as_a_skippable_container(tag):
    """The general form: a tag with no end tag must never open a skip scope."""
    assert tag not in adapters._SKIP_TAGS
    html = f"<html><body><{tag}><p><a href='https://lab.example/x'><strong>" \
           "A headline long enough to qualify</strong></a></p><p>Blurb text.</p></body></html>"
    stories = adapters.extract_stories(html)
    assert [s.title for s in stories] == ["A headline long enough to qualify"]


def test_tldr_strips_the_read_time_suffix():
    """`(4 minute read)` is TLDR's real suffix; the capture confirms it."""
    assert "minute read)" in load_html("tldr")
    _, result = extract("tldr")
    assert result.stories
    assert not any("minute read" in s.title for s in result.stories)


def test_tldr_recovers_publisher_urls_from_its_tracker():
    """Every TLDR link is recoverable: the destination is in the path.

    `tracking.tldrnewsletter.com/CL0/<percent-encoded destination>/1/...` is a
    sendgrid click-tracking shape that carries the article with it, so the
    sanitizer unwraps all of them offline.
    """
    _, result = extract("tldr")
    assert result.report.links_dropped == 0
    for story in result.stories:
        assert story.url.startswith("https://")
        assert "tldrnewsletter" not in story.url
        assert urlsplit(story.url).hostname.endswith(".example")


# --------------------------------------------------------------------------
# beehiiv: the link lives in the other half of the message
# --------------------------------------------------------------------------

def test_a_beehiiv_href_is_opaque_in_the_html(name="therundown"):
    """The premise. If this ever stops being true, the recovery can go."""
    hrefs = re.findall(r'href="(https://link\.mail\.beehiiv\.com/ss/c/[^"]+)"', load_html(name))
    assert hrefs, "the capture must contain beehiiv's wrapped links"
    for href in hrefs:
        assert adapters.sanitize(href) is None, "a beehiiv wrapper must not resolve statically"


def test_beehiiv_links_are_recovered_from_the_plain_text_half():
    _, result = extract("theneuron")
    assert result.report.links_dropped == 0
    assert result.report.links_sanitized == EXPECTED_LINKED["theneuron"]
    for story in result.stories:
        assert "beehiiv" not in story.url


def test_without_the_plain_text_half_the_same_message_ships_linkless():
    """Proves the recovery is what is doing the work, not the HTML."""
    _, with_plain = extract("theneuron")
    _, html_only = extract("theneuron", shape="html_only")
    assert with_plain.report.links_sanitized > 0
    assert html_only.report.links_sanitized == 0
    assert html_only.report.stories_found == with_plain.report.stories_found
    assert all(s.blurb for s in html_only.stories if s.blurb == s.blurb)


def _with_plain_text(plain: str):
    """A minimal message carrying only the given plain-text part."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = SENDERS["theneuron"]
    msg.set_content(plain)
    return msg


def test_the_plain_text_table_reads_the_captured_markdown():
    table = adapters.plain_text_destinations(parsed("theneuron"))
    assert len(table) > 20, "the capture must contain markdown links to read"


def test_an_ambiguous_plain_text_label_is_not_guessed_at():
    """One label, two destinations: ship linkless rather than pick one.

    Pointing a headline at the wrong article is a worse failure than shipping
    it with no link, so the ambiguous key is dropped from the table entirely.
    """
    key = adapters._match_key("A headline that appears twice")
    once = _with_plain_text("[A headline that appears twice](https://one.example/a)\n")
    assert adapters.plain_text_destinations(once).get(key) == "https://one.example/a"

    twice = _with_plain_text(
        "[A headline that appears twice](https://one.example/a)\n"
        "[A headline that appears twice](https://two.example/b)\n"
    )
    assert key not in adapters.plain_text_destinations(twice)


def test_a_short_label_is_never_matched_by_containment():
    """"Read more" appears under every story; it must not link any of them."""
    stories = [adapters.Story(title="Read more", url_raw="")]
    msg = _with_plain_text("[Read more about something else](https://wrong.example/x)\n")
    adapters.recover_destinations(msg, stories)
    assert stories[0].url_raw == ""


def test_a_recovered_destination_still_goes_through_the_sanitizer():
    """The plain-text half widens what is RECOVERED, never what is PUBLISHED."""
    stories = [adapters.Story(title="A headline long enough to match here", url_raw="")]
    msg = _with_plain_text(
        "[A headline long enough to match here]"
        "(https://tracker.example/r/AbCdEf0123456789XyZq?email=reader%40example.invalid)\n"
    )
    adapters.recover_destinations(msg, stories)
    assert adapters.sanitize(stories[0].url_raw) is None


# --------------------------------------------------------------------------
# Milk Road writes headlines, not links
# --------------------------------------------------------------------------

def test_milkroad_stories_come_from_headings_and_ship_without_links():
    _, result = extract("milkroad")
    assert len(result.stories) == EXPECTED_STORIES["milkroad"]
    assert result.report.links_sanitized == 0
    for story in result.stories:
        assert story.url == ""
        assert story.blurb, "a heading with no prose under it is a section label, not a story"


def test_headings_do_not_start_stories_for_the_other_senders():
    """TLDR's `<h2>Big Tech & Startups</h2>` must not become a headline."""
    _, result = extract("tldr")
    titles = {s.title.casefold() for s in result.stories}
    assert "big tech & startups" not in titles
    assert "science & futuristic technology" not in titles


def test_a_heading_with_nothing_under_it_is_not_a_story():
    html = ("<html><body><h1>A section label with no prose at all</h1>"
            "<h1>A real headline with a sentence below</h1>"
            "<p>The sentence that makes it a story rather than a label.</p></body></html>")
    stories = adapters.extract_stories(html, headings_start_stories=True)
    assert [s.title for s in stories] == ["A real headline with a sentence below"]


# --------------------------------------------------------------------------
# advertising does not become news
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Join The Rundown Tech",
    "Check out ours here",
    "Lock in $250/year - yours for life",
    "FREE TRADING COMMUNITY WITH 14,000+ MEMBERS",
    "FREE SEMINAR ON BLOCKCHAIN & PRIVATE MARKETS",
])
def test_a_call_to_action_is_not_a_headline(title):
    assert adapters.is_promo_headline(title)


@pytest.mark.parametrize("title", [
    "Anthropic wires AI agents into real-world machines",
    "UK trials live-video AI in brain surgery",
    "Nvidia is reportedly buying Hugging Face for $12.9B",
    # A real headline may contain a CTA word away from the front.
    "Regulators join the debate over agent liability",
])
def test_a_real_headline_is_not_read_as_advertising(title):
    assert not adapters.is_promo_headline(title)


def test_shouting_is_an_advertisement_on_an_anchor_and_a_voice_on_a_heading():
    """Milk Road writes its own headlines in block capitals."""
    shouted = "AI AGENTS ARE PAYING EACH OTHER ONCHAIN AGAIN"
    assert adapters.is_promo_headline(shouted)
    assert not adapters.is_promo_headline(shouted, shouting_is_promo=False)


# --------------------------------------------------------------------------
# prose is a channel to the page too
# --------------------------------------------------------------------------

def test_an_address_written_in_the_prose_never_reaches_a_story():
    """The sanitizer guards URLs. Nothing was guarding the sentences.

    Found by the live run: TLDR's referral line carries an address in body copy
    and it reached the artifact verbatim. That one is TLDR's public jobs inbox,
    but "You are subscribed as <address>" is the same sentence shape.
    """
    html = (
        "<html><body><p><a href='https://lab.example/x'><strong>"
        "A headline long enough to qualify</strong></a></p>"
        "<p>Send a resume to jobs@example.invalid and we will take a look.</p>"
        "</body></html>"
    )
    stories = adapters.extract_stories(html)
    assert stories
    assert "@" not in stories[0].blurb
    assert adapters.ADDRESS_PLACEHOLDER in stories[0].blurb


def test_an_address_in_a_headline_is_redacted_too():
    html = ("<html><body><p><a href='https://lab.example/x'><strong>"
            "Write to hello@example.invalid about the thing</strong></a></p>"
            "<p>Some blurb.</p></body></html>")
    stories = adapters.extract_stories(html)
    assert stories and "@" not in stories[0].title


def test_beehiiv_opaque_links_drop_while_direct_links_survive():
    _, result = extract("therundown")
    assert result.report.links_dropped >= 1
    assert result.report.links_sanitized >= 1
    dropped = [s for s in result.stories if not s.url]
    assert dropped and dropped[0].blurb, "a link-less story still keeps its blurb"


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
