"""Shared fixture machinery for the newsletter lane, plus its own sanity checks.

Every message the newsletter tests parse is built HERE, from the files in
`tests/fixtures/newsletter/`. The bodies are stored as plain files so a human
can read and review them; the MIME envelope is assembled at test time with
`email.message.EmailMessage` so that the four MIME realities the design doc
names (multipart/alternative, HTML-only, base64 transfer encoding, multipart
nested inside multipart) are REAL messages that go through a real
`message_from_bytes` round trip rather than hand-typed encodings that drift.

**The four live-sender fixtures are now DERIVED FROM REAL MAIL, scrubbed.**
They used to be hand-written reconstructions, and the first live run showed
what that costs: the TLDR adapter read 15 real messages and produced 0 stories,
because real TLDR mail writes `<meta ...>` without a closing slash and the
reconstruction had written `<meta ... />`. A fixture that agrees with the parser
about a format neither has seen proves nothing. So `tldr`, `therundown`,
`theneuron` and `milkroad` are now real messages with every identifier removed,
and `bensbites` stays hand-written because that sender had no mail to capture.

Scrubbing is mechanical and the tests at the bottom of this file enforce it:
no `@` outside `fixture-reader@example.invalid`, no path or query token over 20
characters that does not start with `SYNTHETIC-`, and every destination host
moved under the reserved `.example` TLD so it can never resolve. Tracker
HOSTNAMES stay real, because the sanitizer's host matching is the thing under
test and nothing is ever fetched (`tests/conftest.py` blocks the network).

Real headlines and blurbs are KEPT. They are published newsletter copy, they
carry no identifier, and they are the reason these fixtures are worth having:
the parser is now measured against sentences a person actually wrote.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "newsletter"

# The fake subscriber identifiers baked into each fixture's tracking links.
# The privacy test asserts none of these strings survives into an item URL,
# the state file, or a log record. Every one starts with `SYNTHETIC-` so the
# hygiene test below can state its rule without carve-outs.
FAKE_TOKENS = {
    "tldr": "SYNTHETIC-SUB7f3a9c2b4d6e8f0a1b2c3d4e5f607182",
    "therundown": "SYNTHETIC-SUBb41c8e2d9a7f6053e1d4c7b0a9f83e26",
    "bensbites": "SYNTHETIC-SUBc93d17ea4b8f2605c1d9e7a3b5f04182",
    "theneuron": "SYNTHETIC-SUBd5e1a7c94b3f8206e0d7c4b1a9f52e83",
    "milkroad": "SYNTHETIC-SUBe2f8b6d04a19c73e5d8b0a2c4f61e739",
}

FAKE_READER = "fixture-reader@example.invalid"

# The four fixtures captured from the live mailbox. `bensbites` is not here:
# that sender had no mail in the surveyed week, so its fixture stays a
# hand-written reconstruction and is labelled as one.
FROM_REAL_MAIL = ("tldr", "therundown", "theneuron", "milkroad")

# What each fixture yields, as measured. These are the numbers a real issue
# produces, not a target: the hand-written fixtures used to give a tidy three
# stories each, which was a property of the fixtures and of nothing else.
# They live here so the lane tests can pin the same numbers without every file
# carrying its own copy, and a change to any of them is a real change in what
# the parser gets out of a real message.
EXPECTED_STORIES = {
    "bensbites": 3,
    "milkroad": 6,
    "theneuron": 5,
    "therundown": 10,
    "tldr": 12,
}

# Of those, how many keep a publisher URL after the sanitizer. Milk Road's zero
# is correct and permanent: it writes its own copy under unlinked headings, so
# there is no destination to recover. Two of The Rundown's ten are its own
# beehiiv-hosted pages, whose links stay opaque.
EXPECTED_LINKED = {
    "bensbites": 2,
    "milkroad": 0,
    "theneuron": 5,
    "therundown": 8,
    "tldr": 12,
}

# Public sending addresses, the kind that can live in config. Not subscribers.
SENDERS = {
    "tldr": "dan@tldrnewsletter.com",
    "therundown": "news@mail.therundown.ai",
    "bensbites": "ben@mail.bensbites.co",
    "theneuron": "team@theneurondaily.com",
    "milkroad": "hello@milkroad.com",
}

SUBJECTS = {
    "tldr": "TLDR AI: inference chips and siting rules",
    "therundown": "The Rundown: agent liability guidance",
    "bensbites": "Ben's Bites: constitutional training goes open",
    "theneuron": "The Neuron: model cards in the clinic",
    "milkroad": "Milk Road: settlement windows shrink",
}

# One MIME shape per adapter, so the whole set is covered by a normal run.
# The three beehiiv senders need a real multipart/alternative, because their
# destinations live in the plain-text half of the message and nowhere else.
# `html_only` is still covered: `test_every_mime_shape_yields_the_same_stories`
# runs TLDR through all four, and TLDR's links are recoverable from HTML alone.
SHAPES = {
    "tldr": "alternative",
    "therundown": "alternative",
    "bensbites": "base64",
    "theneuron": "nested",
    "milkroad": "alternative",
}

SENT = datetime(2026, 8, 28, 7, 30, 0, tzinfo=timezone.utc)


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def load_text(name: str) -> str | None:
    """The scrubbed plain-text half of the captured message, when there is one.

    Not decoration. beehiiv's HTML hrefs are encrypted blobs, so this file is
    where the three beehiiv senders' links actually come from.
    """
    path = FIXTURES / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def auth_header(domain: str, *, verdict: str = "pass") -> str:
    """The Authentication-Results line Gmail writes on delivery.

    Shaped like the real thing on purpose: several methods in one header,
    separated by semicolons, with the DKIM verdict and its signing domain in
    the same clause. The lane requires `dkim=pass` for a domain its adapter
    allows, so this is what makes a fixture message authentic.
    """
    return (
        "mx.google.com; "
        f"dkim={verdict} header.d={domain} header.b=FAKESIG; "
        f"spf={verdict} (google.com: domain of bounce@{domain} designates "
        f"198.51.100.7 as permitted sender) smtp.mailfrom=bounce@{domain}; "
        f"dmarc={verdict} (p=REJECT sp=REJECT dis=NONE) header.from={domain}"
    )


def build_message(
    name: str,
    *,
    shape: str | None = None,
    sent: datetime = SENT,
    sender: str | None = None,
    html: str | None = None,
    dkim_domain: str | None = None,
    dkim_verdict: str = "pass",
    authenticated: bool = True,
) -> EmailMessage:
    """One synthetic newsletter as a real MIME message.

    `shape` picks the MIME reality under test:
      alternative  text/plain + text/html (the common case)
      html_only    a single text/html part
      base64       text/html with base64 transfer encoding
      nested       multipart/mixed > multipart/alternative, plus an attachment
                   that must NOT be mistaken for the newsletter body

    `authenticated=False` builds a message with NO Authentication-Results
    header (the fail-closed case), and `dkim_domain` / `dkim_verdict` build one
    that is present and does not authorise the sender.
    """
    shape = shape or SHAPES.get(name, "alternative")
    body = load_html(name) if html is None else html
    plain = load_text(name) or "Plain-text alternative. The HTML part is the real one."
    from_address = sender or SENDERS[name]

    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = FAKE_READER
    msg["Subject"] = SUBJECTS.get(name, "Newsletter")
    msg["Date"] = format_datetime(sent)
    if authenticated:
        signing = dkim_domain or from_address.rpartition("@")[2]
        msg["Authentication-Results"] = auth_header(signing, verdict=dkim_verdict)

    if shape == "html_only":
        msg.set_content(body, subtype="html")
    elif shape == "base64":
        msg.set_content(body, subtype="html", cte="base64")
    elif shape == "nested":
        msg.set_content(plain)
        msg.add_alternative(body, subtype="html")
        msg.make_mixed()
        msg.add_attachment(
            b"<html><body>NOT THE NEWSLETTER</body></html>",
            maintype="text",
            subtype="html",
            filename="attachment.html",
        )
    else:
        msg.set_content(plain)
        msg.add_alternative(body, subtype="html")
    return msg


def as_raw(msg: EmailMessage) -> str:
    """The base64url blob Gmail's `format=raw` would hand back."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")


def parsed(name: str, **kwargs):
    """A fixture message after a real serialize/parse round trip."""
    return message_from_bytes(build_message(name, **kwargs).as_bytes())


def all_messages():
    return [parsed(name) for name in SENDERS]


def field(item, name):
    """Read a lane item whether it came back as an `Item` or as a dict.

    The shared model gains its newsletter fields in a parallel change, so the
    lane emits dicts until it does. These tests assert on the CONTENT either
    way rather than on which of the two shapes arrived.
    """
    if isinstance(item, dict):
        return item[name]
    return getattr(item, name)


# --------------------------------------------------------------------------
# the fixtures' own sanity checks
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SENDERS))
def test_fixture_is_marked_synthetic(name):
    html = load_html(name)
    assert "SYNTHETIC FIXTURE" in html
    assert FAKE_TOKENS[name] in html, "the fixture must carry the token the privacy test hunts for"


# --------------------------------------------------------------------------
# fixture hygiene: nothing real may survive a capture
# --------------------------------------------------------------------------

# Every file the scrubber writes, plus the two hand-written ones. `leakshapes`
# is excluded by name and by design: it is the deliberate carrier for the five
# link shapes review round 1 proved leaked, so it MUST contain an address in a
# query, an address in a path segment, and three token-shaped parameters. The
# end-to-end privacy test asserts those strings are present before asserting
# they are gone from the rendered page, and every one of them is synthetic.
HYGIENE_FILES = sorted(
    p for p in FIXTURES.iterdir()
    if p.suffix in (".html", ".txt") and p.name != "leakshapes.html"
)

_URL_IN_FIXTURE = re.compile(r'https?://[^\s<>()\[\]"\']+')


def _long_tokens(blob: str):
    """Every path segment and query value over 20 characters, with its file."""
    for match in _URL_IN_FIXTURE.finditer(blob):
        parts = urlsplit(match.group())
        values = [seg for seg in (parts.path or "").split("/") if seg]
        values += [value for _, value in parse_qsl(parts.query, keep_blank_values=True)]
        for value in values:
            if len(value) > 20:
                yield value


@pytest.mark.parametrize("path", HYGIENE_FILES, ids=lambda p: p.name)
def test_no_fixture_carries_an_address_other_than_the_fake_reader(path):
    """The literal rule: the only `@` in a fixture is the fake reader's.

    Absolute on purpose. Real newsletter copy is full of `@handles` and
    `plugin@package` strings that are not addresses at all, and deciding
    case-by-case which `@` is safe is exactly the judgement call that lets a
    real subscriber address through. The scrubber rewrites every one of them,
    so the test can simply count.
    """
    blob = path.read_text(encoding="utf-8")
    assert blob.count("@") == blob.count(FAKE_READER), (
        f"{path.name} carries an `@` that is not part of {FAKE_READER}"
    )


@pytest.mark.parametrize("path", HYGIENE_FILES, ids=lambda p: p.name)
def test_no_fixture_carries_an_unscrubbed_token(path):
    """Long URL components are either labelled synthetic or a reserved-TLD URL.

    Two escapes, both narrow. A percent-encoded destination is how TLDR's
    tracker carries the article, so it has to stay a real URL shape; it is
    required to point somewhere that can never resolve. And the fake reader
    address is longer than 20 characters and appears as a path segment in the
    Ben's Bites unsubscribe link, which is the shape the sanitizer must refuse.
    """
    for value in _long_tokens(path.read_text(encoding="utf-8")):
        if value.startswith("SYNTHETIC"):
            continue
        if value == FAKE_READER:
            continue
        decoded = unquote(value)
        if decoded.lower().startswith(("http://", "https://")):
            host = urlsplit(decoded).hostname or ""
            assert host.endswith((".example", ".invalid")), (
                f"{path.name}: nested destination {host!r} is not a reserved test host"
            )
            continue
        raise AssertionError(f"{path.name}: unscrubbed token {value[:40]!r}")


@pytest.mark.parametrize("name", FROM_REAL_MAIL)
def test_a_captured_fixture_keeps_its_plain_text_half(name):
    """The capture is a whole message, not just its HTML.

    This is the file the three beehiiv adapters read their links out of, so a
    capture that dropped it would quietly take the link rate back to zero.
    """
    plain = load_text(name)
    assert plain and len(plain) > 500
    assert "SYNTHETIC FIXTURE" in plain


@pytest.mark.parametrize("name", sorted(SENDERS))
def test_every_shape_survives_a_real_mime_round_trip(name):
    from curator.newsletter.adapters import html_body

    msg = parsed(name)
    body = html_body(msg)
    assert "SYNTHETIC FIXTURE" in body
    assert "NOT THE NEWSLETTER" not in body, "an attachment was mistaken for the body"


def test_nested_shape_is_actually_nested():
    msg = parsed("theneuron")
    assert msg.get_content_type() == "multipart/mixed"
    inner = [p.get_content_type() for p in msg.walk()]
    assert "multipart/alternative" in inner


def test_base64_shape_is_actually_base64():
    msg = parsed("bensbites")
    encodings = {str(p.get("Content-Transfer-Encoding") or "").lower() for p in msg.walk()}
    assert "base64" in encodings
