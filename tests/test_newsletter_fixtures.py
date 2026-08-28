"""Shared fixture machinery for the newsletter lane, plus its own sanity checks.

Every message the newsletter tests parse is built HERE, from the synthetic HTML
in `tests/fixtures/newsletter/`. The HTML is stored as plain files so a human
can read and review it; the MIME envelope is assembled at test time with
`email.message.EmailMessage` so that the four MIME realities the design doc
names (multipart/alternative, HTML-only, base64 transfer encoding, multipart
nested inside multipart) are REAL messages that go through a real
`message_from_bytes` round trip rather than hand-typed encodings that drift.

Nothing in this folder is real: destination hosts use the reserved `.example`
TLD, reader addresses use `.invalid`, and the subscriber tokens are the literal
strings in `FAKE_TOKENS`, which the privacy test hunts for by value.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "newsletter"

# The fake subscriber identifiers baked into each fixture's tracking links.
# The privacy test asserts none of these strings survives into an item URL,
# the state file, or a log record.
FAKE_TOKENS = {
    "tldr": "SUB7f3a9c2b4d6e8f0a1b2c3d4e5f607182",
    "therundown": "SUBb41c8e2d9a7f6053e1d4c7b0a9f83e26",
    "bensbites": "SUBc93d17ea4b8f2605c1d9e7a3b5f04182",
    "theneuron": "SUBd5e1a7c94b3f8206e0d7c4b1a9f52e83",
    "milkroad": "SUBe2f8b6d04a19c73e5d8b0a2c4f61e739",
}

FAKE_READER = "fixture-reader@example.invalid"

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
SHAPES = {
    "tldr": "alternative",
    "therundown": "html_only",
    "bensbites": "base64",
    "theneuron": "nested",
    "milkroad": "html_only",
}

SENT = datetime(2026, 8, 28, 7, 30, 0, tzinfo=timezone.utc)


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def build_message(
    name: str,
    *,
    shape: str | None = None,
    sent: datetime = SENT,
    sender: str | None = None,
    html: str | None = None,
) -> EmailMessage:
    """One synthetic newsletter as a real MIME message.

    `shape` picks the MIME reality under test:
      alternative  text/plain + text/html (the common case)
      html_only    a single text/html part
      base64       text/html with base64 transfer encoding
      nested       multipart/mixed > multipart/alternative, plus an attachment
                   that must NOT be mistaken for the newsletter body
    """
    shape = shape or SHAPES.get(name, "alternative")
    body = load_html(name) if html is None else html

    msg = EmailMessage()
    msg["From"] = sender or SENDERS[name]
    msg["To"] = FAKE_READER
    msg["Subject"] = SUBJECTS.get(name, "Newsletter")
    msg["Date"] = format_datetime(sent)

    if shape == "html_only":
        msg.set_content(body, subtype="html")
    elif shape == "base64":
        msg.set_content(body, subtype="html", cte="base64")
    elif shape == "nested":
        msg.set_content("Plain-text alternative for the nested fixture.")
        msg.add_alternative(body, subtype="html")
        msg.make_mixed()
        msg.add_attachment(
            b"<html><body>NOT THE NEWSLETTER</body></html>",
            maintype="text",
            subtype="html",
            filename="attachment.html",
        )
    else:
        msg.set_content("Plain-text alternative. The HTML part is the real one.")
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
