"""The cross-run health fold, including the dead-but-200 case it exists for.

Every run here is deterministic: fixed observation times, and a real recorded
feed for the progression test rather than a synthesized one.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.contracts.enums import HealthStatus
from curator.models import SourceHealth
from curator.sources import build_builtin_registry
from curator.sources.base import success_result
from curator.sources.feed import parse_feed_document
from curator.source_snapshot import _text
from curator.sources import health_record
from curator.sources.health_record import (
    HealthFoldOrderError,
    _bounded,
    _compose,
    _escaped,
    fold_source_health,
)


FEED_FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def health(
    *,
    status: str,
    usable_items: int,
    newest_at: datetime | None,
    max_age_hours: float = 6.0,
    reason_code: str = "",
    source_id: str = "probe",
    source_type: str = "rss",
) -> SourceHealth:
    return SourceHealth(
        source_id=source_id,
        status=status,
        usable_items=usable_items,
        newest_at=newest_at,
        # Deliberately left inconsistent with newest_at in some tests: the fold
        # recomputes the age and must never read this field.
        age_hours=None,
        max_age_hours=max_age_hours,
        source_type=source_type,
        reason_code=reason_code,
    )


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


# --- the case this record was frozen to catch ------------------------------


def test_a_frozen_archive_reads_stale_and_ages_while_never_failing_a_run():
    """A real recorded feed, re-served unchanged across four observations.

    This is the dead-but-200 shape: HTTP 200, well-formed XML, items parsed,
    every single run. consecutive_failures never moves, which is exactly why a
    failure counter alone could not have caught these routes.
    """

    from curator.config import load_config
    from curator.pipeline import configured_source_specs

    cfg = load_config(Path(__file__).resolve().parent.parent)
    registry = build_builtin_registry()
    spec = {s.id: s for s in configured_source_specs(cfg, registry)}["buzzing"]
    payload = (FEED_FIXTURES / "buzzing.xml").read_bytes()

    observations = [at(29, 21), at(30, 4), at(31, 4), datetime(2026, 9, 2, 4, tzinfo=timezone.utc)]
    record = None
    seen = []
    for moment in observations:
        items = parse_feed_document(payload, spec, moment)
        record = fold_source_health(record, success_result(spec, items, moment).health, moment)
        seen.append((record.status, record.newest_item_age_hours, record.consecutive_failures))

    assert [status for status, _age, _fails in seen] == [
        HealthStatus.FRESH,
        HealthStatus.STALE,
        HealthStatus.STALE,
        HealthStatus.STALE,
    ]
    ages = [age for _status, age, _fails in seen]
    assert ages == sorted(ages) and ages[0] < ages[-1]
    assert ages[-1] > spec.max_age_hours * 10
    # The whole point: nothing ever failed.
    assert [fails for _status, _age, fails in seen] == [0, 0, 0, 0]
    assert record is not None
    assert record.last_success_at == observations[-1]
    assert record.plugin_id == spec.type


# --- counters --------------------------------------------------------------


def test_a_first_observation_starts_from_zero_rather_than_inventing_a_history():
    record = fold_source_health(
        None, health(status="fresh", usable_items=3, newest_at=at(29, 20)), at(29, 21)
    )

    assert record.consecutive_failures == 0
    assert record.last_success_at == at(29, 21)
    assert record.newest_item_age_hours == pytest.approx(1.0)


def test_a_first_observation_that_fails_records_one_failure_and_no_success():
    record = fold_source_health(
        None,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )

    assert record.status is HealthStatus.UNAVAILABLE
    assert record.consecutive_failures == 1
    assert record.last_success_at is None
    assert record.newest_item_age_hours is None
    assert record.reason_code == "timeout"


def test_consecutive_failures_accumulate_then_reset_the_moment_items_return():
    record = None
    for hour in (21, 22, 23):
        record = fold_source_health(
            record,
            health(status="unavailable", usable_items=0, newest_at=None),
            at(29, hour),
        )
    assert record is not None and record.consecutive_failures == 3
    assert record.last_success_at is None

    recovered = fold_source_health(
        record, health(status="fresh", usable_items=2, newest_at=at(30, 3)), at(30, 4)
    )

    assert recovered.status is HealthStatus.FRESH
    assert recovered.consecutive_failures == 0
    assert recovered.last_success_at == at(30, 4)


def test_a_stale_run_still_counts_as_a_success_because_it_delivered_items():
    previous = fold_source_health(
        None, health(status="unavailable", usable_items=0, newest_at=None), at(29, 20)
    )

    record = fold_source_health(
        previous, health(status="fresh", usable_items=1, newest_at=at(29, 1)), at(29, 21)
    )

    assert record.status is HealthStatus.STALE
    assert record.consecutive_failures == 0
    assert record.last_success_at == at(29, 21)


def test_a_disabled_route_neither_fails_nor_succeeds_and_carries_its_history():
    previous = fold_source_health(
        None, health(status="unavailable", usable_items=0, newest_at=None), at(29, 20)
    )

    record = fold_source_health(
        previous,
        health(status="disabled", usable_items=0, newest_at=None, reason_code="disabled_by_config"),
        at(29, 21),
    )

    assert record.status is HealthStatus.DISABLED
    assert record.consecutive_failures == previous.consecutive_failures == 1
    assert record.last_success_at is previous.last_success_at is None


# --- status precedence -----------------------------------------------------


@pytest.mark.parametrize(
    ("live_status", "usable_items", "newest_at", "expected"),
    (
        ("disabled", 0, None, HealthStatus.DISABLED),
        ("unavailable", 0, None, HealthStatus.UNAVAILABLE),
        ("malformed", 0, None, HealthStatus.MALFORMED),
        ("empty", 0, None, HealthStatus.EMPTY),
        # Parsed rows that yielded nothing usable are empty, not fresh.
        ("fresh", 0, None, HealthStatus.EMPTY),
        ("fresh", 1, at(29, 1), HealthStatus.STALE),
        ("fresh", 1, at(29, 20), HealthStatus.FRESH),
        ("link_resolution_degraded", 1, at(29, 20), HealthStatus.LINK_RESOLUTION_DEGRADED),
        # Stale outranks degraded links: a frozen aggregator is the failure
        # this record exists to surface, indirect links are an accepted one.
        ("link_resolution_degraded", 1, at(29, 1), HealthStatus.STALE),
    ),
)
def test_status_follows_the_documented_precedence(live_status, usable_items, newest_at, expected):
    record = fold_source_health(
        None,
        health(status=live_status, usable_items=usable_items, newest_at=newest_at),
        at(29, 21),
    )

    assert record.status is expected


def test_a_salvaged_but_malformed_run_is_recorded_as_malformed_not_fresh():
    # The feed adapter can return items alongside status "malformed".
    record = fold_source_health(
        None,
        health(status="malformed", usable_items=2, newest_at=at(29, 20), reason_code="malformed_salvaged"),
        at(29, 21),
    )

    assert record.status is HealthStatus.MALFORMED
    # It still delivered items, so it is not a failed run.
    assert record.consecutive_failures == 0
    assert record.last_success_at == at(29, 21)


# --- legacy status hints, one test per table row ----------------------------


@pytest.mark.parametrize(
    ("hint", "usable_items", "newest_at", "expected_status", "expected_reason"),
    (
        # Row "": no hint was given, so the run is classified by evidence and
        # the reason is left exactly as it arrived.
        ("", 4, at(29, 20), HealthStatus.FRESH, "given_reason"),
        ("", 0, None, HealthStatus.EMPTY, "given_reason"),
        # Row "degraded": PARTIAL. Status still comes from items and age, the
        # reason is preserved under a partial: prefix.
        ("degraded", 4, at(29, 20), HealthStatus.FRESH, "partial:given_reason"),
        ("degraded", 4, at(29, 1), HealthStatus.STALE, "partial:given_reason"),
        ("degraded", 0, None, HealthStatus.EMPTY, "partial:given_reason"),
        # Anything absent from the table fails closed.
        (
            "mostly_ok",
            4,
            at(29, 20),
            HealthStatus.UNAVAILABLE,
            "unrecognized_status_hint:mostly_ok",
        ),
        (
            "partially_degraded",
            4,
            at(29, 20),
            HealthStatus.UNAVAILABLE,
            "unrecognized_status_hint:partially_degraded",
        ),
    ),
)
def test_every_legacy_status_hint_row_is_normalized_as_documented(
    hint, usable_items, newest_at, expected_status, expected_reason
):
    record = fold_source_health(
        None,
        health(
            status=hint,
            usable_items=usable_items,
            newest_at=newest_at,
            reason_code="given_reason",
        ),
        at(29, 21),
    )

    assert record.status is expected_status
    assert record.reason_code == expected_reason


def test_an_unrecognized_status_hint_fails_closed_rather_than_reading_fresh():
    """The bug this closes: an unknown hint used to read FRESH and reset the counter.

    Failing closed means the run is recorded as unavailable and counted as a
    failure, so an adapter that starts emitting a new word escalates instead of
    silently clearing a route's failure history.
    """

    previous = fold_source_health(
        None, health(status="unavailable", usable_items=0, newest_at=None), at(29, 20)
    )

    record = fold_source_health(
        previous,
        health(status="brand_new_word", usable_items=9, newest_at=at(29, 20)),
        at(29, 21),
    )

    assert record.status is HealthStatus.UNAVAILABLE
    assert record.reason_code == "unrecognized_status_hint:brand_new_word"
    assert record.consecutive_failures == 2
    assert record.last_success_at is None


def test_a_partial_run_that_delivered_items_clears_the_streak_and_keeps_the_signal():
    """The fold measures DELIVERY (design note in health_record.py, 2026-09-02).

    HackerNews emits status_hint="degraded" when some of its per-query requests
    failed. The items it did return are real, so the run delivered and the
    counters follow delivery like any other run. The partial-ness is not lost:
    it is recorded in the reason code, where it can be read without being
    mistaken for an outage. A route delivering items every run is not failing,
    and an alert built on a frozen success stamp would page on a healthy route.
    """

    previous = fold_source_health(
        None, health(status="unavailable", usable_items=0, newest_at=None), at(29, 19)
    )
    previous = fold_source_health(
        previous, health(status="unavailable", usable_items=0, newest_at=None), at(29, 20)
    )
    assert previous.consecutive_failures == 2

    record = fold_source_health(
        previous,
        health(
            status="degraded",
            usable_items=4,
            newest_at=at(29, 20),
            reason_code="query_failures:1",
        ),
        at(29, 21),
    )

    assert record.status is HealthStatus.FRESH
    # The signal survives, in the one field that carries it.
    assert record.reason_code == "partial:query_failures:1"
    # The counters follow delivery, not cleanliness.
    assert record.consecutive_failures == 0
    assert record.last_success_at == at(29, 21)


def test_a_partial_run_that_delivered_nothing_is_still_a_failure():
    previous = fold_source_health(
        None, health(status="unavailable", usable_items=0, newest_at=None), at(29, 20)
    )

    record = fold_source_health(
        previous,
        health(status="degraded", usable_items=0, newest_at=None, reason_code="query_failures:3"),
        at(29, 21),
    )

    assert record.status is HealthStatus.EMPTY
    assert record.consecutive_failures == 2


# --- what the reason code can and cannot carry ------------------------------


def test_a_degraded_run_that_is_also_stale_loses_its_reason_before_the_fold_sees_it():
    """The live path, not a hand-built SourceHealth.

    ``_health`` in ``base.py`` replaces BOTH the status and the reason when the
    newest item is older than the route's threshold, so ``degraded`` /
    ``front_page_stale`` never reaches this layer. Recorded as a fact rather
    than asserted away: the fold cannot recover what was overwritten upstream.
    """

    from curator.config import load_config
    from curator.pipeline import configured_source_specs

    cfg = load_config(Path(__file__).resolve().parent.parent)
    spec = {s.id: s for s in configured_source_specs(cfg, build_builtin_registry())}["buzzing"]
    moment = datetime(2026, 9, 2, 4, tzinfo=timezone.utc)
    items = parse_feed_document((FEED_FIXTURES / "buzzing.xml").read_bytes(), spec, moment)

    result = success_result(
        spec,
        items,
        moment,
        status_hint="degraded",
        reason_code="front_page_stale",
        note="query_failures:1",
    )

    # base.py overwrote both fields before this layer was reached.
    assert result.health.status == "stale"
    assert result.health.reason_code == "newest_item_too_old"

    without_note = fold_source_health(None, result.health, moment)
    assert without_note.status is HealthStatus.STALE
    assert without_note.reason_code == "newest_item_too_old"

    # SourceResult.note is the one field _health never touches, so passing it
    # is how the cause survives the overwrite.
    with_note = fold_source_health(None, result.health, moment, note=result.note)
    assert with_note.reason_code == "newest_item_too_old;note:query_failures:1"


def test_a_note_is_not_repeated_when_the_reason_already_carries_it():
    record = fold_source_health(
        None,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="request_failed"),
        at(29, 21),
        note="request_failed",
    )

    assert record.reason_code == "request_failed"


# --- age -------------------------------------------------------------------


def test_the_age_is_recomputed_against_the_supplied_observation_time():
    line = health(status="fresh", usable_items=1, newest_at=at(29, 12))

    early = fold_source_health(None, line, at(29, 13))
    later = fold_source_health(None, line, at(30, 12))

    assert early.newest_item_age_hours == pytest.approx(1.0)
    assert later.newest_item_age_hours == pytest.approx(24.0)


def test_an_item_stamped_in_the_future_is_not_negatively_aged():
    record = fold_source_health(
        None,
        health(status="fresh", usable_items=1, newest_at=at(29, 21) + timedelta(hours=2)),
        at(29, 21),
    )

    assert record.newest_item_age_hours == 0.0
    assert record.status is HealthStatus.FRESH


# --- refusals --------------------------------------------------------------


def test_a_naive_observation_time_is_refused_rather_than_assumed_to_be_utc():
    with pytest.raises(ValueError, match="timezone-aware"):
        fold_source_health(
            None,
            health(status="fresh", usable_items=1, newest_at=at(29, 20)),
            datetime(2026, 8, 29, 21),
        )


def test_folding_one_source_into_another_sources_record_is_refused():
    previous = fold_source_health(
        None, health(status="fresh", usable_items=1, newest_at=at(29, 20)), at(29, 21)
    )

    with pytest.raises(ValueError, match="one source"):
        fold_source_health(
            previous,
            health(status="fresh", usable_items=1, newest_at=at(29, 20), source_id="other"),
            at(29, 22),
        )


def test_an_observation_older_than_the_record_is_refused_not_silently_applied():
    """Refusing is the recoverable choice.

    Applying an out-of-order observation walks observed_at and last_success_at
    backwards, and the damage is invisible afterwards. Refusing is loud and the
    caller can re-order and replay. A caller that replays history in order
    never reaches this branch.
    """

    previous = fold_source_health(
        None, health(status="fresh", usable_items=1, newest_at=at(30, 3)), at(30, 4)
    )

    with pytest.raises(HealthFoldOrderError, match="backwards"):
        fold_source_health(
            previous,
            health(status="fresh", usable_items=1, newest_at=at(29, 3)),
            at(29, 4),
        )

    # Re-observing at the same moment is idempotent, not out of order: the
    # record itself comes back, so nothing can have moved. Asserting only that
    # observed_at matched could not fail, because the fold stamps observed_at
    # from the moment it was handed.
    same = fold_source_health(
        previous, health(status="fresh", usable_items=1, newest_at=at(30, 3)), at(30, 4)
    )
    assert same is previous
    # An independent expectation, not a restatement of the identity above: this
    # run delivered an item, so the counter it comes back with is 0.
    assert same.consecutive_failures == 0


def test_the_plugin_id_defaults_to_the_registry_key_the_route_was_fetched_through():
    by_default = fold_source_health(
        None,
        health(status="fresh", usable_items=1, newest_at=at(29, 20), source_type="news_sitemap"),
        at(29, 21),
    )
    explicit = fold_source_health(
        None,
        health(status="fresh", usable_items=1, newest_at=at(29, 20), source_type="news_sitemap"),
        at(29, 21),
        plugin_id="atom",
    )

    assert by_default.plugin_id == "news_sitemap"
    assert explicit.plugin_id == "atom"


# --- replaying the same moment ---------------------------------------------


def test_replaying_the_same_failed_observation_does_not_advance_the_counter():
    """The bug this closes: a retry at the same timestamp double-counted.

    A caller that folds an observation, crashes before persisting, and folds it
    again would have recorded two outages from one run.
    """

    first = fold_source_health(
        None,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )
    again = fold_source_health(
        first,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )

    assert again is first
    assert again.consecutive_failures == 1


def test_replaying_the_same_successful_observation_leaves_the_record_alone():
    first = fold_source_health(
        None, health(status="fresh", usable_items=3, newest_at=at(29, 20)), at(29, 21)
    )
    again = fold_source_health(
        first, health(status="fresh", usable_items=3, newest_at=at(29, 20)), at(29, 21)
    )

    assert again is first
    assert again.last_success_at == at(29, 21)
    assert again.consecutive_failures == 0


def test_a_naive_timestamp_on_the_previous_record_is_refused_not_a_type_error():
    """Every naive datetime raises the same typed error, wherever it sits.

    Comparing an aware moment against a naive one raises TypeError from deep
    inside the fold, which reads as a crash rather than as bad input.
    """

    aware = fold_source_health(
        None, health(status="fresh", usable_items=1, newest_at=at(29, 20)), at(29, 21)
    )
    naive_previous = dataclasses.replace(aware, observed_at=datetime(2026, 8, 29, 21))

    with pytest.raises(ValueError, match="timezone-aware"):
        fold_source_health(
            naive_previous,
            health(status="fresh", usable_items=1, newest_at=at(29, 20)),
            at(29, 22),
        )


def test_a_naive_newest_item_timestamp_is_refused_before_anything_is_folded():
    with pytest.raises(ValueError, match="timezone-aware"):
        fold_source_health(
            None,
            health(status="fresh", usable_items=1, newest_at=datetime(2026, 8, 29, 20)),
            at(29, 21),
        )


# --- the partial marker, through the real result path -----------------------


def test_the_partial_marker_in_the_note_survives_base_pys_stale_rewrite():
    """The live path: a degraded HN-shaped run that is ALSO stale.

    ``_health`` replaces both the status ("degraded" -> "stale") and the reason
    ("front_page_stale" -> "newest_item_too_old") before this layer is reached.
    The note is the only field it never touches, so the partial marker rides
    there and the fold reads it before classifying.
    """

    from curator.config import load_config
    from curator.pipeline import configured_source_specs

    cfg = load_config(Path(__file__).resolve().parent.parent)
    spec = {s.id: s for s in configured_source_specs(cfg, build_builtin_registry())}["buzzing"]
    moment = datetime(2026, 9, 2, 4, tzinfo=timezone.utc)
    items = parse_feed_document((FEED_FIXTURES / "buzzing.xml").read_bytes(), spec, moment)

    result = success_result(
        spec,
        items,
        moment,
        status_hint="degraded",
        reason_code="front_page_stale",
        note="partial;front_page_stale;query_failures:1",
    )
    assert result.health.status == "stale"
    assert result.health.reason_code == "newest_item_too_old"

    previous = fold_source_health(
        None,
        health(
            status="unavailable",
            usable_items=0,
            newest_at=None,
            source_id=spec.id,
            source_type=spec.type,
        ),
        moment - timedelta(hours=1),
    )
    assert previous.consecutive_failures == 1

    record = fold_source_health(previous, result.health, moment, note=result.note)

    assert record.status is HealthStatus.STALE
    assert record.reason_code.startswith("partial:newest_item_too_old")
    assert "query_failures:1" in record.reason_code
    # Delivery decides the counters (design note, 2026-09-02): this run
    # returned real items, so it is not an outage.
    assert record.consecutive_failures == 0
    assert record.last_success_at == moment


def test_a_partially_prefixed_word_is_not_read_as_the_partial_marker():
    # Segment-wise, never substring: "partially_degraded" is a cause, not a
    # marker, and must not silently mark the run partial.
    record = fold_source_health(
        None,
        health(status="fresh", usable_items=2, newest_at=at(29, 20), reason_code="given"),
        at(29, 21),
        note="partially_degraded",
    )

    assert record.reason_code == "given;note:partially_degraded"


def test_a_note_that_only_looks_contained_in_the_reason_is_still_recorded():
    # Substring comparison dropped this note: "query_failures:1" reads as
    # contained in "query_failures:10" while meaning something else entirely.
    record = fold_source_health(
        None,
        health(
            status="unavailable",
            usable_items=0,
            newest_at=None,
            reason_code="query_failures:10",
        ),
        at(29, 21),
        note="query_failures:1",
    )

    assert record.reason_code == "query_failures:10;note:query_failures:1"


def test_a_different_observation_at_the_same_moment_is_refused_not_dropped():
    """The lossy half of the old idempotence rule.

    One ``observed_at`` is stamped per pipeline run for every route, so a
    retried fetch inside that stamp arrives at the same moment carrying a
    genuinely different health line. Returning ``previous`` on the timestamp
    alone threw the outage away silently, which is fail-open on exactly the
    axis an alert reads. It is refused now, and the error names both.
    """

    delivered = fold_source_health(
        None, health(status="fresh", usable_items=5, newest_at=at(29, 20)), at(29, 21)
    )

    with pytest.raises(HealthFoldOrderError, match="two different observations") as raised:
        fold_source_health(
            delivered,
            health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
            at(29, 21),
        )

    message = str(raised.value)
    assert "recorded fresh/items=5" in message
    assert "incoming unavailable/items=0" in message


def test_the_same_observation_at_the_same_moment_is_still_the_same_object():
    # The refusal above must not cost the replay guarantee: identical content
    # at an identical moment still returns the record itself, unmoved.
    first = fold_source_health(
        None,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )
    again = fold_source_health(
        first,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )

    assert again is first
    assert again.consecutive_failures == 1


def test_a_replay_that_differs_only_in_its_note_is_refused_too():
    # The note composes into the reason code, so it is part of "same
    # observation": a second run that saw a new cause is not a replay.
    first = fold_source_health(
        None,
        health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
        at(29, 21),
    )

    with pytest.raises(HealthFoldOrderError, match="two different observations"):
        fold_source_health(
            first,
            health(status="unavailable", usable_items=0, newest_at=None, reason_code="timeout"),
            at(29, 21),
            note="dns_failure",
        )


# --- the bound on the composed reason code ---------------------------------


def test_the_composed_reason_code_is_capped_at_the_codebases_own_120():
    """Worst realistic case: a partial prefix, every HN cause, and the note.

    The composed reason stacks three sources of text, so it is bounded before
    persistence gives it a column. 120 is ``snapshot_health_reason``'s cap in
    ``source_snapshot.py``.
    """

    note = (
        "partial;front_page_unavailable;front_page_empty;front_page_stale;"
        "query_failures:99;query_cap:60;time_budget_exhausted"
    )
    record = fold_source_health(
        None,
        health(
            status="stale",
            usable_items=30,
            newest_at=at(20, 20),
            reason_code="newest_item_too_old",
        ),
        at(29, 21),
        note=note,
    )

    assert len(record.reason_code) <= 120
    assert record.reason_code.startswith("partial:newest_item_too_old;note:partial;")
    assert record.reason_code.endswith(";trunc:f57c2c53")

    # Cut on a SEGMENT boundary. The fixed-offset slice this replaced produced
    # the segment `query_failur` out of `query_failures:99`: a cause no adapter
    # emits, which a segment-parsing alert would have believed.
    composed = (
        "partial:newest_item_too_old;note:partial;front_page_unavailable;"
        "front_page_empty;front_page_stale;query_failures:99;query_cap:60;"
        "time_budget_exhausted"
    )
    kept, marker = record.reason_code.rsplit(";", 1)
    assert marker.startswith("trunc:")
    assert all(segment in composed.split(";") for segment in kept.split(";"))
    assert "query_failur" not in record.reason_code


def test_two_different_oversized_reasons_do_not_store_as_one_value():
    """The collision the bare ``;truncated`` marker allowed.

    Both inputs used to become the same 120-character string, so the stored
    field could not say whether truncation happened, and the equal-moment
    replay check could not tell the two observations apart.
    """

    legitimate = "x" * 110 + ";truncated"  # exactly 120: nothing is cut
    oversized = "x" * 110 + "DIFFERENT_TAIL"  # 124: truncated

    assert _bounded(legitimate) == legitimate
    assert _bounded(oversized) != _bounded(legitimate)
    # The truncated form still NAMES a cause. It used to be the bare
    # `;trunc:<hex>`: 15 characters, no cause at all, on a path whose whole
    # purpose is to be loud.
    assert _bounded(oversized).startswith("cut:x")
    marker = _bounded(oversized).rsplit(";", 1)[1]
    assert marker.startswith("trunc:") and len(marker) == len("trunc:") + 8


def test_the_truncation_marker_is_a_digest_of_the_complete_reason():
    # Same retained prefix, different tails past the bound: the stored forms
    # must still differ, because that is what makes the stored value safe to
    # compare in the equal-moment check.
    head = "a" * 100
    first = _bounded(f"{head};cause_alpha;{'z' * 40}")
    second = _bounded(f"{head};cause_omega;{'z' * 40}")

    assert first != second
    assert len(first) <= 120 and len(second) <= 120
    assert first.startswith(head) and second.startswith(head)


def test_a_different_observation_past_the_bound_at_one_moment_is_refused():
    """A genuinely different long observation at one stamp is never dropped.

    Round 3 bounded the composed reason BEFORE the equal-moment comparison, so
    everything past the cut was invisible and a genuinely different observation
    sharing one stamp came back as `previous`: silently dropped. These two
    notes agree for well past 120 characters and differ only in the tail.

    The refusal now comes from the stronger rule (a truncated record is not
    replayable at all), so the assertion is on the refusal, not on which of the
    two rules produced it.
    """

    shared = (
        "partial;front_page_unavailable;front_page_empty;front_page_stale;"
        "query_failures:99;query_cap:60;time_budget_exhausted"
    )
    first = fold_source_health(
        None,
        health(
            status="stale",
            usable_items=30,
            newest_at=at(20, 20),
            reason_code="newest_item_too_old",
        ),
        at(29, 21),
        note=f"{shared};cause_alpha",
    )
    assert len(f"{shared};cause_alpha") > 120

    # Refusal, not WHICH rule refused: two independent rules cover this input
    # (the escape plus the digest, and the truncated-record refusal), and
    # pinning the message would turn a defence-in-depth win into a false red.
    with pytest.raises(HealthFoldOrderError):
        fold_source_health(
            first,
            health(
                status="stale",
                usable_items=30,
                newest_at=at(20, 20),
                reason_code="newest_item_too_old",
            ),
            at(29, 21),
            note=f"{shared};cause_omega",
        )


def test_a_true_replay_past_the_bound_is_refused_not_silently_accepted():
    """A SHORTENED REPRESENTATION CAN PROVE DIFFERENCE BUT NEVER SAMENESS.

    An earlier round kept the replay guarantee here by comparing stored forms,
    which required the claim that no two complete reasons share one. That claim
    was false: a short reason ending in a well-formed ``;trunc:<8 hex>``
    segment stored identically to the truncated form of a long one. Escaping
    closes the reachable forgery, but the general one-way property remains, so
    an equal-moment fold onto a TRUNCATED record refuses instead of comparing.

    This deliberately costs the replay guarantee for over-long reasons. A
    refused true replay is loud and recoverable; an accepted forgery silently
    drops an observation, which is the failure this record exists to close.
    """

    note = (
        "partial;front_page_unavailable;front_page_empty;front_page_stale;"
        "query_failures:99;query_cap:60;time_budget_exhausted;cause_alpha"
    )
    first = fold_source_health(
        None,
        health(
            status="stale",
            usable_items=30,
            newest_at=at(20, 20),
            reason_code="newest_item_too_old",
        ),
        at(29, 21),
        note=note,
    )
    assert len(first.reason_code) <= 120
    assert first.reason_code.endswith(";trunc:60a8e732")

    with pytest.raises(HealthFoldOrderError, match="was truncated"):
        fold_source_health(
            first,
            health(
                status="stale",
                usable_items=30,
                newest_at=at(20, 20),
                reason_code="newest_item_too_old",
            ),
            at(29, 21),
            note=note,
        )


def test_a_short_reason_cannot_impersonate_a_truncated_long_one():
    """The round-5 aliasing pair, from the Codex leg.

    ``"a" * 121`` stored as ``;trunc:e9615320`` under the old rule, and the
    literal short reason ``";trunc:e9615320"`` stored as itself. Identical
    stored forms, so the second observation came back as the first and was
    silently dropped. Two independent rules now close it: the escape makes the
    short one store as ``;\\trunc:e9615320``, and a truncated record refuses
    an equal-moment fold outright.
    """

    long_reason = "a" * 121
    forged = ";trunc:e9615320"

    # `_bounded` no longer escapes: the escape moved to `_compose`, which
    # applies it to each caller field BEFORE the two are joined. Caller text
    # therefore reaches the bound already escaped, which is what this asserts.
    assert _bounded(_escaped(long_reason)) != _bounded(_escaped(forged))
    assert _bounded(_escaped(forged)) == ";\\trunc:e9615320"
    assert _bounded(_escaped(long_reason)).endswith(";trunc:e9615320")

    first = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code=long_reason),
        at(29, 21),
    )
    with pytest.raises(HealthFoldOrderError):
        fold_source_health(
            first,
            health(status="fresh", usable_items=3, newest_at=at(29, 20),
                   reason_code=forged),
            at(29, 21),
        )


def test_two_long_reasons_sharing_a_digest_prefix_are_still_not_folded_as_one(
    monkeypatch,
):
    """The refusal does not rest on the digest being collision-free.

    Eight hex characters is 32 bits; a shared prefix is findable. The digest is
    pinned to a constant here so the two stored forms are byte-identical, which
    is the worst case, and the fold must still refuse.
    """

    class _FixedDigest:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr(health_record.hashlib, "sha256", _FixedDigest)

    alpha = "shared_head;" + "alpha" * 40
    omega = "shared_head;" + "omega" * 40
    assert alpha != omega
    assert _bounded(alpha) == _bounded(omega)  # worst case, forced

    first = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code=alpha),
        at(29, 21),
    )
    with pytest.raises(HealthFoldOrderError):
        fold_source_health(
            first,
            health(status="fresh", usable_items=3, newest_at=at(29, 20),
                   reason_code=omega),
            at(29, 21),
        )


def test_a_note_cannot_forge_the_truncation_marker():
    """The note channel is where untrusted text will arrive.

    ``_with_note`` appends the note as further ``;`` segments verbatim, so a
    note ending in a well-formed ``trunc:<8 hex>`` segment could compose a
    reason whose stored form imitated a truncated one. The escape neutralizes
    the marker at the segment level, so the composed reason keeps the note and
    still cannot be mistaken for a truncated value.
    """

    forging_note = (
        "partial;front_page_unavailable;front_page_empty;front_page_stale;"
        "trunc:f57c2c53"
    )
    record = fold_source_health(
        None,
        health(status="stale", usable_items=30, newest_at=at(20, 20),
               reason_code="newest_item_too_old"),
        at(29, 21),
        note=forging_note,
    )

    assert len(record.reason_code) <= 120
    assert record.reason_code.endswith(";\\trunc:f57c2c53")
    assert not health_record._was_truncated(record.reason_code)
    # The cause survives the escape: nothing is dropped, only made unambiguous.
    assert "front_page_stale" in record.reason_code


def test_the_bound_enforces_the_whole_of_the_precedent_it_cites():
    """``snapshot_health_reason`` is ``_text(..., 120, ...)``, and ``_text``
    enforces TWO rules: the length AND no character below ``ord`` 32. The bound
    used to enforce only the length, so this fold could produce a row the next
    slice's persistence would hard-reject.
    """

    record = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code="given"),
        at(29, 21),
        note="cause\twith\ncontrol\rchars",
    )

    assert not any(ord(ch) < 32 for ch in record.reason_code)
    # The real validator, not a restatement of it.
    assert _text(record.reason_code, 120, "snapshot_health_reason") == record.reason_code


def test_a_fail_closed_reason_never_stores_without_naming_its_cause():
    """SF-1. ``_classify`` builds ``unrecognized_status_hint:<hint>`` out of an
    unvalidated live status string, so the loud branch is exactly where a
    single oversized segment arrives. Returning only ``;trunc:<hex>`` there
    defeated the purpose of failing closed.
    """

    record = fold_source_health(
        None,
        health(status="Z" * 300, usable_items=0, newest_at=None),
        at(29, 21),
    )

    assert record.status is HealthStatus.UNAVAILABLE
    assert record.consecutive_failures == 1
    assert record.reason_code.startswith("cut:unrecognized_status_hint:Z")
    assert len(record.reason_code) <= 120
    assert _text(record.reason_code, 120, "snapshot_health_reason") == record.reason_code


def test_a_reason_code_that_already_fits_is_left_exactly_as_composed():
    record = fold_source_health(
        None,
        health(status="fresh", usable_items=2, newest_at=at(29, 20), reason_code="given"),
        at(29, 21),
        note="cause",
    )

    assert record.reason_code == "given;note:cause"


# --- the note-segment parser, edge by edge ----------------------------------


@pytest.mark.parametrize(
    ("note", "marked"),
    (
        ("", False),
        ("front_page_stale", False),
        ("cause:partial", False),
        ("partially_degraded", False),
        ("partial", True),
        ("partial:cause:with:colons", True),
        ("a;partial;b", True),
        ("partial;partial:second", True),
    ),
    ids=(
        "empty",
        "no_partial_segment",
        "colon_elsewhere",
        "prefixed_word",
        "bare_marker",
        "marker_with_colons_in_the_cause",
        "marker_not_leading",
        "multiple_partial_segments",
    ),
)
def test_which_notes_mark_a_run_partial(note, marked):
    """The marker is a COMPLETE ``;`` segment, equal to or prefixed ``partial:``.

    ``cause:partial`` is the inverse of the marker and must not read as one;
    ``partially_degraded`` is a cause; ``partial:cause:with:colons`` is one
    marker whose cause happens to contain colons.
    """

    record = fold_source_health(
        None,
        health(status="fresh", usable_items=2, newest_at=at(29, 20), reason_code="given"),
        at(29, 21),
        note=note,
    )

    assert record.reason_code.startswith("partial:") is marked


def test_multiple_partial_segments_mark_the_reason_once_not_twice():
    record = fold_source_health(
        None,
        health(status="fresh", usable_items=2, newest_at=at(29, 20), reason_code="given"),
        at(29, 21),
        note="partial;partial:second",
    )

    assert record.reason_code.count("partial:") == 2  # the prefix, and the note's own
    assert record.reason_code == "partial:given;note:partial;partial:second"


# --- the note channel's reach, today ----------------------------------------


def test_the_fold_is_not_wired_into_the_production_collection_path_yet():
    """The current unwired boundary. BOTH possible wiring routes are pinned.

    ``fold_source_health`` sees the partial marker only when a caller passes
    ``note=``, and no production caller does. There are exactly two documented
    ways to change that, and the earlier version of this test pinned only one,
    so wiring the OTHER one would have left it green:

      route A  add a per-source note to the snapshot health row
               (``_health_dict`` in ``source_snapshot.py``)
      route B  call the fold inside ``curator.pipeline.collect``, while the
               ``SourceResult`` is still in hand

    WHAT A ROUTE-A CHANGE DELETES. Taking route A deletes the two ``row``
    assertions below (``"note" not in row`` and the exact ten-key set). Taking
    route B deletes the two ``collect`` assertions below (no
    ``fold_source_health`` name and no ``.note`` read in the source of
    ``curator.pipeline.collect``). Taking both deletes the whole test. Nothing
    here is deleted for being inconvenient: each assertion states a fact that
    is true today and false the moment its route is wired.
    """

    import inspect

    from curator import pipeline
    from curator.source_snapshot import _health_dict

    # route A: the snapshot health row.
    row = _health_dict(
        health(status="stale", usable_items=30, newest_at=at(20, 20), reason_code="x")
    )
    assert "note" not in row
    assert set(row) == {
        "source_id",
        "status",
        "usable_items",
        "newest_at",
        "age_hours",
        "max_age_hours",
        "language",
        "source_type",
        "echo_eligible",
        "reason_code",
    }

    # route B: the production collection path. `collect` is the function that
    # builds the sources tier and holds every `SourceResult`.
    collect_source = inspect.getsource(pipeline.collect)
    assert "fold_source_health" not in collect_source
    assert "result.note" not in collect_source
    assert "fold_source_health" not in inspect.getsource(pipeline)


# --- the composition namespace: two caller fields, one string ---------------


def _unescape_segment(segment: str) -> str:
    r"""Left inverse of ``_escape_segment``, so the escape is provably injective.

    A lone leading backslash is only ever the reserved-form marker: a real
    backslash is doubled first, and no reserved form starts with ``\`` or
    ``x``, so the two cases never collide.
    """

    if len(segment) >= 2 and segment[0] == "\\" and segment[1] not in "\\x":
        segment = segment[1:]
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "\\":
            following = segment[index + 1]
            if following == "\\":
                out.append("\\")
                index += 2
                continue
            assert following == "x", f"dangling escape in {segment!r}"
            out.append(chr(int(segment[index + 2 : index + 4], 16)))
            index += 4
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _unescaped(escaped: str) -> str:
    return ";".join(_unescape_segment(segment) for segment in escaped.split(";"))


ADVERSARIAL_FIELD_TEXT = (
    "",
    "timeout",
    "note:dns",
    "a;note:dns",
    "cut:query_failur",
    "a;cut:query_failur",
    "trunc:e9615320",
    ";trunc:e9615320",
    "\\note:dns",
    "\\\\note:dns",
    "back\\slash",
    "tab\there",
    "nul\x00end",
    "partial;front_page_stale",
    "查询失败;note:超时",
)


def test_a_caller_note_segment_cannot_impersonate_the_module_note_separator():
    """The round-6 aliasing pair, from the Codex leg.

    ``_with_note`` joins the reason code and the note with a bare ``;note:``.
    While the escape ran on the JOINED string, that separator was
    indistinguishable from a caller segment that simply read ``note:...``, so
    these two DIFFERENT observations composed to the one value
    ``timeout;note:dns`` and an equal-moment fold of the second onto the first
    returned the first record instead of raising: an observation silently
    dropped, which is the exact failure this record exists to close.
    """

    first = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code="timeout;note:dns"),
        at(29, 21),
    )
    # The caller's `note:` segment is escaped; only the module writes a bare one.
    assert first.reason_code == "timeout;\\note:dns"

    with pytest.raises(HealthFoldOrderError, match="two different observations"):
        fold_source_health(
            first,
            health(status="fresh", usable_items=3, newest_at=at(29, 20),
                   reason_code="timeout"),
            at(29, 21),
            note="dns",
        )

    assert _compose("timeout;note:dns", "") != _compose("timeout", "dns")


def test_a_note_that_carries_its_own_note_segment_is_still_distinct():
    """The same alias one level in: ``note:`` inside a semicolon-separated note.

    Escaping per FIELD is what closes this. Escaping the joined string could
    not, because by then both `note:` occurrences look alike.
    """

    first = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code="timeout"),
        at(29, 21),
        note="dns;note:forged",
    )
    assert first.reason_code == "timeout;note:dns;\\note:forged"

    with pytest.raises(HealthFoldOrderError, match="two different observations"):
        fold_source_health(
            first,
            health(status="fresh", usable_items=3, newest_at=at(29, 20),
                   reason_code="timeout;note:dns"),
            at(29, 21),
            note="forged",
        )


def test_the_escape_round_trips_so_it_can_never_merge_two_observations():
    """Injectivity is the property the whole scheme rests on, so it is PROVEN.

    A left inverse exists means the map is injective. The decoder is written
    here rather than in the module because nothing in production reads a stored
    reason back yet; when the persistence slice does, this is the contract it
    inherits.
    """

    for text in ADVERSARIAL_FIELD_TEXT:
        assert _unescaped(_escaped(text)) == text, text

    escapes = [_escaped(text) for text in ADVERSARIAL_FIELD_TEXT]
    assert len(set(escapes)) == len(escapes)

    # And across BOTH fields at once: distinct (reason, note) pairs never
    # compose to one string. `_with_note` deliberately drops an empty note and
    # a note already present as a segment of the reason; those pairs are the
    # SAME observation restated, not a collision, so they are excluded.
    seen: dict[str, tuple[str, str]] = {}
    for reason in ADVERSARIAL_FIELD_TEXT:
        segments = tuple(
            segment for segment in _escaped(reason).split(";") if segment
        )
        for note in ADVERSARIAL_FIELD_TEXT:
            if not note or _escaped(note) in segments:
                continue
            composed = _compose(reason, note)
            assert composed not in seen, (composed, seen.get(composed), (reason, note))
            seen[composed] = (reason, note)


def test_a_hard_cut_never_lands_inside_an_escape_sequence():
    r"""SF-B. The fail-closed head used to be a fixed-offset slice over ALREADY
    escaped text, so it cut ``\x09`` into ``\x0`` or left a dangling backslash:
    rule 2's defect class, reintroduced one level down inside the escape
    alphabet by the code written to close rule 4.
    """

    for pad in (98, 99, 100):
        raw = "u" * pad + "\t" + "v" * 200
        stored = _bounded(_escaped(raw))

        assert len(stored) <= 120, pad
        body, marker = stored.rsplit(";", 1)
        assert marker.startswith("trunc:"), pad
        assert body.startswith("cut:"), pad
        # Decodable: no half-written escape survived the cut.
        assert _unescape_segment(body[len("cut:"):]) == "u" * pad, pad


def test_a_caller_cannot_forge_the_hard_cut_marker():
    """SF-C. ``cut:`` was declared a module marker in the docstrings but was
    not reserved out of caller text, unlike ``trunc:<8 hex>``: one of the two
    structural forms was defended and the other only described. A reader could
    see a hard-cut fragment in a reason that was never cut.
    """

    record = fold_source_health(
        None,
        health(status="fresh", usable_items=3, newest_at=at(29, 20),
               reason_code="given"),
        at(29, 21),
        note="a;cut:query_failur",
    )

    assert record.reason_code == "given;note:a;\\cut:query_failur"
    assert not any(
        segment.startswith("cut:") for segment in record.reason_code.split(";")
    )
