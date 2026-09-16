"""Cross-language same-event grouping, tier 1 only (deterministic key).

Tier 1 is deliberately high precision and low recall: two stories in different
languages join a group only when they share enough Latin entity tokens AND
carry identical, non-empty number sets inside one window. A wrong merge hides
a story, which is exactly the failure this feature exists to fix, so recall is
the side that is allowed to suffer. Tier 2 (bounded embedding similarity) is
out of Phase 1 scope and is not stubbed here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Sequence

from .dedup import numbers_in
from .normalize import fold_text


_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'’-]{2,}")
_MIN_TOKEN_CHARS = 3


@dataclass(frozen=True)
class GroupingPolicy:
    """Validated tier-1 policy. Every value is configurable, none is hardcoded."""

    cross_language_enabled: bool = True
    min_shared_entity_tokens: int = 2
    window_hours: int = 24

    def __post_init__(self) -> None:
        if not isinstance(self.cross_language_enabled, bool):
            raise ValueError("grouping.cross_language_enabled must be a boolean")
        for label, value, low, high in (
            ("grouping.min_shared_entity_tokens", self.min_shared_entity_tokens, 1, 10),
            ("trend.window_hours", self.window_hours, 1, 72),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{label} must be an integer in [{low}, {high}]")

    @classmethod
    def from_config(cls, grouping: Mapping[str, object], trend: Mapping[str, object]) -> "GroupingPolicy":
        return cls(
            cross_language_enabled=bool(grouping.get("cross_language_enabled", True)),
            min_shared_entity_tokens=int(grouping.get("min_shared_entity_tokens", 2)),
            window_hours=int(trend.get("window_hours", 24)),
        )


@dataclass(frozen=True)
class GroupingCandidate:
    story_id: str
    language: str
    title: str
    summary: str
    published_at: datetime


def entity_tokens(title: str, summary: str = "") -> frozenset[str]:
    """Latin tokens of at least three characters, case folded."""

    folded = fold_text(f"{title} {summary}")
    return frozenset(
        token.casefold() for token in _LATIN_TOKEN.findall(folded) if len(token) >= _MIN_TOKEN_CHARS
    )


def number_key(title: str, summary: str = "") -> frozenset[str]:
    return frozenset(numbers_in(f"{title} {summary}"))


def assign_event_groups(
    candidates: Sequence[GroupingCandidate] | Iterable[GroupingCandidate],
    *,
    policy: GroupingPolicy | None = None,
) -> dict[str, str]:
    """Return ``story_id -> event_group_id`` for stories that joined a group.

    Only cross-language pairs are considered: same-language duplicates are
    already handled upstream by ``curator.dedup``. Stories that join nothing are
    absent from the mapping, so a single-member group is never given an id.
    """

    active = policy or GroupingPolicy()
    rows = list(candidates)
    if not active.cross_language_enabled or len(rows) < 2:
        return {}
    window = timedelta(hours=active.window_hours)
    parent: dict[str, str] = {row.story_id: row.story_id for row in rows}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            # Smaller story id wins so the derived group id is order independent.
            if b < a:
                a, b = b, a
            parent[b] = a

    # The identical non-empty number set is the bucketing key, so the pairwise
    # token comparison stays linear in practice instead of quadratic overall.
    buckets: dict[frozenset[str], list[tuple[GroupingCandidate, frozenset[str]]]] = {}
    for row in rows:
        numbers = number_key(row.title, row.summary)
        if not numbers:
            # An identical EMPTY number set is not evidence of the same event.
            continue
        buckets.setdefault(numbers, []).append((row, entity_tokens(row.title, row.summary)))

    for members in buckets.values():
        for index, (left, left_tokens) in enumerate(members):
            for right, right_tokens in members[index + 1:]:
                if left.language == right.language:
                    continue
                if abs(left.published_at - right.published_at) > window:
                    continue
                if len(left_tokens & right_tokens) < active.min_shared_entity_tokens:
                    continue
                union(left.story_id, right.story_id)

    sizes: dict[str, int] = {}
    for row in rows:
        sizes[find(row.story_id)] = sizes.get(find(row.story_id), 0) + 1
    return {
        row.story_id: event_group_id(find(row.story_id))
        for row in rows
        if sizes[find(row.story_id)] > 1
    }


def event_group_id(root_story_id: str) -> str:
    """Derived from the lowest member story id, so it is stable per member set."""

    return "group:" + hashlib.sha256(root_story_id.encode("utf-8")).hexdigest()[:32]
