"""Fold one run's per-route health into the durable cross-run record.

PURE. No I/O, no persistence, no clock of its own: the caller supplies the
observation time. Persistence lands with the checkpoint wiring, which this
module deliberately does not touch.

WHY THIS EXISTS. ``SourceHealth`` is per-run only. Three routes served a
parseable but frozen archive for 77 to 237 days and nothing escalated, because
each individual run looked exactly like a healthy one: HTTP 200, well-formed
XML, items parsed. The run was fine every single time. What was not fine was
the SEQUENCE, and nothing was keeping one.

WHAT THIS FOLD MEASURES: DELIVERY. Design note, decided 2026-09-02.

Two review rounds reversed each other on whether a partial run should freeze
the counters. A reversal means the question was not decidable from the evidence
either round held, so it is settled here on an explicit second-order principle
and written INTO the module, so a later round cannot flip it back quietly:

    THE FOLD MEASURES DELIVERY, NOT CLEANLINESS.

    ``last_success_at``     stamps whenever the run delivered at least one
                            usable item.
    ``consecutive_failures``
                            counts consecutive runs that delivered zero usable
                            items or failed in transport, and resets on ANY
                            delivery.
    a PARTIAL run           (the legacy ``degraded`` hint, or a ``partial``
                            marker in the supplied note) is recorded ONLY as a
                            reason-code signal, ``partial:<reason>``. The
                            partial-ness moves neither the counter nor the
                            stamp: what the counters do is decided by delivery,
                            exactly as for every other run.

WHY THAT WAY ROUND. A route delivering 30 items per run is not failing,
whatever hint it carries, and an alert built on a frozen ``last_success_at``
would page on a healthy route. A route delivering nothing IS failing, whatever
hint it carries. Making "not clean" a third counter state puts both of those
errors into the same field. The cleanliness signal is real and is kept, in the
reason code, where it can be read without being mistaken for an outage.

THE ONE NON-DELIVERY CASE is ``disabled``: the route was not polled at all, so
there was no delivery to measure and both counters carry through untouched.

DEAD-BUT-200 DETECTION USES ``newest_item_age_hours`` AND ``status``, NEVER THE
COUNTER. A frozen archive succeeds every run under the rule above, so its
``consecutive_failures`` sits at 0 and its ``last_success_at`` keeps advancing.
That is correct and expected: the route really is delivering. The age axis is
what catches it, reading STALE with a growing age. Any alert built on this
record must read those two fields for the frozen-archive case; a counter-only
alert would call such a route healthy forever, which is exactly the miss this
record was frozen to close.

STATUS PRECEDENCE, in this order and no other:

    1. disabled            the route was not polled at all
    2. unavailable         the transport failed
    3. malformed           the payload failed to parse
    4. empty               parsed, but zero usable items
    5. stale               newest item older than the route's max_age_hours
    6. link_resolution_degraded   items are real, their links are indirect
    7. fresh               everything else

Stale outranks link-resolution-degraded on purpose: an aggregator whose links
are indirect is a known, accepted condition, while an aggregator that stopped
publishing is the failure this record exists to surface. Recording the
precedence here, rather than in a reviewer's head, is what stops a later round
from reversing it silently.

STATUS IS RECOMPUTED, NOT COPIED. The live status string is consulted only for
the two conditions this layer cannot re-derive (transport failure, parse
failure). Freshness and staleness are recomputed from ``newest_at`` against the
supplied observation time, so a stale route cannot be recorded as fresh by a
health line that was built with a different clock.

LEGACY STATUS HINTS, AND WHY AN UNKNOWN ONE FAILS CLOSED.
``HackerNewsAdapter`` emits ``status_hint="degraded"``, which is not one of the
seven frozen ``HealthStatus`` values. The frozen enum has NO partial-success
value, so this layer cannot record the word. It is normalized instead, from one
table (``_LEGACY_STATUS_HINTS``) with a test per row:

    hint          treatment
    ""            no hint was given; classify by evidence
    "degraded"    PARTIAL: the status is computed from items and age as usual,
                  the reason is preserved as ``partial:<original reason>``, and
                  the counters follow delivery per the design note above
    anything else FAIL CLOSED: ``unavailable`` with reason
                  ``unrecognized_status_hint:<hint>``, counted as a failure

Escalated, not decided here: the frozen ``HealthStatus`` vocabulary has no
value for "delivered items, run was not clean". Adding one is a contract
question for the next freeze revision, and it stays open.

WHAT THE REASON CODE CAN AND CANNOT CARRY. ``_health`` in ``base.py``
OVERWRITES ``reason_code`` with ``newest_item_too_old`` whenever items exist and
the newest is older than the route's threshold, and overwrites the status with
``stale`` at the same time. So a degraded run that is ALSO stale reaches this
fold with neither its ``degraded`` status nor its original reason: both were
replaced before this layer ever saw the line.

What survives that rewrite is ``SourceResult.note``, which the adapter sets
separately and ``_health`` never touches. So the note is the channel a partial
run signals through: a ``;``-separated segment equal to ``partial`` or starting
with ``partial:`` marks the run partial, and this fold reads that marker BEFORE
classifying, so a run relabelled ``stale`` upstream still records
``partial:<reason>``. ``HackerNewsAdapter`` emits that leading ``partial``
segment on its degraded branch. The rest of the note is recorded alongside the
reason rather than lost.

THE NOTE CHANNEL REACHES THIS FOLD AND NOTHING ELSE, TODAY. The partial marker
arrives ONLY when a caller passes ``note=`` into this function, and no
production code does: ``curator/pipeline.py`` builds the sources tier from
``result.items`` and ``result.health`` and replaces every per-source note with
one ``"N source alerts"`` summary, so ``SourceResult.note`` dies in
``curator.pipeline.collect``'s own stack frame. (An earlier draft of this
paragraph named that function ``_source_tier``, which does not exist anywhere
in the tree; the sources tier is built by ``collect``.) The snapshot cannot
carry it either (``_health_dict`` in ``source_snapshot.py`` has ten keys and
none of them is a note). Wiring the fold therefore means either calling it
inside ``curator.pipeline.collect`` while the result is still in hand, or
adding a per-source note to the snapshot health row. Either route is possible
future implementation work; this module records no project order and stops at
the pure fold.

THE REASON CODE IS BOUNDED AT 120 CHARACTERS FOR STORAGE ONLY, the codebase's
own precedent for a health reason (``snapshot_health_reason`` in
``source_snapshot.py``). The bound is named here, before persistence exists,
because the composed reason can stack a ``partial:`` prefix, several upstream
causes and an appended note.

TWO PROPERTIES OF THE BOUND ARE LOAD-BEARING, and both exist because the first
version of it broke something else.

    CUT ON A SEGMENT BOUNDARY, NEVER MID-SEGMENT. A fixed-offset slice turned
    ``query_failures:99`` into the segment ``query_failur``, which a later
    alert parsing ``;`` segments would read as a real cause that no adapter
    ever emitted. Only complete segments are kept.

    THE STORED FORM CAN PROVE DIFFERENCE, NEVER SAMENESS. A truncated reason
    ends ``;trunc:<8 hex characters of the sha256 of the COMPLETE composed
    reason>``. Two different long reasons sharing a retained prefix therefore
    store differently. That is a one-way guarantee and the module now treats it
    as one. The earlier marker was the plain segment ``;truncated``, an
    ordinary segment name: a reason genuinely ending in it was
    indistinguishable from a truncated one, and two different oversized reasons
    stored identically. A segment of the form ``trunc:<hex>`` is NOT a
    recognized cause and must never be parsed as one.

    WHAT IS NOT TRUE, stated because an earlier version of this file asserted
    it without qualification: the stored form is NOT a collision-free stand-in
    for an arbitrary composed reason. ``_bounded`` returns any input of 120
    characters or fewer verbatim, so a SHORT reason whose text ends in a
    well-formed ``;trunc:<8 hex>`` segment would store identically to the
    truncated form of some long one. Two rules close that, and both are needed:

        ESCAPE EACH FIELD ON THE WAY IN, INDEPENDENTLY, BEFORE COMPOSITION.
        The reason code and the note are the only two caller inputs, and each
        is escaped on its own ``;`` segments BEFORE the two are joined: a
        segment matching ``trunc:<8 hex>`` becomes ``\\trunc:<8 hex>``, a
        segment beginning with a prefix this module owns (``note:``, ``cut:``)
        becomes ``\\note:...`` or ``\\cut:...``, a backslash becomes two, and a
        control character becomes ``\\xNN``. The escape is injective, so it
        cannot merge two different inputs, and after it the ONLY bare
        ``;trunc:<hex>``, ``;note:`` or ``cut:`` form in a stored value is one
        this module wrote.

        COMPOSING FIRST AND ESCAPING AFTERWARDS WAS ITS OWN ALIAS, written
        down here because it survived four rounds. ``_with_note`` joins the
        two fields with a bare ``;note:`` separator, so escaping the JOINED
        string left that separator indistinguishable from caller text: the
        pair ``reason_code="timeout;note:dns", note=""`` and
        ``reason_code="timeout", note="dns"`` composed to one stored value,
        and an equal-moment fold of the second onto the first returned the
        first record instead of raising. Escaping per field closes it, because
        the caller's ``note:`` segment is escaped and the module's is not.

        Escaping was chosen over rejecting the input: rejection would throw
        away a real observation (and would need its own reason code, which is
        the very channel under repair) to defend against text no live adapter
        emits, whereas escaping preserves the observation exactly and is
        reversible by eye. ``partial`` is deliberately NOT reserved: adapters
        emit it on purpose through the note channel, so it is shared
        vocabulary rather than a marker this module owns.

        THE ENCODE PATH IS ONE-SHOT, NOT IDEMPOTENT, BY DESIGN, and round 5
        recorded the opposite as a verified-clean property. ``_escaped`` is an
        encoder: ``_escaped(_escaped(x)) != _escaped(x)`` whenever x contains a
        backslash, a control character or a reserved form. It runs exactly
        once per observation, at composition, on raw caller text. ``_bounded``
        alone is idempotent because it no longer escapes, but a value read back
        from storage must never be sent through ``_compose`` again.

        A TRUNCATED PRIOR IS NON-REPLAYABLE. When the record already on disk
        carries the reserved suffix, an equal-moment fold raises
        ``HealthFoldOrderError`` naming the truncation, rather than comparing
        stored forms: A SHORTENED REPRESENTATION CAN PROVE DIFFERENCE BUT
        NEVER SAMENESS. This costs the replay guarantee for over-long reasons
        on purpose. Refusing a true replay is a loud, recoverable error;
        accepting a forgery as a replay silently drops an observation, which
        is the failure this whole record exists to close.

        HOW OFTEN THIS FIRES, measured rather than assumed: an ordinary Hacker
        News run that is both degraded and stale composes a reason of about
        133 characters out of vocabulary ``hackernews.py`` actually emits, so
        it truncates on the everyday path rather than in some edge case. The
        operational consequence, stated plainly: on that route an IDENTICAL
        replay of a long reason RAISES. A caller is expected to fold once per
        observation; a persistence retry must key on the record it already
        wrote, never on re-folding the same observation at the same moment.

    Codex's round-4 prescription (carry ``reason_truncated`` as its own field
    so the state is not encoded in an allowed text segment) is DECLINED for
    this slice, and the declination is written here rather than left silent:
    ``SourceHealthRecord`` is frozen and is a hard boundary this slice must not
    cross. Adding the field is the right shape and belongs in the next contract
    revision; the two rules above are what makes the frozen field safe until
    then.

COMPARE FIRST, BOUND LAST. The equal-moment check is made on the COMPLETE
composed reason (the stored form can prove difference; a truncated one can never
prove sameness, which is why a truncated prior refuses replay), never on a value
that was already cut. Bounding first and comparing second made everything past
the cut invisible to the replay check, so a genuinely different observation
sharing one stamp came back as a replay: silently dropped, in the same
fail-open direction this fold was written to close. The ordering is restated at
the call site so the two cannot trade places again.

ONE KNOWN BYPASS OF THE STALENESS SIGNAL, named so a later alert is not built
on the wrong field: ``feed.py`` stamps ``published_at=min(published, now)``, so
a frozen archive whose entries are dated in the FUTURE reads FRESH with age 0
forever. The age axis cannot see that route.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Mapping

from ..contracts.enums import HealthStatus
from ..contracts.source_plugin import SourceHealthRecord
from ..models import SourceHealth


class HealthFoldOrderError(ValueError):
    """An observation that cannot be folded onto the record it was given.

    Two cases, both refused rather than silently dropped or silently applied:

    1. An observation STRICTLY OLDER than the record. Applying it walks
       ``observed_at`` and ``last_success_at`` backwards; dropping it hides a
       replay bug.
    2. A DIFFERENT observation at exactly the record's own moment. Sharing a
       timestamp is not evidence of being the same run: one ``observed_at`` is
       stamped per pipeline run for every route, so a retried or re-dispatched
       fetch inside that stamp produces a genuinely different health line. The
       fold cannot tell which of the two is right, and the failing one is the
       one silent-drop would throw away, so it refuses instead.

    An observation at the record's moment whose CONTENT matches (same status,
    same usable-item count, same newest-item age, same composed reason code) is
    a true replay and is not this error: the record comes back unchanged.

    A caller that replays history in order never sees either case.
    """


#: Live status strings this layer trusts, because it cannot re-derive them from
#: the item set alone. Everything else is decided from the evidence.
_TRANSPORT_FAILURE = "unavailable"
_PARSE_FAILURE = "malformed"
_DISABLED = "disabled"
_DEGRADED_LINKS = "link_resolution_degraded"

#: What a run does to the two counters.
_SUCCESS = "success"
_FAILURE = "failure"
_NEITHER = "neither"

#: How a live status string outside the frozen vocabulary is normalized. One
#: row per hint, one test per row. Anything absent from this table fails closed.
_NO_HINT = "no_hint"
_PARTIAL = "partial"
_LEGACY_STATUS_HINTS: Mapping[str, str] = {
    "": _NO_HINT,
    "degraded": _PARTIAL,
}

#: The note segment an adapter uses to say "this run was partial" through the
#: one field ``_health`` never rewrites. See the module docstring.
_PARTIAL_NOTE_MARKER = "partial"

_FROZEN_STATUS_VALUES = frozenset(status.value for status in HealthStatus)

#: The codebase's own bound on a health reason (``snapshot_health_reason``,
#: ``source_snapshot.py``). Named here before a persistence column exists.
_MAX_REASON_CODE = 120
#: A truncated reason ends with this prefix plus a short digest of the
#: COMPLETE composed reason. The stored form can prove that two observations
#: differ; it can never prove they are the same, so a truncated prior is
#: non-replayable. Not a cause; never parse it as one.
_TRUNCATION_PREFIX = ";trunc:"
_TRUNCATION_DIGEST_CHARS = 8
#: The reserved segment form, as it appears AFTER escaping. A caller segment
#: matching this is escaped on the way in, so the only bare occurrence in a
#: stored value is the marker this module appended.
_RESERVED_SEGMENT = re.compile(r"trunc:[0-9a-f]{8}")
#: The same form anchored as the tail of a stored value: this is how the fold
#: recognizes that a record it was handed was truncated.
_TRUNCATED_TAIL = re.compile(r";trunc:[0-9a-f]{8}$")
#: Marks a segment that had to be cut mid-way because no complete segment fit.
#: Not a cause either; it exists so a fail-closed reason still names something.
_HARD_CUT_PREFIX = "cut:"
#: The separator ``_with_note`` writes between the reason code and the note.
_NOTE_PREFIX = "note:"
#: Segment prefixes this module OWNS in a composed reason. A caller segment
#: beginning with one of them is escaped on the way in, exactly as a whole
#: ``trunc:<8 hex>`` segment is, so the only bare occurrence in a stored value
#: is one this module wrote. ``partial`` is deliberately absent: adapters emit
#: it on purpose through the note channel (see ``_PARTIAL_NOTE_MARKER``), so it
#: is shared vocabulary rather than a module-owned marker.
_RESERVED_PREFIXES = (_NOTE_PREFIX, _HARD_CUT_PREFIX)
#: The two hex digits of an escaped control character, used to walk escape
#: units so a hard cut never lands inside one.
_HEX_PAIR = re.compile(r"[0-9a-f]{2}")


def fold_source_health(
    previous: SourceHealthRecord | None,
    health: SourceHealth,
    observed_at: datetime,
    *,
    plugin_id: str = "",
    note: str = "",
) -> SourceHealthRecord:
    """Return the record that results from observing ``health`` at ``observed_at``.

    ``previous`` is ``None`` for a route's first ever observation; the returned
    record then starts its counters from zero rather than inventing a history.

    ``plugin_id`` defaults to the health line's own ``source_type``, which is
    the registry key the route was fetched through.

    ``note`` is ``SourceResult.note``. It carries the partial marker and any
    degradation cause that ``_health`` in ``base.py`` overwrote, so pass it
    whenever the caller holds the result.

    Re-observing at exactly ``previous.observed_at`` returns ``previous``
    itself ONLY when the observation is the same one: same status after
    normalization, same usable-item count, same newest-item age, same composed
    reason code. A replay of an already-folded observation must not move a
    counter. A DIFFERENT observation at that same moment raises
    ``HealthFoldOrderError`` naming both, because the fold has no way to tell
    which is authoritative and silently keeping the first would drop an outage.
    A strictly older moment raises the same error.
    """

    # Every datetime is validated up front, before any early return, so a naive
    # value anywhere raises this same typed error rather than surfacing as a
    # TypeError from a comparison deeper in.
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if health.newest_at is not None and health.newest_at.tzinfo is None:
        raise ValueError("newest_at must be timezone-aware")
    if previous is not None:
        if previous.source_id != health.source_id:
            raise ValueError("health record and health line must describe one source")
        if previous.observed_at.tzinfo is None:
            raise ValueError("previous.observed_at must be timezone-aware")
        if previous.last_success_at is not None and (
            previous.last_success_at.tzinfo is None
        ):
            raise ValueError("previous.last_success_at must be timezone-aware")

    moment = observed_at.astimezone(timezone.utc)
    if previous is not None and moment < previous.observed_at:
        raise HealthFoldOrderError("observations must not go backwards")

    partial_note = _note_marks_partial(note)
    age_hours = _newest_age_hours(health.newest_at, moment)
    status, reason_code, outcome = _classify(health, age_hours)
    if partial_note:
        reason_code = _mark_partial(reason_code)
    # COMPLETE, unbounded, and ESCAPED PER FIELD before the two are joined:
    # escaping the joined string instead let a caller's `note:` segment
    # impersonate the module's own separator. This is the value the
    # equal-moment check below is made against; `_bounded` only caps length.
    composed = _compose(reason_code, note)

    # An equal moment is decided by CONTENT, not by the timestamp alone. The
    # observation is classified first so there is something to compare: two
    # runs sharing one stamp are the same run only if they saw the same thing.
    # Deciding on the timestamp alone was silently lossy in the one direction
    # that matters, dropping a transport failure that arrived after a success.
    #
    # ORDERING CONSTRAINT, LOAD-BEARING: COMPARE FIRST, BOUND LAST. `composed`
    # is the complete observation; the 120-character bound is a STORAGE rule
    # and must never run before this comparison. It did once, and everything
    # past the cut became invisible here: two different long observations at
    # one stamp compared equal and the second was returned as `previous`, with
    # no exception, no counter and no record that it ever arrived. `_bounded`
    # embeds a digest of the COMPLETE value precisely so the stored form
    # remains a faithful stand-in for what is compared here.
    #
    # WHICH HALF IS LOAD-BEARING, MEASURED, not assumed, and RESTATED in round
    # 5 because the answer changed. Under round 4's rules the DIGEST was the
    # mechanism and this ordering was defence in depth. Round 5 replaced the
    # comparison's licence entirely: a truncated record is now refused below
    # rather than compared through, so neither the digest nor this ordering is
    # what stops the over-the-bound silent drop. Both are kept anyway: the
    # digest still distinguishes two truncated values in the error message and
    # in storage, and composing before bounding is what gives that message the
    # complete incoming observation to name. Do not re-describe either one as
    # the guarantee.
    if previous is not None and moment == previous.observed_at:
        # A SHORTENED REPRESENTATION CAN PROVE DIFFERENCE BUT NEVER SAMENESS.
        # If the stored reason carries the reserved truncation suffix, the
        # complete value it stood for is gone, so no comparison made through it
        # can establish that this observation is the one already folded in. The
        # fold refuses instead of guessing. This costs the replay guarantee for
        # over-long reasons deliberately: a refused true replay is loud and
        # recoverable, an accepted forgery silently drops an observation.
        if _was_truncated(previous.reason_code):
            raise HealthFoldOrderError(
                "cannot replay onto a truncated record at one moment "
                f"{moment.isoformat()}: the recorded reason "
                f"{previous.reason_code!r} was truncated, so its stored form "
                "can prove difference but never sameness"
            )
        if not _same_observation(previous, status, health.usable_items, age_hours, composed):
            raise HealthFoldOrderError(
                "two different observations share one moment "
                f"{moment.isoformat()}: recorded "
                f"{previous.status.value}/items={previous.usable_items}/"
                f"age={previous.newest_item_age_hours}/reason={previous.reason_code!r} "
                f"vs incoming {status.value}/items={health.usable_items}/"
                f"age={age_hours}/reason={_bounded(composed)!r}"
            )
        return previous

    reason_code = _bounded(composed)

    carried_failures = previous.consecutive_failures if previous else 0
    carried_success = previous.last_success_at if previous else None

    if outcome == _SUCCESS:
        consecutive_failures = 0
        last_success_at = moment
    elif outcome == _FAILURE:
        consecutive_failures = carried_failures + 1
        last_success_at = carried_success
    else:
        consecutive_failures = carried_failures
        last_success_at = carried_success

    return SourceHealthRecord(
        source_id=health.source_id,
        plugin_id=plugin_id or health.source_type,
        status=status,
        usable_items=health.usable_items,
        newest_item_age_hours=age_hours,
        max_age_hours=health.max_age_hours,
        observed_at=moment,
        reason_code=reason_code,
        consecutive_failures=consecutive_failures,
        last_success_at=last_success_at,
    )


def _newest_age_hours(newest_at: datetime | None, moment: datetime) -> float | None:
    """Hours between the newest item's publication and the observation.

    Recomputed here rather than read from ``SourceHealth.age_hours`` so the age
    is always measured against the observation time this fold was given. Clamped
    at zero: an item stamped slightly in the future is not negatively aged.
    """

    if newest_at is None:
        return None
    delta = (moment - newest_at.astimezone(timezone.utc)).total_seconds() / 3600.0
    return max(0.0, delta)


def _same_observation(
    previous: SourceHealthRecord,
    status: HealthStatus,
    usable_items: int,
    age_hours: float | None,
    composed_reason: str,
) -> bool:
    """Whether this observation is the one already folded into ``previous``.

    ``composed_reason`` is the COMPLETE, UNBOUNDED composed reason. It is
    compared against ``previous.reason_code``, which is that same value after
    ``_bounded``, so the comparison is made through the stored stand-in rather
    than against a value that was cut. Comparing an already-truncated value
    directly is what re-opened the silent-drop bug, so the bound is applied
    HERE, to the incoming value, and never earlier.

    THE SCOPE OF WHAT THIS COMPARISON CAN CONCLUDE, stated exactly, because an
    earlier version of this docstring overclaimed it as "two different complete
    reasons cannot share one stored form". That is false in general:
    ``_bounded`` returns any value of 120 characters or fewer verbatim, so
    without further rules a short reason could imitate the truncated form of a
    long one. The caller guarantees it relies on are:

    1. ``_compose`` ESCAPES each caller field on its own before joining them,
       injectively, covering the reserved ``trunc:<8 hex>`` segment form and
       the module-owned ``note:`` and ``cut:`` prefixes. So a value that was
       NOT truncated can never end in the bare marker, a caller cannot forge
       the note separator, and among non-truncated values the stored form is
       the escaped value itself: equality really is equality.
    2. ``fold_source_health`` never reaches this function when ``previous`` was
       truncated: that case raises, because the complete value is gone.

    So this function decides sameness only where the stored forms are faithful.

    ``newest_item_age_hours`` stands in for the newest item's timestamp: at an
    equal ``observed_at`` the age is computed against the same moment, so equal
    ages mean the same newest item time. The composed reason carries the
    supplied note, which is why the raw note is not compared separately:
    ``previous`` does not store one.

    TWO OF THE RECORD'S TEN FIELDS ARE DELIBERATELY EXEMPT.
    ``consecutive_failures`` and ``last_success_at`` are the fold's own
    outputs, not observations. ``plugin_id`` and ``max_age_hours`` are route
    CONFIGURATION rather than what the run saw: they come from the spec, not
    from the fetch, so a change in either is a reconfiguration between two
    runs sharing one stamp and not a second observation. They are named here
    rather than left as an unstated gap.
    """

    return (
        previous.status is status
        and previous.usable_items == usable_items
        and previous.newest_item_age_hours == age_hours
        and previous.reason_code == _bounded(composed_reason)
    )


def _bounded(reason_code: str) -> str:
    """Cap the composed reason at the codebase's 120-character precedent.

    ``snapshot_health_reason`` in ``source_snapshot.py`` already caps a health
    reason at 120, and the composed reason here can stack a ``partial:``
    prefix, several upstream causes and an appended note.

    Five rules, each there because a simpler version broke something:

    1. THE INPUT IS ALREADY ESCAPED, AND IS NEVER ESCAPED AGAIN HERE. The
       escape is the whole of the ``_text`` rule the cited precedent enforces,
       not half of it: ``_text`` in ``source_snapshot.py`` rejects a reason on
       TWO grounds, length and any character with ``ord(ch) < 32``, and
       enforcing only the length produced rows this fold accepted and
       persistence would reject. ``_compose`` applies ``_escaped`` to the
       reason code and to the note SEPARATELY, before joining them, so what
       arrives here is escaped text in which the module's own ``;note:``
       separator is already bare. Escaping again would rewrite that separator
       and every ``\\xNN`` this module wrote a second time, so this function
       only measures and cuts.
    2. ONLY COMPLETE ``;`` SEGMENTS ARE KEPT. A fixed-offset slice produced the
       segment ``query_failur`` out of ``query_failures:99``, a cause no
       adapter ever emitted and which a segment-parsing alert would believe.
    3. THE MARKER CARRIES A DIGEST OF THE COMPLETE STRING, ``;trunc:<8 hex>``.
       A bare ``;truncated`` marker is an ordinary segment name, so a reason
       genuinely ending in it was indistinguishable from a truncated one, and
       two different oversized reasons sharing a prefix stored identically.
       ``trunc:<hex>`` IS NOT A CAUSE and must never be parsed as one. The
       digest proves DIFFERENCE between two complete values; it never proves
       sameness, which is why ``fold_source_health`` refuses an equal-moment
       fold onto a truncated record instead of comparing through it.
    4. A TRUNCATED REASON ALWAYS NAMES A CAUSE. When no complete segment fits
       the budget the earlier version returned ``';trunc:<hex>'`` and nothing
       else: 15 characters, no cause at all, on the branch designed to be loud
       (``unrecognized_status_hint:<hint>`` builds its segment from an
       unvalidated live status string). A hard-cut head of the first non-empty
       segment is kept instead, prefixed ``cut:`` so it can never be read as a
       complete cause. That prefix is reserved out of caller text by
       ``_escaped``, exactly as ``trunc:<hex>`` is, so a bare ``cut:`` segment
       in a stored value is always one this function wrote. Documenting the
       marker without reserving it left one of the two forms defended and the
       other only described.
    5. A HARD CUT LANDS BETWEEN ESCAPE UNITS, never inside one. The head is
       built unit by unit through ``_escape_units`` rather than by a
       fixed-offset slice, so the cut cannot split a ``\\xNN`` into ``\\x0``
       or leave a dangling lone backslash. This is rule 2 one level down,
       inside the escape alphabet: the same defect class, reintroduced once by
       the code that closed rule 4.

    The record stays exactly as frozen: this is a representation rule inside
    the existing ``reason_code`` field, not a new field.

    IDEMPOTENT ON ITS OWN, since it no longer escapes. The ENCODE PATH it
    belongs to is not: ``_compose`` escapes exactly once, on raw caller text.
    Never send a stored reason back through ``_compose``.
    """

    escaped = reason_code
    if len(escaped) <= _MAX_REASON_CODE:
        return escaped
    digest = hashlib.sha256(escaped.encode("utf-8")).hexdigest()
    marker = f"{_TRUNCATION_PREFIX}{digest[:_TRUNCATION_DIGEST_CHARS]}"
    budget = _MAX_REASON_CODE - len(marker)
    segments = escaped.split(";")
    kept: list[str] = []
    used = 0
    for segment in segments:
        cost = len(segment) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(segment)
        used += cost
    if not any(kept):
        head = next((segment for segment in segments if segment), "")
        room = budget - len(_HARD_CUT_PREFIX)
        cut = ""
        for unit in _escape_units(head):
            if len(cut) + len(unit) > room:
                break
            cut += unit
        kept = [f"{_HARD_CUT_PREFIX}{cut}"]
    return ";".join(kept) + marker


def _compose(reason_code: str, note: str) -> str:
    r"""Join the two caller fields into one reason, escaping each one FIRST.

    The order is the whole point. ``_with_note`` separates the two with a bare
    ``;note:``; if the escape ran on the JOINED string, that separator would be
    indistinguishable from a caller segment that simply reads ``note:...``, and
    the two different observations ``("timeout;note:dns", "")`` and
    ``("timeout", "dns")`` would compose to one stored value, so an
    equal-moment fold of the second onto the first returned the first record
    instead of raising. Escaped separately, the caller's ``note:`` segment
    becomes ``\note:`` and only the separator ``_with_note`` appends is bare.
    """

    return _with_note(_escaped(reason_code), _escaped(note))


def _escaped(reason_code: str) -> str:
    """Escape ONE caller field so the stored form is safe and persistable.

    Injective: no two different inputs escape to one output, so applying it can
    never merge two observations. ``;`` is structural and is not escaped; every
    segment is escaped on its own. Call it per field, before composition, and
    exactly once: this is an encoder, not a normalizer.
    """

    return ";".join(_escape_segment(segment) for segment in reason_code.split(";"))


def _escape_segment(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "\\":
            out.append("\\\\")
        elif ord(char) < 32:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    escaped = "".join(out)
    # A real backslash is already doubled above, so a single leading backslash
    # here can only be one this function added: the escape stays reversible.
    # Both structural shapes this module owns are reserved the same way: the
    # whole-segment `trunc:<8 hex>` marker, and the `note:` / `cut:` prefixes.
    if _RESERVED_SEGMENT.fullmatch(escaped) or escaped.startswith(_RESERVED_PREFIXES):
        return "\\" + escaped
    return escaped


def _escape_units(escaped: str) -> tuple[str, ...]:
    r"""Split ALREADY-ESCAPED text into the units a cut may fall between.

    A unit is one character of the original in its escaped form: ``\\`` for a
    backslash, ``\xNN`` for a control character, the lone ``\`` prefixed to a
    reserved segment, or any other single character. Cutting anywhere else
    lands inside an escape and stores a value no decoder can read back, on the
    branch designed to be loud.
    """

    units: list[str] = []
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char == "\\" and index + 1 < len(escaped):
            following = escaped[index + 1]
            if following == "\\":
                units.append("\\\\")
                index += 2
                continue
            if following == "x" and _HEX_PAIR.fullmatch(escaped[index + 2 : index + 4]):
                units.append(escaped[index : index + 4])
                index += 4
                continue
        units.append(char)
        index += 1
    return tuple(units)


def _was_truncated(stored_reason: str) -> bool:
    r"""Whether a stored reason carries the reserved truncation suffix.

    Caller text cannot produce this tail: ``_escaped`` rewrites a matching
    segment to ``\trunc:<hex>``, whose preceding character is a backslash
    rather than the ``;`` this pattern requires.
    """

    return _TRUNCATED_TAIL.search(stored_reason) is not None


def _note_segments(note: str) -> tuple[str, ...]:
    return tuple(segment for segment in note.split(";") if segment)


def _note_marks_partial(note: str) -> bool:
    """Whether the note carries the partial marker as a complete segment.

    Segment-wise, never substring: a note reading ``partially_degraded`` is not
    a partial marker, and must not be read as one.
    """

    return any(
        segment == _PARTIAL_NOTE_MARKER
        or segment.startswith(f"{_PARTIAL_NOTE_MARKER}:")
        for segment in _note_segments(note)
    )


def _mark_partial(reason_code: str) -> str:
    if _note_marks_partial(reason_code):
        return reason_code
    return f"{_PARTIAL_NOTE_MARKER}:{reason_code}" if reason_code else _PARTIAL_NOTE_MARKER


def _with_note(reason_code: str, note: str) -> str:
    r"""Append the result note when it carries a cause the reason lost.

    BOTH ARGUMENTS ARE ALREADY ESCAPED (``_compose`` is the only caller), and
    the ``note:`` segment appended here is the module's own, written LAST and
    left bare. A caller segment reading ``note:...`` was escaped to
    ``\note:...`` on the way in, so the separator cannot be forged.

    Compares COMPLETE ``;``-separated segments. Substring comparison silently
    swallowed a real note: ``query_failures:1`` reads as contained in
    ``query_failures:10`` while meaning something else entirely.
    """

    if not note:
        return reason_code
    if note in _note_segments(reason_code):
        return reason_code
    if reason_code:
        return f"{reason_code};{_NOTE_PREFIX}{note}"
    return f"{_NOTE_PREFIX}{note}"


def _classify(
    health: SourceHealth, age_hours: float | None
) -> tuple[HealthStatus, str, str]:
    """Return the ``(status, reason_code, outcome)`` this run records."""

    live = health.status
    if live in _FROZEN_STATUS_VALUES:
        return _classify_frozen(live, health, age_hours)

    handling = _LEGACY_STATUS_HINTS.get(live)
    if handling is None:
        # Fail closed. An unrecognized hint means this layer does not know what
        # the run actually did, and guessing FRESH is how a degradation became
        # a reset failure counter.
        return HealthStatus.UNAVAILABLE, f"unrecognized_status_hint:{live}", _FAILURE
    if handling == _PARTIAL:
        status = _from_evidence(health, age_hours, degraded_links=False)
        reason = _mark_partial(health.reason_code)
        # Delivery decides the counters (design note, 2026-09-02). The partial
        # signal lives in the reason code and nowhere else.
        return status, reason, _outcome(health)
    return _classify_frozen("", health, age_hours)


def _classify_frozen(
    live: str, health: SourceHealth, age_hours: float | None
) -> tuple[HealthStatus, str, str]:
    """Apply the precedence documented at the top of this module."""

    if live == _DISABLED:
        # Never polled: neither a success nor a failure.
        return HealthStatus.DISABLED, health.reason_code, _NEITHER
    if live == _TRANSPORT_FAILURE:
        return HealthStatus.UNAVAILABLE, health.reason_code, _outcome(health)
    if live == _PARSE_FAILURE:
        return HealthStatus.MALFORMED, health.reason_code, _outcome(health)
    status = _from_evidence(health, age_hours, degraded_links=live == _DEGRADED_LINKS)
    return status, health.reason_code, _outcome(health)


def _outcome(health: SourceHealth) -> str:
    return _SUCCESS if health.usable_items > 0 else _FAILURE


def _from_evidence(
    health: SourceHealth, age_hours: float | None, *, degraded_links: bool
) -> HealthStatus:
    if health.usable_items <= 0 or age_hours is None:
        return HealthStatus.EMPTY
    if age_hours > health.max_age_hours:
        return HealthStatus.STALE
    if degraded_links:
        return HealthStatus.LINK_RESOLUTION_DEGRADED
    return HealthStatus.FRESH
