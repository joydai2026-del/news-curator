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


def test_a_single_unfetchable_message_does_not_lose_the_others_and_the_loss_is_reported():
    """Round 1: the old version of this test encoded the loss as correct.

    Keeping the readable message is right. Returning `ok=True` and saying
    nothing about the one that vanished is what let the caller advance a cursor
    past mail it never read.
    """
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}, {"id": "gone"}]}),
        messages={"m1": raw_response("tldr")},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.ok and len(result.messages) == 1 and result.listed == 2
    assert result.fetch_failures == 1, "the message that could not be read must be counted"
    assert not result.complete, "a batch missing a message is not a complete batch"


def test_a_clean_full_batch_reports_itself_complete():
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}]}),
        messages={"m1": raw_response("tldr")},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.complete and not result.truncated and result.fetch_failures == 0


def test_more_mail_than_the_cap_is_reported_as_truncated():
    """The M4 shape: 40 waiting, 30 taken, and nobody told."""
    listing = FakeResponse(200, {"messages": [{"id": f"m{i}"} for i in range(40)]})
    session = FakeSession(listing=listing, messages={f"m{i}": raw_response("tldr") for i in range(40)})
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=30)
    assert len(result.messages) == 30 and result.listed == 40
    assert result.truncated and not result.complete


def test_a_next_page_token_is_truncation_even_when_the_page_fits():
    """Gmail says "there is more" and this run is not going to go get it."""
    listing = FakeResponse(200, {"messages": [{"id": "m1"}], "nextPageToken": "more"})
    session = FakeSession(listing=listing, messages={"m1": raw_response("tldr")})
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=30)
    assert result.truncated and not result.complete


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_messages_come_back_parsed_oldest_first():
    """Gmail lists newest first; this client reads the OTHER end of the list.

    The order is not cosmetic. It is the mechanism behind the no-skip contract:
    the messages this run did not read are the NEWEST ones, so the lane's
    watermark (the newest message it processed) leaves them inside the next
    window instead of behind it.
    """
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": "m1"}, {"id": "m2"}]}),
        messages={"m1": raw_response("tldr"), "m2": raw_response("milkroad")},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session)
    assert result.ok and result.reason == gmail.OK
    senders = [m.get("From") for m in result.messages]
    assert SENDERS["milkroad"] in senders[0], "the OLDEST listed id is read first"
    assert SENDERS["tldr"] in senders[1]


def test_the_per_run_message_cap_is_honoured_and_says_it_capped():
    listing = FakeResponse(200, {"messages": [{"id": f"m{i}"} for i in range(10)]})
    session = FakeSession(listing=listing, messages={f"m{i}": raw_response("tldr") for i in range(10)})
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=3)
    assert len(result.messages) == 3
    assert result.truncated


def test_the_cap_takes_the_oldest_ids_not_the_newest():
    """The reviewer's 40-against-30 repro, at the id level.

    Gmail returns m0 (newest) .. m39 (oldest). A run capped at 30 must fetch
    m39..m10, leaving the ten NEWEST unread. The old code fetched m0..m29 and
    left the ten oldest unread, which the watermark then moved past.
    """
    ids = [f"m{i}" for i in range(40)]
    session = FakeSession(
        listing=FakeResponse(200, {"messages": [{"id": i} for i in ids]}),
        messages={i: raw_response("tldr") for i in ids},
    )
    gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=30)
    fetched = [url.rsplit("/", 1)[-1] for _method, url in session.calls if "/messages/" in url]
    assert fetched == list(reversed(ids))[:30]
    assert set(fetched) == {f"m{i}" for i in range(10, 40)}
    assert not ({"m0", "m9"} & set(fetched)), "the newest ten are the ones deferred"


# --------------------------------------------------------------------------
# pagination (round 2, R2-2)
# --------------------------------------------------------------------------

class PagingSession(FakeSession):
    """A listing endpoint that answers in pages, like the real one."""

    def __init__(self, pages, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages
        self.page_tokens: list[str | None] = []

    def request(self, method, url, timeout=None, **kwargs):
        if url.endswith("/messages"):
            self.calls.append((method, url))
            token = (kwargs.get("params") or {}).get("pageToken")
            self.page_tokens.append(token)
            index = 0 if token is None else int(token)
            rows, nxt = self.pages[index]
            payload = {"messages": [{"id": i} for i in rows]}
            if nxt is not None:
                payload["nextPageToken"] = str(nxt)
            return FakeResponse(200, payload)
        return super().request(method, url, timeout=timeout, **kwargs)


def test_the_listing_follows_next_page_tokens_until_the_window_is_drained():
    ids_a = [f"a{i}" for i in range(5)]
    ids_b = [f"b{i}" for i in range(4)]
    session = PagingSession(
        pages=[(ids_a, 1), (ids_b, None)],
        messages={i: raw_response("tldr") for i in ids_a + ids_b},
    )
    result = gmail.fetch(["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=30)
    assert session.page_tokens == [None, "1"], "the second page must be requested"
    assert result.listed == 9
    assert len(result.messages) == 9
    assert not result.truncated, "a fully drained window is not truncated"


def test_the_id_budget_bounds_the_listing_and_reports_the_shortfall():
    pages = [([f"p{p}i{i}" for i in range(5)], p + 1) for p in range(10)]
    pages[-1] = (pages[-1][0], None)
    every_id = [i for rows, _ in pages for i in rows]
    session = PagingSession(
        pages=pages, messages={i: raw_response("tldr") for i in every_id}
    )
    result = gmail.fetch(
        ["tldrnewsletter.com"], WINDOW, env=ENV, session=session, limit=30, id_budget=12,
    )
    assert result.listed == 12, "the budget cuts the listing, it does not silently drain it"
    assert result.truncated, "a budget-bounded listing must report that it was short"
    assert len(session.page_tokens) == 3, "one page past the budget, then stop"


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
