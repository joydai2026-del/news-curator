"""The Gmail client: statuses instead of exceptions, and a logging rule.

Nothing here touches the network (`tests/conftest.py` blocks it). Every call
goes through a fake session so the failure modes that matter, a revoked refresh
token above all, can be exercised deterministically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
import requests

from curator.newsletter import gmail
from tests.test_newsletter_fixtures import (
    FAKE_READER,
    FAKE_TOKENS,
    SENDERS,
    SUBJECTS,
    as_raw,
    build_message,
)

WINDOW = datetime(2026, 8, 28, 6, 0, 0, tzinfo=timezone.utc)

ENV = {
    "GMAIL_CLIENT_ID": "fixture-client-id.apps.googleusercontent.invalid",
    "GMAIL_CLIENT_SECRET": "fixture-client-secret",
    "GMAIL_REFRESH_TOKEN": "fixture-refresh-token",
}


class FakeResponse:
    def __init__(self, status_code: int, payload=None, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Answers by URL prefix and records what was asked, never why."""

    def __init__(self, token=None, listing=None, messages=None, raise_on=None):
        self.token = token if token is not None else FakeResponse(200, {"access_token": "fixture-access"})
        self.listing = listing if listing is not None else FakeResponse(200, {"messages": []})
        self.messages = messages or {}
        self.raise_on = raise_on or ()
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url))
        for marker in self.raise_on:
            if marker in url:
                raise requests.ConnectionError("fixture connection failure")
        if url.startswith(gmail.TOKEN_URL):
            return self.token
        if url.endswith("/messages"):
            return self.listing
        message_id = url.rsplit("/", 1)[-1]
        return self.messages.get(message_id, FakeResponse(404, {}))

    def close(self):
        self.closed = True


def raw_response(name: str) -> FakeResponse:
    return FakeResponse(200, {"raw": as_raw(build_message(name))})


# --------------------------------------------------------------------------
# credentials and query
# --------------------------------------------------------------------------

def test_has_credentials_requires_all_three():
    assert gmail.has_credentials(ENV)
    for missing in gmail.REQUIRED_ENV:
        partial = dict(ENV)
        partial[missing] = ""
        assert not gmail.has_credentials(partial)


def test_build_query_uses_an_epoch_and_the_sender_allowlist():
    query = gmail.build_query(["tldrnewsletter.com", "milkroad.com"], WINDOW)
    assert query.startswith(f"after:{int(WINDOW.timestamp())}")
    assert "from:tldrnewsletter.com OR from:milkroad.com" in query


def test_the_declared_scope_is_read_only():
    assert gmail.SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------

def test_missing_credentials_is_a_status_not_a_call():
    session = FakeSession()
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env={}, session=session)
    assert (result.ok, result.reason) == (False, gmail.MISSING_CREDENTIALS)
    assert session.calls == [], "no request may be made before the credential check"


def test_no_senders_is_a_status():
    result = gmail.fetch([], WINDOW, env=ENV, session=FakeSession())
    assert result.reason == gmail.NO_SENDERS


def test_revoked_refresh_token_darkens_the_lane_without_raising():
    session = FakeSession(token=FakeResponse(400, {"error": "invalid_grant"}))
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert (result.ok, result.reason) == (False, gmail.AUTH_REVOKED)
    assert "re-authorize" in result.note


def test_other_auth_failures_are_distinguished_from_revocation():
    session = FakeSession(token=FakeResponse(500, {"error": "backend_error"}))
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.reason == gmail.AUTH_FAILED


def test_a_403_on_listing_reads_as_revoked_access():
    session = FakeSession(listing=FakeResponse(403, {}))
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.reason == gmail.AUTH_REVOKED


def test_an_api_error_on_listing_is_reported_not_raised():
    session = FakeSession(listing=FakeResponse(503, {}))
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert (result.ok, result.reason) == (False, gmail.API_ERROR)


def test_a_network_failure_retries_exactly_once_then_reports():
    session = FakeSession(raise_on=(gmail.TOKEN_URL,))
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert (result.ok, result.reason) == (False, gmail.NETWORK_ERROR)
    assert len(session.calls) == 2, "one retry, and only one"


def test_a_single_unfetchable_message_does_not_lose_the_others():
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}, {"id": "gone"}]}),
        messages={"m1": raw_response("tldr")},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.ok and len(result.messages) == 1 and result.listed == 2


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_messages_come_back_parsed():
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}, {"id": "m2"}]}),
        messages={"m1": raw_response("tldr"), "m2": raw_response("milkroad")},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.ok and result.reason == gmail.OK
    senders = [m.get("From") for m in result.messages]
    assert SENDERS["tldr"] in senders[0]


def test_the_per_run_message_cap_is_honoured():
    listing = FakeResponse(200, {"messages": [{"id": f"m{i}"} for i in range(10)]})
    session = FakeSession(listing=listing, messages={f"m{i}": raw_response("tldr") for i in range(10)})
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=3)
    assert len(result.messages) == 3


def test_decode_raw_rejects_junk():
    assert gmail.decode_raw("") is None
    assert gmail.decode_raw("!!!not-base64!!!") is None


# --------------------------------------------------------------------------
# the logging rule
# --------------------------------------------------------------------------

FORBIDDEN_IN_LOGS = (
    FAKE_READER,
    SENDERS["tldr"],
    SUBJECTS["tldr"],
    FAKE_TOKENS["tldr"],
    "fixture-refresh-token",
    "fixture-access",
    "http",  # no URLs, and no message ids inside them
)


@pytest.mark.parametrize(
    "session_factory",
    [
        lambda: FakeSession(
            listing=FakeResponse(200, {"messages": [{"id": "m1"}]}),
            messages={"m1": raw_response("tldr")},
        ),
        lambda: FakeSession(token=FakeResponse(400, {"error": "invalid_grant"})),
        lambda: FakeSession(listing=FakeResponse(503, {})),
        lambda: FakeSession(raise_on=(gmail.TOKEN_URL,)),
        lambda: FakeSession(token=FakeResponse(200, {}, bad_json=True)),
    ],
)
def test_logs_carry_counts_and_class_names_only(caplog, session_factory):
    with caplog.at_level(logging.DEBUG, logger="curator.newsletter.gmail"):
        gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session_factory())
    blob = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in FORBIDDEN_IN_LOGS:
        assert forbidden not in blob, f"log leaked {forbidden!r}"
