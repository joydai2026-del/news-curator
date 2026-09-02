"""Reading the newsletter mailbox, over plain HTTPS, with nothing new installed.

**Why no google-api-python-client.** The design doc expected a pinned MIME/Gmail
dependency because the repo had none. It turned out not to be needed: Gmail's
REST v1 is three ordinary HTTPS calls (mint a token, list message ids, get one
message as raw bytes) and the standard library's `email` package is a complete
MIME parser. `requests` is already a dependency. So the whole lane adds ZERO
new install surface to a public repo that a fork has to audit, which is worth
more than the convenience of a client library.

**Auth.** A refresh token from a PUBLISHED OAuth app, so it does not expire
after seven days the way a Testing-mode token does. The three secrets arrive
only through the environment:

    GMAIL_CLIENT_ID  GMAIL_CLIENT_SECRET  GMAIL_REFRESH_TOKEN

Scope needed: https://www.googleapis.com/auth/gmail.readonly

**Revocation is a status, never an exception.** If JJ revokes access, or the
token is invalid, Google answers `invalid_grant`. That must darken this lane
and leave a visible warning, not fail the hourly build of a page whose category
tabs are fine. Every entry point here returns a `GmailResult` carrying a
machine-readable `reason`; nothing escapes as a raised exception.

**LOGGING RULE (module-wide, no exceptions).** This module may log COUNTS,
adapter id slugs, and exception CLASS NAMES. It may never log an email address,
a subject, a URL, a message id, a token, or a response body. Response bodies
from an auth endpoint routinely contain the token; exception payloads from
`requests` routinely contain the URL. So error handling reads
`type(exc).__name__` and stops there. If you add a log line here, it goes
through that rule first.
"""

from __future__ import annotations

import base64
import binascii
import email
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message

import requests

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

ENV_CLIENT_ID = "GMAIL_CLIENT_ID"
ENV_CLIENT_SECRET = "GMAIL_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "GMAIL_REFRESH_TOKEN"
REQUIRED_ENV = (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN)

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_MESSAGES = 30
# How many message IDS one run will list before it stops asking for pages.
# Listing is cheap (ids only, 100 per page); fetching BODIES is what costs, and
# that is capped separately by `limit`. The two are different numbers because
# they answer different questions: the budget bounds "how much of the window do
# we know about", the limit bounds "how much of it do we read this run".
DEFAULT_ID_BUDGET = 500
_PAGE_SIZE = 100

# Machine-readable statuses. The pipeline turns these into page/workflow text;
# they are never assembled from an exception string.
OK = "ok"
MISSING_CREDENTIALS = "missing_credentials"
AUTH_REVOKED = "auth_revoked"
AUTH_FAILED = "auth_failed"
API_ERROR = "api_error"
NETWORK_ERROR = "network_error"
NO_SENDERS = "no_senders"

REASON_TEXT = {
    OK: "newsletter mailbox read",
    MISSING_CREDENTIALS: "Gmail credentials not configured",
    AUTH_REVOKED: "Gmail refresh token revoked or invalid; re-authorize the newsletter account",
    AUTH_FAILED: "Gmail authorization failed",
    API_ERROR: "Gmail API returned an error",
    NETWORK_ERROR: "Gmail API unreachable",
    NO_SENDERS: "no newsletter senders configured",
}


@dataclass
class GmailResult:
    """What one mailbox read produced, including how it degraded.

    `listed` used to be the whole story of degradation, and nothing read it.
    Round 1 proved the consequence: 40 messages waiting, 30 fetched, watermark
    advanced to now, ten messages permanently outside the next window. So the
    two ways a batch can be short of the mailbox are now named fields, and
    `complete` is the single question the lane asks before advancing a cursor.

    What each field means after round 2's pagination change:
      `listed`     ids the whole window held, up to the id budget.
      `truncated`  the run read fewer BODIES than there were ids (a backlog
                   remains, and it is the NEWEST part of the window because
                   bodies are read oldest-first), or the id budget itself ran
                   out before the listing was drained.
      `fetch_failures`  ids that were listed and then could not be read.
    `truncated` is now normal-and-recoverable rather than lossy: the unread
    remainder is newer than everything processed, so the caller's watermark
    leaves it inside the next window. It is still reported, because a backlog
    that never shrinks is a thing JJ should be able to see.
    """

    ok: bool
    reason: str
    messages: list[Message] = field(default_factory=list)
    listed: int = 0  # ids Gmail offered, before the per-run cap
    truncated: bool = False  # more mail matched the query than this run took
    fetch_failures: int = 0  # ids that were listed and then could not be read

    @property
    def note(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)

    @property
    def complete(self) -> bool:
        """Every message that matched the query is in `messages`.

        The ONLY condition under which the caller may treat the window as
        fully consumed and move its watermark to the wall clock.
        """
        return not self.truncated and self.fetch_failures == 0


def has_credentials(env: dict | None = None) -> bool:
    """All three secrets present. Presence only; never their values."""
    source = os.environ if env is None else env
    return all(str(source.get(name) or "").strip() for name in REQUIRED_ENV)


def build_query(senders: list[str], after: datetime) -> str:
    """Gmail search: newer than the window start, from an allowlisted sender.

    Sender addresses are the newsletters' PUBLIC sending addresses, which is why
    they can live in config. The subscriber's own address never appears.
    """
    epoch = int(after.astimezone(timezone.utc).timestamp())
    cleaned = [s.strip() for s in senders if s and s.strip()]
    if not cleaned:
        return f"after:{epoch}"
    froms = " OR ".join(f"from:{s}" for s in cleaned)
    return f"after:{epoch} ({froms})"


def _request(session: requests.Session, method: str, url: str, *, timeout: float, **kwargs):
    """One HTTP call with exactly one retry, and no leaky logging.

    Retrying more than once against someone else's API on an hourly schedule
    buys nothing: the next run is an hour away and will try again anyway.
    """
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last = exc
            log.warning("gmail request failed (%s), attempt %d", type(exc).__name__, attempt)
    raise _Unavailable(NETWORK_ERROR) from last


class _Unavailable(Exception):
    """Internal. Carries a status slug; never crosses this module's boundary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _access_token(session: requests.Session, env, timeout: float) -> str:
    response = _request(
        session,
        "POST",
        TOKEN_URL,
        timeout=timeout,
        data={
            "client_id": str(env.get(ENV_CLIENT_ID) or "").strip(),
            "client_secret": str(env.get(ENV_CLIENT_SECRET) or "").strip(),
            "refresh_token": str(env.get(ENV_REFRESH_TOKEN) or "").strip(),
            "grant_type": "refresh_token",
        },
    )
    if response.status_code == 200:
        try:
            token = str((response.json() or {}).get("access_token") or "")
        except ValueError:
            raise _Unavailable(AUTH_FAILED) from None
        if not token:
            raise _Unavailable(AUTH_FAILED)
        return token

    # `invalid_grant` is the revoked/expired case and the one JJ has to act on.
    # Reading the body for that ONE word is safe; the body is never logged.
    revoked = False
    try:
        revoked = str((response.json() or {}).get("error") or "").lower() == "invalid_grant"
    except ValueError:
        revoked = False
    log.warning("gmail token endpoint returned %d", response.status_code)
    raise _Unavailable(AUTH_REVOKED if revoked else AUTH_FAILED)


def _list_message_ids(
    session, token: str, query: str, timeout: float, *, id_budget: int = DEFAULT_ID_BUDGET
) -> tuple[list[str], bool]:
    """Every id in this window, newest first, plus "the budget cut it short".

    Gmail answers a page at a time and hands back a `nextPageToken` when the
    query matched more than the page. This function FOLLOWS that token until
    the window is drained or `id_budget` ids have been collected, and that is
    what makes the no-skip contract real: the caller cannot decide which mail
    to read next when it only knows about the newest page of it.

    The earlier version deliberately did not paginate and returned the token as
    an honest "this run saw a prefix" signal. Round 2 measured what the honesty
    was worth: the prefix was the NEWEST 30, the watermark moved past it, and
    the older tail fell permanently outside the next window. Listing is the
    cheap half of the API (ids only), so the bound moved from one page to a
    budget, and the second return value now means "even the budget did not
    drain the window", which is a genuinely rare state rather than the norm.
    """
    ids: list[str] = []
    page_token: str | None = None
    budget = max(1, int(id_budget))
    while True:
        params = {"q": query, "maxResults": _PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        response = _request(
            session,
            "GET",
            f"{API_ROOT}/messages",
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if response.status_code in (401, 403):
            raise _Unavailable(AUTH_REVOKED)
        if response.status_code != 200:
            log.warning("gmail list returned %d", response.status_code)
            raise _Unavailable(API_ERROR)
        try:
            payload = response.json() or {}
        except ValueError:
            raise _Unavailable(API_ERROR) from None
        rows = payload.get("messages")
        if isinstance(rows, list):
            ids += [
                str(row.get("id")) for row in rows
                if isinstance(row, dict) and row.get("id")
            ]
        page_token = str(payload.get("nextPageToken") or "") or None
        if page_token is None:
            return ids[:budget], len(ids) > budget
        if len(ids) >= budget:
            # The budget ran out with pages still to go. This is the only
            # remaining shape of "the window was not drained", and it is
            # reported rather than hidden.
            return ids[:budget], True


def _get_message(session, token: str, message_id: str, timeout: float) -> Message | None:
    response = _request(
        session,
        "GET",
        f"{API_ROOT}/messages/{message_id}",
        timeout=timeout,
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "raw"},
    )
    if response.status_code in (401, 403):
        raise _Unavailable(AUTH_REVOKED)
    if response.status_code != 200:
        log.warning("gmail message fetch returned %d", response.status_code)
        return None
    try:
        raw = str((response.json() or {}).get("raw") or "")
    except ValueError:
        return None
    return decode_raw(raw)


def decode_raw(raw: str) -> Message | None:
    """base64url payload -> a parsed MIME message, or None if it is not one."""
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    try:
        return email.message_from_bytes(blob)
    except Exception as exc:  # a malformed message is data, not a crash
        log.warning("gmail message unparseable (%s)", type(exc).__name__)
        return None


def fetch(
    senders: list[str],
    after: datetime,
    *,
    env: dict | None = None,
    session: requests.Session | None = None,
    limit: int = DEFAULT_MAX_MESSAGES,
    timeout: float = DEFAULT_TIMEOUT,
    id_budget: int = DEFAULT_ID_BUDGET,
) -> GmailResult:
    """Read the OLDEST `limit` messages from allowlisted senders since `after`.

    Oldest, not newest, and that word is the whole no-skip mechanism. Gmail
    lists newest first; reading the newest `limit` and then moving the cursor
    to `now` is precisely how the older tail got skipped in round 1 and round
    2. Reading the OLDEST `limit` instead means every message this run did not
    read is NEWER than every message it did, so the caller can park its
    watermark on the newest message it processed and know the remainder is
    still inside the next window. The backlog drains a runful per run instead
    of being lost.

    Never raises. Every failure comes back as `ok=False` plus a status slug the
    pipeline can render as a visible warning.
    """
    source = os.environ if env is None else env
    if not has_credentials(source):
        return GmailResult(ok=False, reason=MISSING_CREDENTIALS)
    if not [s for s in senders if s and s.strip()]:
        return GmailResult(ok=False, reason=NO_SENDERS)

    owned = session is None
    client = session or requests.Session()
    try:
        token = _access_token(client, source, timeout)
        ids, budget_hit = _list_message_ids(
            client, token, build_query(senders, after), timeout, id_budget=id_budget
        )
        # Gmail lists newest first. Reverse it, then take from the front: the
        # oldest `limit` ids, which is the backlog-draining order. The messages
        # come back in the same oldest-first order, which the lane does not
        # depend on (it reads the Date header) but which makes a log or a
        # debugger read the way the run actually happened.
        oldest_first = list(reversed(ids))
        taken = oldest_first[: max(0, int(limit))]
        messages: list[Message] = []
        for message_id in taken:
            parsed = _get_message(client, token, message_id, timeout)
            if parsed is not None:
                messages.append(parsed)
        truncated = budget_hit or len(taken) < len(ids)
        failures = len(taken) - len(messages)
        log.info(
            "gmail: listed %d messages, parsed %d, truncated=%s, unreadable=%d",
            len(ids), len(messages), truncated, failures,
        )
        return GmailResult(
            ok=True, reason=OK, messages=messages, listed=len(ids),
            truncated=truncated, fetch_failures=failures,
        )
    except _Unavailable as exc:
        log.warning("gmail lane unavailable: %s", exc.reason)
        return GmailResult(ok=False, reason=exc.reason)
    except Exception as exc:
        # A bug here darkens one lane. It does not take the hourly build down.
        log.warning("gmail lane raised (%s)", type(exc).__name__)
        return GmailResult(ok=False, reason=API_ERROR)
    finally:
        if owned:
            client.close()
