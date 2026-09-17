"""The model decides language exclusivity, one small question per story.

A token heuristic cannot tell whether a Chinese story is the same event as an
English one: the round-2 measurement found it fired on nothing real and merged
what it should not. So the question goes to the model that is already
configured for translation, and the ANSWER IS PERSISTED, so a story is decided
once and every later run reuses that decision rather than re-deciding it.

Three outcomes, and the third is deliberate:
  match     -> the story joins the matched display-language story's group
  no match  -> the story is language exclusive and may be translated
  undecided -> nothing is claimed: not translated, and not shown in the section
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

from curator.grouping import GroupingCandidate, event_group_id_for
from curator.sources import SafeTransportError

from .base import TranslationProviderError
from .store import TranslationStoreError

# A provider or transport failure is "undecided". A TypeError in our own code is
# a defect and must reach the caller, not become a silent non-answer.
PAIRING_TRANSIENT_ERRORS = (TranslationProviderError, TranslationStoreError, SafeTransportError)


PAIRING_POLICY_ID = "pairing-json-v1"
MATCH = "matched"
EXCLUSIVE = "exclusive"
UNDECIDED = "undecided"
_SUMMARY_CONTEXT_CHARS = 200

_SYSTEM_PROMPT = (
    "You decide whether a news story has already been reported by another outlet. "
    "You are given one story and a numbered list of candidate stories in another language. "
    "Answer with the index of the candidate that reports THE SAME EVENT, or null when none does. "
    "The same company or the same topic is not the same event. "
    'Reply with one JSON object with exactly the field "match_index", whose value is an '
    "integer index from the list or null. No other field, no prose."
)


@dataclass(frozen=True)
class PairingPolicy:
    """Every operational value is a validated `translation.*` key."""

    window_hours: int = 48
    max_context_titles: int = 60
    daily_call_limit: int = 600
    max_attempts: int = 2
    recheck_hours: int = 6
    # ONE RUN's share of the work, so an hourly job that finds a backlog of
    # hundreds of undecided stories stops on its own terms instead of being
    # cancelled by the job timeout with the run's writes lost. Progress is not
    # thrown away: every decision is persisted as it is made and the next run
    # resumes with what is still undecided.
    max_calls_per_run: int = 40
    run_time_budget_seconds: int = 300
    model: str = ""
    policy_id: str = PAIRING_POLICY_ID

    def __post_init__(self) -> None:
        for label, value, low, high in (
            ("translation.pairing_window_hours", self.window_hours, 1, 168),
            ("translation.pairing_max_context_titles", self.max_context_titles, 1, 500),
            ("translation.pairing_daily_call_limit", self.daily_call_limit, 0, 5_000),
            ("translation.pairing_max_attempts", self.max_attempts, 1, 10),
            ("translation.pairing_recheck_hours", self.recheck_hours, 1, 48),
            ("translation.pairing_max_calls_per_run", self.max_calls_per_run, 0, 500),
            ("translation.run_time_budget_seconds", self.run_time_budget_seconds, 30, 780),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{label} must be an integer in [{low}, {high}]")

    @classmethod
    def from_config(cls, translation: Mapping[str, object]) -> "PairingPolicy":
        return cls(
            window_hours=int(translation.get("pairing_window_hours", 48)),
            max_context_titles=int(translation.get("pairing_max_context_titles", 60)),
            daily_call_limit=int(translation.get("pairing_daily_call_limit", 600)),
            max_attempts=int(translation.get("pairing_max_attempts", 2)),
            recheck_hours=int(translation.get("pairing_recheck_hours", 6)),
            max_calls_per_run=int(translation.get("pairing_max_calls_per_run", 40)),
            run_time_budget_seconds=int(translation.get("run_time_budget_seconds", 300)),
            model=str(translation.get("model") or ""),
            # One source of truth for the policy id: the reader reads the same
            # value, and a mismatch makes the section silently empty.
            policy_id=str(translation.get("pairing_policy_id") or PAIRING_POLICY_ID),
        )


@dataclass(frozen=True)
class ExclusivityDecision:
    """One persisted decision, keyed by (story, display language, policy)."""

    story_id: str
    decided_at: datetime
    model: str
    policy_id: str
    match_story_id: str | None
    outcome: str = EXCLUSIVE
    display_language: str = "en"
    attempts: int = 1
    retry_after: datetime | None = None
    rechecked_at: datetime | None = None

    @property
    def settled(self) -> bool:
        return self.outcome in (EXCLUSIVE, MATCH)

    def as_dict(self) -> dict[str, object]:
        return {"story_id": self.story_id, "display_language": self.display_language,
                "decided_at": self.decided_at.isoformat(), "model": self.model,
                "policy_id": self.policy_id, "outcome": self.outcome,
                "match_story_id": self.match_story_id, "attempts": self.attempts,
                "retry_after": self.retry_after.isoformat() if self.retry_after else None}


@dataclass(frozen=True)
class PairingCost:
    """Pairing is a paid call. Its price comes from the same config keys."""

    input_cost_per_million_tokens_usd: float = 0.25
    output_cost_per_million_tokens_usd: float = 2.0
    characters_per_token: int = 4
    # Must equal the cap the REQUEST sends, or every call under-reserves.
    output_allowance_tokens: int = 64

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_cost_per_million_tokens_usd
                + output_tokens * self.output_cost_per_million_tokens_usd) / 1_000_000

    def estimate_usd(self, question: Mapping[str, object]) -> float:
        characters = len(json.dumps(question, ensure_ascii=False)) + len(_SYSTEM_PROMPT)
        input_tokens = -(-characters // max(1, self.characters_per_token))
        return self.cost_usd(input_tokens, self.output_allowance_tokens)


@dataclass
class PairingResult:
    decisions: dict[str, ExclusivityDecision] = field(default_factory=dict)
    group_ids: dict[str, str] = field(default_factory=dict)
    matched_pairs: dict[str, str] = field(default_factory=dict)
    undecided: set[str] = field(default_factory=set)
    pending: list = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    budget_refusals: int = 0
    persistence_failures: int = 0
    # Every ATTEMPT at a paid call, counted at the reservation rather than at the
    # answer: a refused reservation and a provider failure each cost a round
    # trip, so both are what the per-run bound counts.
    attempted_calls: int = 0
    # Set when a bound stopped the loop: "calls", "time" or "daily_cap".
    budget_stop: str | None = None
    # Stories this run did not ask about because the bound was already reached.
    budget_skipped: int = 0
    elapsed_seconds: float = 0.0

    @property
    def exclusive_story_ids(self) -> tuple[str, ...]:
        return tuple(sorted(story_id for story_id, decision in self.decisions.items()
                            if decision.outcome == EXCLUSIVE))


class PairingProvider(Protocol):
    provider_id: str
    model_version: str

    def decide(self, *, story: GroupingCandidate,
               context: Sequence[GroupingCandidate]) -> tuple[int | None, int, int]:
        """Return (match index or None, input tokens, output tokens).

        Raising is an UNDECIDED outcome, never an exclusivity claim.
        """


class PairingLedger(Protocol):
    """Reserves one pairing call against the persisted UTC-day budget."""

    def reserve_call(self, amount_usd: float) -> bool: ...
    def settle_call(self, reserved_usd: float, settled_usd: float) -> None: ...


def decide_exclusivity(
    stories: Sequence[GroupingCandidate],
    corpus: Sequence[GroupingCandidate],
    *,
    display_language: str,
    policy: PairingPolicy,
    provider: PairingProvider,
    now: datetime,
    already_decided: Mapping[str, ExclusivityDecision] | None = None,
    prefilter: Mapping[str, str] | None = None,
    ledger: PairingLedger | None = None,
    persist=None,
    recheck=None,
    cost: "PairingCost | None" = None,
    monotonic=time.monotonic,
    started: float | None = None,
) -> PairingResult:
    """Ask once per undecided story, reuse every decision already on record.

    Three rules keep the answer honest over time:
      * a decision from a DIFFERENT policy id is ignored (the caller filters),
      * an UNDECIDED answer is re-asked at most `max_attempts` times, and
      * an EXCLUSIVE answer is re-checked once, after `recheck_hours`, when new
        display-language stories have arrived in the same categories since it
        was made. English coverage routinely lags the Chinese wire by hours, so
        the first look is the wrong moment to decide for ever.

    A run also stops on its own bound (`max_calls_per_run`, `run_time_budget_seconds`,
    or the first refusal from the persisted daily ledger). Reaching one is not a
    failure: every decision already made is persisted, the stories not reached
    stay undecided and claim nothing, and the next run asks about them. Before
    this bound existed, a backlog of hundreds of stories ran the job past its
    timeout and the whole run, corpus write included, was lost.

    `stories` is the work QUEUE, not only the current fetch. The ingest merges
    the retained batch with the undecided other-language rows already in the
    corpus window, or a story skipped for budget would be re-asked only while it
    happened to stay in the feed.
    """

    result = PairingResult()
    decided = dict(already_decided or {})
    grouped = dict(prefilter or {})
    window = timedelta(hours=policy.window_hours)
    pricing = cost or PairingCost()
    remaining = policy.daily_call_limit

    pool_by_id = {}
    for row in list(corpus) + list(stories):
        if row.language != display_language or now - row.published_at > window:
            continue
        pool_by_id.setdefault(row.story_id, row)
    display_pool = list(pool_by_id.values())

    # The clock belongs to the RUN, not to this loop: the ingest passes the value
    # it read at entry, so the corpus read-back that precedes pairing spends the
    # same budget. Defaulting to now() keeps a direct caller honest.
    started = monotonic() if started is None else started
    # Newest first, and everything inside the pairing window before anything
    # older, so the section a reader is looking at fills before the tail does.
    ordered = sorted(stories, key=lambda row: (now - row.published_at > window,
                                               -row.published_at.timestamp(), row.story_id))
    for story in ordered:
        if story.language == display_language:
            continue
        if story.story_id in grouped:
            result.group_ids[story.story_id] = grouped[story.story_id]
            continue
        prior = decided.get(story.story_id)
        rechecking = prior is not None and prior.outcome == EXCLUSIVE and _needs_asking(
            prior, story, display_pool, policy=policy, now=now)
        if prior is not None and not _needs_asking(prior, story, display_pool, policy=policy, now=now):
            result.decisions[story.story_id] = prior
            if prior.outcome == MATCH and prior.match_story_id:
                result.group_ids[story.story_id] = event_group_id_for(prior.match_story_id)
                result.matched_pairs[story.story_id] = prior.match_story_id
            elif prior.outcome == UNDECIDED:
                result.undecided.add(story.story_id)
            continue
        context = _context_for(story, display_pool, policy=policy, window=window, now=now)
        if not context:
            result.undecided.add(story.story_id)
            continue
        if remaining <= 0:
            result.undecided.add(story.story_id)
            continue
        if result.budget_stop is None:
            if result.attempted_calls >= policy.max_calls_per_run:
                result.budget_stop = "calls"
            elif monotonic() - started >= policy.run_time_budget_seconds:
                result.budget_stop = "time"
        if result.budget_stop is not None:
            # Out of this run's share. Nothing is claimed and nothing is
            # persisted for this story: it was never asked, so it keeps its
            # attempt count and the next run asks it first.
            result.budget_skipped += 1
            result.undecided.add(story.story_id)
            continue
        estimate = pricing.estimate_usd(build_question(story, context))
        result.attempted_calls += 1
        if ledger is not None and not ledger.reserve_call(estimate):
            # The DAY's budget is spent, not this run's share, and that ledger is
            # persisted: every later story, in this run and in every later run
            # today, gets the same answer. Asking again is one Supabase round
            # trip per story for the rest of the day, so this refusal is
            # terminal and the rest of the batch is skipped without asking.
            result.budget_refusals += 1
            result.budget_stop = "daily_cap"
            result.budget_skipped += 1
            result.undecided.add(story.story_id)
            continue
        remaining -= 1
        try:
            index, input_tokens, output_tokens = provider.decide(story=story, context=context)
        except PAIRING_TRANSIENT_ERRORS:
            # A provider failure is undecided, and it still cost money.
            if ledger is not None:
                ledger.settle_call(estimate, estimate)
            result.undecided.add(story.story_id)
            _persist_non_answer(result, persist, recheck, story, policy, display_language, prior, now,
                                rechecking=rechecking)
            continue
        result.calls += 1
        result.input_tokens += max(0, int(input_tokens or 0))
        result.output_tokens += max(0, int(output_tokens or 0))
        if ledger is not None:
            observed = pricing.cost_usd(int(input_tokens or 0), int(output_tokens or 0))
            ledger.settle_call(estimate, observed if observed > 0 else estimate)
        if index is None:
            decision = ExclusivityDecision(story_id=story.story_id, decided_at=now, model=policy.model,
                                           policy_id=policy.policy_id, match_story_id=None,
                                           outcome=EXCLUSIVE, display_language=display_language,
                                           rechecked_at=now if rechecking else None)
            stored = (_recheck(result, recheck, decision) if rechecking
                      else (decision if _record(result, persist, decision) else None))
            if stored is not None and stored.outcome == EXCLUSIVE:
                result.decisions[story.story_id] = stored
            elif stored is not None:
                # The store says this story is no longer exclusive. Believe it.
                result.decisions[story.story_id] = stored
                if stored.match_story_id:
                    result.group_ids[story.story_id] = event_group_id_for(stored.match_story_id)
            else:
                # Not persisted means not trusted: it cannot drive money or
                # visibility this run, and it will be asked again next run.
                result.undecided.add(story.story_id)
            continue
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(context):
            result.undecided.add(story.story_id)
            _persist_non_answer(result, persist, recheck, story, policy, display_language, prior, now,
                                rechecking=rechecking)
            continue
        matched = context[index]
        decision = ExclusivityDecision(story_id=story.story_id, decided_at=now, model=policy.model,
                                       policy_id=policy.policy_id, match_story_id=matched.story_id,
                                       outcome=MATCH, display_language=display_language,
                                       rechecked_at=now if rechecking else None)
        # A recheck must be able to CHANGE the answer, and the persisted row is
        # the answer. The attempted write is not evidence of anything.
        stored = (_recheck(result, recheck, decision) if rechecking
                  else (decision if _record(result, persist, decision) else None))
        if stored is None:
            result.undecided.add(story.story_id)
            continue
        if stored.outcome != MATCH:
            # The store kept the older answer, so the lane keeps it too.
            result.decisions[story.story_id] = stored
            continue
        result.decisions[story.story_id] = stored
        group = matched.event_group_id or event_group_id_for(matched.story_id)
        result.group_ids[story.story_id] = group
        # BOTH rows carry the group. Writing only the foreign one left the peer
        # at NULL, which is what made a matched story look exclusive.
        result.group_ids[matched.story_id] = group
        result.matched_pairs[story.story_id] = matched.story_id
    result.elapsed_seconds = monotonic() - started
    return result


def _persist_non_answer(result, persist, recheck, story, policy, display_language, prior, now, *, rechecking):
    """Record that we ASKED and got nothing usable.

    On a first look that is an `undecided` row with an attempt count. On a
    RE-check it must still stamp `rechecked_at`, or a story whose provider keeps
    failing is re-asked on every run for the rest of the window: 504 paid calls
    for one story at twelve runs an hour, which is what this bound exists to
    stop. The decision itself stays exclusive; only the "we looked" mark moves.
    """

    decision = _undecided_decision(story, policy, display_language, prior, now)
    if not rechecking:
        _record(result, persist, decision)
        return
    _recheck(result, recheck, replace(decision, outcome=UNDECIDED, rechecked_at=now))


def _undecided_decision(story, policy, display_language, prior, now):
    attempts = (prior.attempts if prior else 0) + 1
    return ExclusivityDecision(
        story_id=story.story_id, decided_at=now, model=policy.model, policy_id=policy.policy_id,
        match_story_id=None, outcome=UNDECIDED, display_language=display_language,
        attempts=attempts, retry_after=now + timedelta(hours=policy.recheck_hours))


def _recheck(result, recheck, decision):
    """Persist a re-check and return the PERSISTED decision, or None.

    The RPC refuses a second re-check and refuses to move a matched decision, so
    what it returns is what the lane will serve, whatever we attempted.
    """

    if recheck is None:
        result.pending.append(decision)
        return decision
    try:
        stored = recheck(decision)
    except PAIRING_TRANSIENT_ERRORS:
        # Store and transport failures only. A TypeError in the callable is our
        # bug and must surface, the same rule the ingest boundary states.
        result.persistence_failures += 1
        return None
    if stored is None:
        result.persistence_failures += 1
        return None
    result.pending.append(stored)
    return stored


def _record(result, persist, decision) -> bool:
    """Persist BEFORE the decision is allowed to matter. False means undecided."""

    if persist is None:
        result.pending.append(decision)
        return True
    try:
        persist(decision)
    except PAIRING_TRANSIENT_ERRORS:
        result.persistence_failures += 1
        return False
    result.pending.append(decision)
    return True


def _needs_asking(prior: ExclusivityDecision, story, display_pool, *, policy, now) -> bool:
    if prior.policy_id != policy.policy_id:
        return True
    if prior.outcome == UNDECIDED:
        if prior.attempts >= policy.max_attempts:
            return False
        return prior.retry_after is None or now >= prior.retry_after
    if prior.outcome == MATCH:
        return False
    # EXCLUSIVE: re-check once, after the recheck window, and only when a
    # display-language story in the same categories arrived since the decision.
    if prior.rechecked_at is not None:
        return False
    if now - prior.decided_at < timedelta(hours=policy.recheck_hours):
        return False
    if now - story.published_at > timedelta(hours=policy.window_hours):
        return False
    categories = set(story.category_ids)
    return any(row.published_at > prior.decided_at
               and (not categories or not row.category_ids or categories & set(row.category_ids))
               for row in display_pool)


def _context_for(story, display_pool, *, policy, window, now):
    categories = set(story.category_ids)
    rows = [row for row in display_pool
            if abs(row.published_at - story.published_at) <= window
            and (not categories or not row.category_ids or categories & set(row.category_ids))]
    rows.sort(key=lambda row: (abs(row.published_at - story.published_at), row.story_id))
    return rows[: policy.max_context_titles]


def build_question(story: GroupingCandidate, context: Sequence[GroupingCandidate]) -> dict[str, object]:
    """The exact payload sent. Titles and a short summary prefix only."""

    return {
        "story": {"title": story.title, "summary": story.summary[:_SUMMARY_CONTEXT_CHARS]},
        "candidates": [{"index": index, "title": row.title,
                        "summary": row.summary[:_SUMMARY_CONTEXT_CHARS]}
                       for index, row in enumerate(context)],
    }


def parse_match_index(content: str, *, context_size: int) -> int | None | str:
    """Strict JSON. Anything else is UNDECIDED, never an exclusivity claim."""

    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return UNDECIDED
    if not isinstance(parsed, Mapping) or set(parsed) != {"match_index"}:
        return UNDECIDED
    value = parsed["match_index"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < context_size:
        return UNDECIDED
    return value


SYSTEM_PROMPT = _SYSTEM_PROMPT
