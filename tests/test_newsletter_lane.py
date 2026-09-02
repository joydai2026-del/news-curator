"""The lane end to end: the flag, the statuses, the cursor, the caps.

The Gmail HTTP layer is replaced wholesale by `FakeGmail`, so these tests
exercise routing, extraction, dedup and reporting without a socket in sight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.newsletter import gmail, lane, state as state_module
from tests.test_newsletter_fixtures import (
    EXPECTED_STORIES,
    SENDERS,
    build_message,
    field,
    parsed,
)

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

ENV = {
    "GMAIL_CLIENT_ID": "fixture-client-id",
    "GMAIL_CLIENT_SECRET": "fixture-client-secret",
    "GMAIL_REFRESH_TOKEN": "fixture-refresh-token",
}

CFG = {"enabled": True, "max_items": 50, "max_age_hours": 48, "max_messages": 30}


class FakeGmail:
    """Stands in for the module, not for a session. Records what it was asked."""

    def __init__(self, messages=None, *, ok=True, reason=gmail.OK, credentials=True,
                 truncated=False, fetch_failures=0):
        found = list(messages or [])
        self.result = gmail.GmailResult(
            ok=ok, reason=reason, messages=found,
            listed=len(found) + fetch_failures,
            truncated=truncated, fetch_failures=fetch_failures,
        )
        self.credentials = credentials
        self.calls: list[tuple[list[str], datetime, int]] = []
        self.budgets: list[int] = []

    def has_credentials(self, env):
        return self.credentials

    def fetch(self, senders, after, *, env=None, limit=30, timeout=20.0,
              id_budget=gmail.DEFAULT_ID_BUDGET):
        self.calls.append((list(senders), after, limit))
        self.budgets.append(id_budget)
        return self.result


def fresh_state():
    return state_module.NewsletterState(watermark=NOW - timedelta(hours=6), salt="fixture-salt")


def run(messages=None, *, cfg=None, st=None, client=None, flag=None, env=ENV):
    return lane.fetch(
        cfg or CFG,
        st or fresh_state(),
        NOW,
        env=env,
        flag=flag,
        client=client or FakeGmail(messages if messages is not None else all_five()),
    )


def all_five():
    return [parsed(name, sent=NOW - timedelta(hours=2)) for name in SENDERS]


# --------------------------------------------------------------------------
# the feature flag
# --------------------------------------------------------------------------

def test_the_flag_needs_both_the_switch_and_the_secrets():
    assert lane.enabled(ENV, flag=True)
    assert not lane.enabled(ENV, flag=False)
    assert not lane.enabled({}, flag=True)


def test_disabled_returns_dark_without_calling_gmail():
    client = FakeGmail()
    result = lane.fetch({"enabled": False}, fresh_state(), NOW, env=ENV, client=client)
    assert result.dark and result.reason == lane.DISABLED
    assert result.items == [] and client.calls == []


def test_missing_credentials_returns_dark_without_listing_mail():
    client = FakeGmail(credentials=False)
    result = lane.fetch(CFG, fresh_state(), NOW, env={}, client=client)
    assert result.dark and result.reason == gmail.MISSING_CREDENTIALS
    assert client.calls == []


def test_a_revoked_token_darkens_the_lane_and_keeps_the_watermark():
    st = fresh_state()
    client = FakeGmail(ok=False, reason=gmail.AUTH_REVOKED)
    result = lane.fetch(CFG, st, NOW, env=ENV, client=client)
    assert result.dark and not result.ok
    assert result.items == [] and result.watermark == st.watermark
    assert set(result.status) == set(lane.adapters_module.ADAPTER_IDS)


def test_an_empty_adapter_list_is_a_configuration_error_not_a_silent_run():
    result = lane.fetch({"enabled": True, "adapters": ["nope"]}, fresh_state(), NOW, env=ENV, client=FakeGmail())
    assert result.dark and result.reason == lane.NO_ADAPTERS


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_a_full_run_produces_items_from_every_adapter():
    result = run()
    assert result.ok and not result.dark
    # One fewer than the sum of the fixtures: The Rundown runs the same
    # headline twice in an issue and the lane dedups it.
    assert len(result.items) == sum(EXPECTED_STORIES.values()) - 1
    senders = {field(i, "newsletter_sender") for i in result.items}
    assert senders == {a.name for a in lane.adapters_module.ADAPTERS}


def test_every_item_carries_the_newsletter_identity_and_no_image():
    for item in run().items:
        assert field(item, "is_newsletter") is True
        assert field(item, "image_url") == ""
        assert field(item, "source_id").startswith("newsletter:")
        assert field(item, "published_at").tzinfo is not None


def test_a_story_with_no_recoverable_link_still_ships_with_a_canonical_identity():
    linkless = [i for i in run().items if not field(i, "url")]
    assert linkless, "the fixtures include tracker links that cannot be resolved"
    for item in linkless:
        assert field(item, "canonical_url").startswith("newsletter:")
        assert field(item, "description")


def test_per_adapter_status_reports_a_measurable_hit_rate():
    status = run().status
    for adapter_id, entry in status.items():
        expected = EXPECTED_STORIES[adapter_id]
        assert entry.seen == 1
        assert entry.extracted == expected
        assert entry.hit_rate == float(expected)
        assert entry.state == "ok"
        assert entry.published >= 1


def test_a_sender_whose_adapter_extracts_nothing_is_reported_as_pending():
    empty = build_message("tldr", html="<html><body><p>nothing parseable here</p></body></html>")
    result = run([empty])
    assert result.status["tldr"].state == "pending"
    assert "tldr" in result.pending_adapters
    assert result.status["milkroad"].state == "idle", "no mail is not the same as a broken adapter"


def test_mail_from_an_unlisted_sender_is_counted_but_never_identified():
    stranger = build_message("tldr", sender="someone@stranger.example")
    result = run([stranger])
    assert result.unmatched_messages == 1
    assert result.items == []


# --------------------------------------------------------------------------
# window, caps, dedup
# --------------------------------------------------------------------------

def test_the_poll_window_overlaps_the_watermark():
    client = FakeGmail([])
    st = state_module.NewsletterState(watermark=NOW - timedelta(hours=1), salt="s")
    lane.fetch({"enabled": True, "overlap_hours": 6}, st, NOW, env=ENV, client=client)
    _senders, after, _limit = client.calls[0]
    assert after == NOW - timedelta(hours=7)


def test_messages_older_than_the_retention_window_are_dropped():
    old = parsed("tldr", sent=NOW - timedelta(hours=72))
    result = run([old], cfg={"enabled": True, "max_age_hours": 48})
    assert result.items == []
    assert result.status["tldr"].seen == 1, "the message was still seen and reported"


def test_the_lane_cap_applies_after_sorting_newest_first():
    result = run(cfg={"enabled": True, "max_items": 4})
    assert len(result.items) == 4
    times = [field(i, "published_at") for i in result.items]
    assert times == sorted(times, reverse=True)


def test_only_published_stories_are_remembered():
    result = run(cfg={"enabled": True, "max_items": 4})
    assert len(result.hashes) == 4, "a story cut by the cap must be eligible again next run"


def test_a_second_run_after_advancing_the_cursor_publishes_nothing_new(tmp_path):
    path = tmp_path / "newsletter_state.json"
    first = state_module.load(path, now=NOW)
    result = run(st=first)
    assert result.items
    committed = state_module.advance(path, first, watermark=result.watermark, new_hashes=result.hashes)

    second = run(st=committed)
    assert second.items == [], "the salted hashes must suppress the overlap re-read"
    assert second.status["tldr"].extracted == EXPECTED_STORIES["tldr"], (
        "the stories were still seen and counted"
    )


def test_the_watermark_returned_is_the_run_time_and_the_caller_commits_it(tmp_path):
    path = tmp_path / "newsletter_state.json"
    st = state_module.load(path, now=NOW)
    result = run(st=st)
    assert result.watermark == NOW
    assert not path.exists(), "fetch() must not write the cursor itself"


# --------------------------------------------------------------------------
# the cursor never jumps over mail it did not read (round 1, M4)
# --------------------------------------------------------------------------

class TestTheCursorOnAShortBatch:
    def sent_two_hours_ago(self):
        return [parsed(name, sent=NOW - timedelta(hours=2)) for name in SENDERS]

    def test_a_truncated_batch_holds_the_cursor_at_the_newest_message_read(self):
        """`now` would put the unread tail outside the next window forever."""
        client = FakeGmail(self.sent_two_hours_ago(), truncated=True)
        result = lane.fetch(CFG, fresh_state(), NOW, env=ENV, client=client)
        assert result.ok and result.items, "a truncated run still publishes what it read"
        assert result.watermark == NOW - timedelta(hours=2)
        assert result.watermark != NOW

    def test_an_unreadable_message_also_holds_the_cursor(self):
        client = FakeGmail(self.sent_two_hours_ago(), fetch_failures=1)
        result = lane.fetch(CFG, fresh_state(), NOW, env=ENV, client=client)
        assert result.watermark == NOW - timedelta(hours=2)

    def test_a_short_batch_says_so_in_the_status_line(self):
        client = FakeGmail(self.sent_two_hours_ago(), truncated=True, fetch_failures=2)
        result = lane.fetch(CFG, fresh_state(), NOW, env=ENV, client=client)
        assert result.lossy and result.truncated and result.unreadable_messages == 2
        assert "more mail matched" in result.note
        assert "2 messages could not be read" in result.note
        for forbidden in ("@", "http"):
            assert forbidden not in result.note, "the note reports counts, never identities"

    def test_a_complete_clean_batch_still_advances_to_now(self):
        result = lane.fetch(CFG, fresh_state(), NOW, env=ENV,
                            client=FakeGmail(self.sent_two_hours_ago()))
        assert result.watermark == NOW and not result.lossy
        assert result.note == gmail.REASON_TEXT[gmail.OK]

    def test_the_cursor_never_moves_backwards(self):
        """Old mail in the overlap window must not drag the watermark back."""
        st = state_module.NewsletterState(watermark=NOW - timedelta(hours=1), salt="s")
        old = [parsed("tldr", sent=NOW - timedelta(hours=5))]
        result = lane.fetch(CFG, st, NOW, env=ENV, client=FakeGmail(old, truncated=True))
        assert result.watermark == st.watermark

    def test_an_empty_truncated_batch_keeps_the_committed_cursor(self):
        st = fresh_state()
        result = lane.fetch(CFG, st, NOW, env=ENV, client=FakeGmail([], truncated=True))
        assert result.watermark == st.watermark

    def test_the_short_batch_note_says_the_backlog_is_read_next_run(self):
        """R2-2: the note used to claim "cursor held back" while it moved to now.

        The claim is now the behaviour: the cursor stops at the newest message
        read, the unread remainder is newer than that, and the words say so.
        """
        client = FakeGmail(self.sent_two_hours_ago(), truncated=True)
        result = lane.fetch(CFG, fresh_state(), NOW, env=ENV, client=client)
        assert "backlog remains; it is read next run" in result.note
        assert "held back" not in result.note


# --------------------------------------------------------------------------
# the no-skip contract, end to end (round 2, R2-2)
# --------------------------------------------------------------------------
"""Design doc line 50: "mail is never silently skipped".

Round 2's repro, restated as a test. Forty messages match the window, the cap
is thirty, and the question is what happens to the other ten. Before the fix:
Gmail listed newest first, the run read the newest thirty, the watermark moved
to the newest message (which is `now` in all but name), and the OLDEST ten fell
permanently outside the next window.

The fix is in two halves and both are asserted here. `gmail.fetch` reads bodies
OLDEST first, so the ten left over are the NEWEST ten. `lane.fetch` parks the
watermark on the newest message it processed, so those ten are all newer than
the cursor and re-enter the next window. Nothing is skipped; the backlog drains
a runful per run.
"""


class TestNothingIsSkippedAcrossRuns:
    """The scenario is a resume after an outage, which is the live case.

    No `newsletter_state.json` is committed to the repo, and GitHub disables a
    scheduled workflow after 60 days of repo inactivity, so the first run on
    `main` starts from the full lookback window with a backlog waiting.
    """

    CAP = 30
    BACKLOG_CFG = {
        **CFG, "max_messages": CAP, "max_age_hours": 72,
        "overlap_hours": lane.DEFAULT_OVERLAP_HOURS,
    }

    def mailbox(self, count=40):
        """`count` messages, one per hour going back, NEWEST FIRST (Gmail order)."""
        return [parsed("tldr", sent=NOW - timedelta(hours=1 + i)) for i in range(count)]

    def read_this_run(self, mailbox, cap):
        """What `gmail.fetch`'s oldest-first order hands the lane."""
        return list(reversed(mailbox))[:cap]

    def stale_state(self, hours=41):
        return state_module.NewsletterState(
            watermark=NOW - timedelta(hours=hours), salt="fixture-salt"
        )

    def next_window_start(self, watermark):
        start, _end = state_module.plan_window(
            state_module.NewsletterState(watermark=watermark, salt="s"),
            NOW,
            overlap_hours=self.BACKLOG_CFG["overlap_hours"],
        )
        return start

    def test_the_forty_against_thirty_repro_leaves_no_message_outside_the_next_window(self):
        mailbox = self.mailbox(40)
        taken = self.read_this_run(mailbox, self.CAP)
        result = lane.fetch(
            self.BACKLOG_CFG, self.stale_state(), NOW, env=ENV,
            client=FakeGmail(taken, truncated=True),
        )
        assert result.truncated and result.lossy

        # The oldest ten, which the old code lost, were READ this run.
        read_now = {lane._sent_at(m, NOW) for m in taken}
        oldest_ten = {lane._sent_at(m, NOW) for m in list(reversed(mailbox))[:10]}
        assert oldest_ten <= read_now, "the oldest tail is what this run drains first"

        # The ten it did not read are the NEWEST ten, every one of them is
        # newer than the committed cursor, and the next window still covers them.
        deferred = [m for m in mailbox if m not in taken]
        assert len(deferred) == 10
        start = self.next_window_start(result.watermark)
        for message in deferred:
            sent = lane._sent_at(message, NOW)
            assert sent > result.watermark, "a deferred message must be newer than the cursor"
            assert sent >= start, "and must therefore fall inside the next window"

    def test_a_second_run_drains_the_remainder_and_then_advances_to_now(self):
        """Convergence, not just non-loss. The backlog is finite and shrinks."""
        mailbox = self.mailbox(40)
        first = lane.fetch(
            self.BACKLOG_CFG, self.stale_state(), NOW, env=ENV,
            client=FakeGmail(self.read_this_run(mailbox, self.CAP), truncated=True),
        )
        remainder = [m for m in mailbox if lane._sent_at(m, NOW) > first.watermark]
        assert len(remainder) == 10, "one runful drained, the rest still waiting"

        st = state_module.NewsletterState(watermark=first.watermark, salt="fixture-salt")
        second = lane.fetch(
            self.BACKLOG_CFG, st, NOW, env=ENV,
            client=FakeGmail(list(reversed(remainder)), truncated=False),
        )
        assert not second.lossy
        assert second.watermark == NOW, "a drained window advances the cursor to now"

    def test_the_next_window_never_starts_later_than_this_one_did(self):
        """The general form of the invariant the two tests above instantiate.

        Whichever branch the watermark takes (`newest_processed`, or the old
        cursor when that would be a step backwards), the next window's start is
        never later than this window's start plus what was actually consumed.
        So a message that was in this window and went unread is in the next one.
        """
        st = self.stale_state()
        this_start = self.next_window_start(st.watermark)
        mailbox = self.mailbox(40)
        result = lane.fetch(
            self.BACKLOG_CFG, st, NOW, env=ENV,
            client=FakeGmail(self.read_this_run(mailbox, self.CAP), truncated=True),
        )
        unread = [m for m in mailbox if lane._sent_at(m, NOW) > result.watermark]
        assert unread
        start = self.next_window_start(result.watermark)
        for message in unread:
            sent = lane._sent_at(message, NOW)
            assert sent >= this_start, "the fixture must really be inside this window"
            assert sent >= start, "and it stays inside the next one"

    def test_the_id_budget_reaches_the_gmail_client_from_config(self):
        client = FakeGmail(self.mailbox(2))
        lane.fetch({**CFG, "id_budget": 77}, fresh_state(), NOW, env=ENV, client=client)
        assert client.budgets == [77]

    def test_the_id_budget_defaults_without_config(self):
        client = FakeGmail(self.mailbox(2))
        lane.fetch(CFG, fresh_state(), NOW, env=ENV, client=client)
        assert client.budgets == [gmail.DEFAULT_ID_BUDGET]

# --------------------------------------------------------------------------
# sender authentication (round 1, S2)
# --------------------------------------------------------------------------

class TestSenderAuthentication:
    def test_a_dkim_pass_from_the_adapters_domain_is_published(self):
        result = run([parsed("tldr", sent=NOW - timedelta(hours=1))])
        assert result.status["tldr"].published == EXPECTED_STORIES["tldr"]
        assert result.unauthenticated_messages == 0
        assert result.unauthenticated_missing == 0

    def test_a_spoofed_from_header_is_counted_and_dropped(self):
        """The attack: anyone can write `From: dan@tldrnewsletter.com`.

        The receiving server's DKIM stamp is the half they cannot write, and
        here it names a domain the adapter does not allow.
        """
        spoofed = parsed("tldr", sent=NOW - timedelta(hours=1), dkim_domain="attacker.example")
        result = run([spoofed])
        assert result.items == []
        assert result.unauthenticated_messages == 1
        assert result.status["tldr"].seen == 0, "it is dropped before it is parsed"

    def test_a_failing_dkim_verdict_is_dropped_even_on_the_right_domain(self):
        failed = parsed("tldr", sent=NOW - timedelta(hours=1), dkim_verdict="fail")
        result = run([failed])
        assert result.items == [] and result.unauthenticated_messages == 1

    def test_a_forged_foreign_authserv_id_header_publishes_nothing(self):
        """R2-1, at the lane boundary: the bypass that put attacker copy live.

        Gmail's own verdict is `fail`; underneath it sits the header the
        attacker wrote for themselves. Reading the whole header set found their
        `pass` and published their headline under TLDR's name.
        """
        msg = parsed("tldr", sent=NOW - timedelta(hours=1), dkim_verdict="fail")
        msg["Authentication-Results"] = (
            "mx.evil-attacker.example; dkim=pass header.d=tldrnewsletter.com"
        )
        result = run([msg])
        assert result.items == []
        assert result.unauthenticated_messages == 1
        assert result.status["tldr"].seen == 0, "dropped before it is parsed"

    def test_the_trusted_authserv_id_comes_from_config(self):
        """A non-Gmail mailbox is a config change, not a source edit."""
        msg = parsed("tldr", sent=NOW - timedelta(hours=1), authenticated=False)
        msg["Authentication-Results"] = (
            "mx.fastmail.example; dkim=pass header.d=tldrnewsletter.com"
        )
        default = run([msg])
        assert default.items == [] and default.unauthenticated_missing == 1

        configured = run([msg], cfg={**CFG, "authserv_id": "mx.fastmail.example"})
        assert configured.items, "the configured server's verdict must be honoured"
        assert configured.unauthenticated_missing == 0

    def test_an_empty_configured_authserv_id_falls_back_rather_than_trusting_all(self):
        result = run(
            [parsed("tldr", sent=NOW - timedelta(hours=1))],
            cfg={**CFG, "authserv_id": ""},
        )
        assert result.items, "an empty value means the Gmail default, not 'no server'"

    def test_a_missing_header_fails_closed_with_its_own_counter(self):
        """Gmail writes this header on delivery, so a message without one is a
        surprise. Fail closed, and make the surprise visible on the first run."""
        bare = parsed("tldr", sent=NOW - timedelta(hours=1), authenticated=False)
        result = run([bare])
        assert result.items == []
        assert result.unauthenticated_missing == 1 and result.unauthenticated_messages == 0
        assert "failed sender authentication" in result.note

    def test_a_subdomain_signature_still_authenticates(self):
        """The live senders sign as `newsletter.theneurondaily.com`."""
        live = parsed("theneuron", sent=NOW - timedelta(hours=1),
                      sender="team@newsletter.theneurondaily.com")
        result = run([live])
        assert result.status["theneuron"].published >= 1
        assert result.unauthenticated_messages == 0

    def test_a_lookalike_domain_does_not_authenticate(self):
        evil = parsed("milkroad", sent=NOW - timedelta(hours=1),
                      sender="hello@evilmilkroad.com")
        result = run([evil])
        assert result.unmatched_messages == 1, "it is not even routed to an adapter"
        assert result.items == []


# --------------------------------------------------------------------------
# the record builder
# --------------------------------------------------------------------------

def test_build_record_holds_the_privacy_invariants():
    record = lane.build_record(
        title="  A headline  ",
        url="https://www.chipdesk.example/story",
        blurb="x" * 900,
        adapter_id="tldr",
        display_name="TLDR",
        published_at=NOW,
    )
    assert record["title"] == "A headline"
    assert record["image_url"] == ""
    assert record["is_newsletter"] is True
    assert record["canonical_url"] == "https://chipdesk.example/story"
    assert len(record["description"]) <= 600


def test_build_record_falls_back_to_a_stable_content_identity():
    a = lane.build_record(
        title="Same headline", url="", blurb="", adapter_id="tldr",
        display_name="TLDR", published_at=NOW,
    )
    b = lane.build_record(
        title="Same headline", url="", blurb="", adapter_id="tldr",
        display_name="TLDR", published_at=NOW + timedelta(hours=1),
    )
    assert a["canonical_url"] == b["canonical_url"] != ""
    assert a["canonical_url"].startswith("newsletter:")


def test_records_convert_to_items_only_when_the_model_supports_them():
    records = [
        lane.build_record(
            title="A headline", url="", blurb="b", adapter_id="tldr",
            display_name="TLDR", published_at=NOW,
        )
    ]
    out = lane.to_items(records)
    assert len(out) == 1
    assert field(out[0], "is_newsletter") is True
    assert field(out[0], "image_url") == ""


@pytest.mark.parametrize("name", sorted(SENDERS))
def test_a_message_from_each_sender_routes_and_publishes(name):
    result = run([parsed(name, sent=NOW - timedelta(hours=1))])
    assert result.status[name].published >= 1
