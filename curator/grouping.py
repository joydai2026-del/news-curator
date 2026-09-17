"""Cheap, exact pre-filter for same-event grouping. No heuristics live here.

Round-2 review measured what the token-and-number heuristic actually did: it
merged sixty unrelated stories through union-find chaining, it could not fire at
all on the shipped Chinese sources (0 of 5 real fixture items reach two shared
Latin tokens), and a wrong merge silently deletes a story from the very lane
this feature exists to fill. So the heuristic is gone.

What remains is only what is certain: two stories are the same event when they
share a canonical URL or an identical normalized title. Everything else is
decided by the translation model in `curator.translation.pairing`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from .dedup import normalize_title


@dataclass(frozen=True)
class GroupingPolicy:
    """Validated pre-filter policy. The pairing window lives in translation."""

    cross_language_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.cross_language_enabled, bool):
            raise ValueError("grouping.cross_language_enabled must be a boolean")

    @classmethod
    def from_config(cls, grouping: Mapping[str, object]) -> "GroupingPolicy":
        return cls(cross_language_enabled=bool(grouping.get("cross_language_enabled", True)))


@dataclass(frozen=True)
class GroupingCandidate:
    story_id: str
    language: str
    title: str
    summary: str
    published_at: datetime
    canonical_url: str = ""
    category_ids: tuple[str, ...] = ()
    # Carried when the candidate was read back from the corpus. An id a story
    # already has is authoritative and is propagated, never recomputed.
    event_group_id: str | None = None


def event_group_id_for(story_id: str) -> str:
    """Stable id derived from ONE story id, so it never depends on batch order."""

    return "group:" + hashlib.sha256(story_id.encode("utf-8")).hexdigest()[:32]


def exact_matches(
    candidates: Sequence[GroupingCandidate] | Iterable[GroupingCandidate],
    *,
    policy: GroupingPolicy | None = None,
) -> dict[str, str]:
    """Return ``story_id -> event_group_id`` for CERTAIN cross-language matches.

    Certain means the same canonical URL or the same normalized title. The id is
    derived from the lowest-sorting member of the pair, and an id a member
    already carries wins, so a later run never renames an existing group.
    """

    active = policy or GroupingPolicy()
    rows = list(candidates)
    if not active.cross_language_enabled or len(rows) < 2:
        return {}
    assigned: dict[str, str] = {}
    buckets: dict[tuple[str, str], list[GroupingCandidate]] = {}
    for row in rows:
        if row.canonical_url:
            buckets.setdefault(("url", row.canonical_url), []).append(row)
        normalized = normalize_title(row.title)
        if normalized:
            buckets.setdefault(("title", normalized), []).append(row)
    for members in buckets.values():
        if len(members) < 2 or len({member.language for member in members}) < 2:
            continue
        existing = sorted({member.event_group_id for member in members if member.event_group_id})
        group = existing[0] if existing else event_group_id_for(min(member.story_id for member in members))
        for member in members:
            assigned[member.story_id] = group
    return assigned
