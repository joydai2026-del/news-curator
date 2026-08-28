"""The lane end to end: the flag, the statuses, the cursor, the caps.

The Gmail HTTP layer is replaced wholesale by `FakeGmail`, so these tests
exercise routing, extraction, dedup and reporting without a socket in sight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.newsletter import gmail, lane, state as state_module
from tests.test_newsletter_fixtures import (
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

    def has_credentials(self, env):
        return self.credentials

    def fetch(self, senders, after, *, env=None, limit=30, timeout=20.0):
        self.calls.append((list(senders), after, limit))
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
    assert len(result.items) == 15  # 3 stories from each of the five fixtures
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
        assert entry.seen == 1
        assert entry.extracted == 3
        assert entry.hit_rate == 3.0
        assert entry.state == "ok"
        assert entry.published == 3


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
    assert second.status["tldr"].extracted == 3, "the stories were still seen and counted"


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


# --------------------------------------------------------------------------
# sender authentication (round 1, S2)
# --------------------------------------------------------------------------

class TestSenderAuthentication:
    def test_a_dkim_pass_from_the_adapters_domain_is_published(self):
        result = run([parsed("tldr", sent=NOW - timedelta(hours=1))])
        assert result.status["tldr"].published == 3
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
