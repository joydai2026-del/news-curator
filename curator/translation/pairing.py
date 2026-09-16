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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

from curator.grouping import GroupingCandidate, event_group_id_for


PAIRING_POLICY_ID = "pairing-json-v1"
MATCH = "match"
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
    model: str = ""
    policy_id: str = PAIRING_POLICY_ID

    def __post_init__(self) -> None:
        for label, value, low, high in (
            ("translation.pairing_window_hours", self.window_hours, 1, 168),
            ("translation.pairing_max_context_titles", self.max_context_titles, 1, 500),
            ("translation.pairing_daily_call_limit", self.daily_call_limit, 0, 5_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{label} must be an integer in [{low}, {high}]")

    @classmethod
    def from_config(cls, translation: Mapping[str, object]) -> "PairingPolicy":
        return cls(
            window_hours=int(translation.get("pairing_window_hours", 48)),
            max_context_titles=int(translation.get("pairing_max_context_titles", 60)),
            daily_call_limit=int(translation.get("pairing_daily_call_limit", 600)),
            model=str(translation.get("model") or ""),
        )


@dataclass(frozen=True)
class ExclusivityDecision:
    """One persisted decision. `match_story_id` None means language exclusive."""

    story_id: str
    decided_at: datetime
    model: str
    policy_id: str
    match_story_id: str | None

    @property
    def outcome(self) -> str:
        return MATCH if self.match_story_id else EXCLUSIVE

    def as_dict(self) -> dict[str, object]:
        return {"story_id": self.story_id, "decided_at": self.decided_at.isoformat(),
                "model": self.model, "policy_id": self.policy_id,
                "match_story_id": self.match_story_id}


@dataclass
class PairingResult:
    decisions: dict[str, ExclusivityDecision] = field(default_factory=dict)
    group_ids: dict[str, str] = field(default_factory=dict)
    undecided: set[str] = field(default_factory=set)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

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
    call_budget: int | None = None,
) -> PairingResult:
    """Ask once per undecided story, reuse every decision already on record."""

    result = PairingResult()
    decided = dict(already_decided or {})
    grouped = dict(prefilter or {})
    window = timedelta(hours=policy.window_hours)
    remaining = policy.daily_call_limit if call_budget is None else min(call_budget, policy.daily_call_limit)

    # Context is every display-language story in the window, whether it arrived
    # in THIS batch or in an earlier run. Restricting it to the corpus would
    # make a same-batch pair undecidable for no reason.
    pool_by_id = {}
    for row in list(corpus) + list(stories):
        if row.language != display_language or now - row.published_at > window:
            continue
        pool_by_id.setdefault(row.story_id, row)
    display_pool = list(pool_by_id.values())

    for story in stories:
        if story.language == display_language:
            continue
        if story.story_id in grouped:
            # The exact pre-filter already decided this one, for free.
            result.group_ids[story.story_id] = grouped[story.story_id]
            continue
        prior = decided.get(story.story_id)
        if prior is not None:
            # Decided once, in an earlier run. Never re-asked, never re-billed.
            result.decisions[story.story_id] = prior
            if prior.match_story_id:
                result.group_ids[story.story_id] = event_group_id_for(prior.match_story_id)
            continue
        context = _context_for(story, display_pool, policy=policy, window=window, now=now)
        if not context:
            # Nothing to compare against is not evidence of exclusivity.
            result.undecided.add(story.story_id)
            continue
        if remaining <= 0:
            result.undecided.add(story.story_id)
            continue
        remaining -= 1
        try:
            index, input_tokens, output_tokens = provider.decide(story=story, context=context)
        except Exception:
            result.undecided.add(story.story_id)
            continue
        result.calls += 1
        result.input_tokens += max(0, int(input_tokens or 0))
        result.output_tokens += max(0, int(output_tokens or 0))
        if index is None:
            decision = ExclusivityDecision(story_id=story.story_id, decided_at=now,
                                           model=policy.model, policy_id=policy.policy_id,
                                           match_story_id=None)
            result.decisions[story.story_id] = decision
            continue
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(context):
            result.undecided.add(story.story_id)
            continue
        matched = context[index]
        decision = ExclusivityDecision(story_id=story.story_id, decided_at=now,
                                       model=policy.model, policy_id=policy.policy_id,
                                       match_story_id=matched.story_id)
        result.decisions[story.story_id] = decision
        # Derived from the MATCHED story, so the id is identical in every run
        # and an existing group on that story is what new members join.
        result.group_ids[story.story_id] = matched.event_group_id or event_group_id_for(matched.story_id)
    return result


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
