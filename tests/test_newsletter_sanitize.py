"""The URL privacy rule, tested one tracker shape at a time.

The contract under test: a newsletter link becomes a publisher link that
carries no identifier, or it becomes None. There is no third answer, and None
is a normal one.

Every URL here is synthetic. Tracker HOSTNAMES are real, because host matching
is the thing being tested; destination hosts use the reserved `.example` TLD so
nothing here could resolve even if the suite were allowed to touch the network,
which it is not.
"""

from __future__ import annotations

import pytest

from curator.normalize import safe_url
from curator.newsletter.sanitize import (
    is_suspect,
    is_token_like,
    is_tracker_host,
    sanitize,
    strip_tracking,
)

TOKEN = "SUB7f3a9c2b4d6e8f0a1b2c3d4e5f607182"
CLEAN = "https://www.chipdesk.example/2026/08/inference-chip"


# --------------------------------------------------------------------------
# extraction that works
# --------------------------------------------------------------------------

def test_tldr_percent_encoded_path_is_unwrapped():
    """TLDR's sendgrid-shaped link carries the destination in the path."""
    url = f"https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fwww.chipdesk.example%2Fstory/1/010001{TOKEN}/abc"
    assert sanitize(url) == "https://www.chipdesk.example/story"


def test_url_query_parameter_is_unwrapped():
    url = f"https://link.mail.beehiiv.com/ss/c/{TOKEN}?url=https%3A%2F%2Fwww.chipdesk.example%2Fstory"
    assert sanitize(url) == "https://www.chipdesk.example/story"


def test_redirect_query_parameter_is_unwrapped():
    url = f"https://click.convertkit-mail2.com/{TOKEN}?redirect=https%3A%2F%2Flab.example%2Fpapers%2Fx"
    assert sanitize(url) == "https://lab.example/papers/x"


def test_base64_payload_segment_is_unwrapped():
    import base64

    payload = base64.urlsafe_b64encode(b"https://newsroom.example/posts/siting").decode().rstrip("=")
    assert sanitize(f"https://tracking.tldrnewsletter.com/c/{payload}/{TOKEN}") == (
        "https://newsroom.example/posts/siting"
    )


def test_double_wrapped_link_unwraps_within_the_depth_bound():
    inner = "https%3A%2F%2Fwww.chipdesk.example%2Fstory"
    middle = f"https://link.mail.beehiiv.com/ss/c/{TOKEN}?url={inner}"
    from urllib.parse import quote

    outer = f"https://tracking.tldrnewsletter.com/CL0/{quote(middle, safe='')}/1/{TOKEN}"
    assert sanitize(outer) == "https://www.chipdesk.example/story"


# --------------------------------------------------------------------------
# extraction that must give up
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        f"https://link.mail.beehiiv.com/ss/c/{TOKEN}/aGVhZGVy/01",
        f"https://tracking.tldrnewsletter.com/CL0/{TOKEN}/1/0100019abcdef",
        f"https://links.tldr.tech/click/{TOKEN}",
        f"https://email.mg2.substack.com/c/{TOKEN}/story",
        f"https://substack.com/redirect/{TOKEN}?j=eyJ1IjoiYWJjIn0",
        f"https://example.us17.list-manage.com/track/click?u=abc123&id=9f8e7d&e={TOKEN}",
        f"https://url1234.ct.sendgrid.net/ls/click?upn={TOKEN}",
        f"https://click.convertkit-mail2.com/{TOKEN}/e0u8h7h8k0d2c3",
        f"https://link.mail.example.com/track/{TOKEN}",
    ],
)
def test_opaque_tracker_links_return_none(url):
    assert sanitize(url) is None, "an unresolvable tracker link must drop, never leak"


def test_mailchimp_u_parameter_is_not_mistaken_for_a_destination():
    """`u=` on Mailchimp is the ACCOUNT id, not a URL. It must be ignored."""
    url = f"https://example.us17.list-manage.com/track/click?u=abc123def456&id=9f8e&e={TOKEN}"
    assert sanitize(url) is None


def test_non_http_schemes_are_refused():
    assert sanitize("javascript:alert(1)") is None
    assert sanitize("data:text/html;base64,PHNjcmlwdD4=") is None
    assert sanitize("") is None


# --------------------------------------------------------------------------
# stripping and passthrough
# --------------------------------------------------------------------------

def test_clean_publisher_url_passes_through_unchanged():
    assert sanitize(CLEAN) == CLEAN


def test_tracking_parameters_are_stripped():
    url = f"{CLEAN}?utm_source=milkroad&utm_campaign=daily&e={TOKEN}&ck_subscriber_id=99"
    assert sanitize(url) == CLEAN


def test_meaningful_parameters_survive_stripping():
    assert strip_tracking("https://lab.example/search?q=agents&page=2") == (
        "https://lab.example/search?q=agents&page=2"
    )


def test_fragment_is_dropped():
    assert sanitize(f"{CLEAN}#section-two") == CLEAN


# --------------------------------------------------------------------------
# the predicate the privacy test leans on
# --------------------------------------------------------------------------

def test_is_suspect_flags_tracker_hosts_and_tokens():
    assert is_suspect(f"https://link.mail.beehiiv.com/ss/c/{TOKEN}")
    assert is_suspect(f"https://www.chipdesk.example/story?e={TOKEN}")
    assert is_suspect("https://www.chipdesk.example/r/9f8e7d6c5b4a39281706f5e4d3c2b1a0")
    assert is_suspect("not a url")


def test_is_suspect_accepts_an_ordinary_article_url():
    assert not is_suspect(CLEAN)
    assert not is_suspect("https://newsroom.example/2026/08/28/the-rise-of-small-language-models")


def test_token_heuristic_separates_slugs_from_identifiers():
    assert not is_token_like("the-rise-of-small-language-models")
    assert not is_token_like("short")
    assert is_token_like("9f8e7d6c5b4a39281706f5e4d3c2b1a0")
    assert is_token_like("eyJhbGciOiJIUzI1NiJ9")
    assert is_token_like(TOKEN)


def test_tracker_host_recognition():
    assert is_tracker_host("https://link.mail.beehiiv.com/x")
    assert is_tracker_host("https://substack.com/redirect/abc")
    assert not is_tracker_host(CLEAN)


def test_every_returned_url_passes_safe_url():
    candidates = [
        CLEAN,
        f"{CLEAN}?utm_source=x",
        f"https://tracking.tldrnewsletter.com/CL0/https:%2F%2Flab.example%2Fp/1/{TOKEN}",
        f"https://link.mail.beehiiv.com/ss/c/{TOKEN}/x/01",
    ]
    for candidate in candidates:
        out = sanitize(candidate)
        if out is not None:
            assert safe_url(out) == out
