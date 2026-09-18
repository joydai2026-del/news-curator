"""The candidate recipe: four labeled pools with quotas, replacing "50 newest".

This is the product. The ranking model only reorders what this hands it, so the
mix a reader sees is decided here, from config, and is replayable: given the same
rows, profile, policy and clock, the same window comes out in the same order.

Pools, and what each one means to the reader:

    updates  "fresh"     brand new
    hot      "hot"       enough independent outlets are carrying it
    interested "for you" matches what she actually reads
    surprise "surprise"  deliberately outside her profile, still quality-gated

A story is assigned ONE primary lane: the first lane in the configured
lane_priority it is eligible for. Only the primary lane consumes quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .composition import CompositionPolicy
from .profile import BehaviorProfile


@dataclass(frozen=True)
class LanedCandidate:
    story_id: str
    lane: str
    lane_score: float
    row: Mapping[str, object]


def _age_hours(row: Mapping[str, object], now: datetime) -> float:
    published = row.get("published_at")
    if not isinstance(published, str) or not published:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


def _categories(row: Mapping[str, object]) -> tuple[str, ...]:
    value = row.get("category_ids")
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _independent_sources(row: Mapping[str, object]) -> int:
    value = row.get("independent_source_count")
    # Absent means "the corpus has not counted coverage for this story", which is
    # zero independent outlets, never an assumed one.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def assign_lane(row: Mapping[str, object], *, profile: BehaviorProfile, policy: CompositionPolicy,
                now: datetime) -> tuple[str, float]:
    """Return the story's primary lane and its score inside that lane."""
    age = _age_hours(row, now)
    independent = _independent_sources(row)
    categories = _categories(row)
    source_id = str(row.get("source_id", ""))
    affinity = profile.affinity(source_id=source_id, category_ids=categories)
    is_aggregator = bool(row.get("source_is_aggregator"))

    eligible: dict[str, float] = {}
    if age <= policy.updates_max_age_hours:
        eligible["updates"] = -age
    if independent >= policy.trend_min_independent_sources and age <= policy.trend_window_hours:
        eligible["hot"] = float(independent)
    if affinity > 0:
        eligible["interested"] = affinity
    # Surprise needs a profile to be outside of. With no profile every story
    # would be "off profile", which would turn exploration into the whole feed.
    if (not profile.is_empty and affinity <= 0 and age <= policy.exploration_max_age_hours
            and independent >= policy.exploration_min_independent_sources
            and not (policy.exploration_require_non_aggregator and is_aggregator)):
        eligible["surprise"] = -age
    for lane in policy.lane_priority:
        if lane in eligible:
            return lane, eligible[lane]
    # Eligible for nothing: it is still a story, and the freshest of them is the
    # honest fallback. It keeps its "fresh" label, so the reader is never shown
    # an unlabeled card.
    return "updates", -age


def lane_window_quotas(policy: CompositionPolicy, size: int) -> dict[str, int]:
    """Quotas that sum to exactly ``size``; the remainder goes by lane priority."""
    quotas = {lane: int(size * policy.lane_ratios[lane]) for lane in policy.lane_priority}
    remainder = size - sum(quotas.values())
    order = sorted(policy.lane_priority,
                   key=lambda lane: (-(size * policy.lane_ratios[lane] - quotas[lane]),
                                     policy.lane_priority.index(lane)))
    for lane in order[:max(0, remainder)]:
        quotas[lane] += 1
    return quotas


def build_window(rows: Sequence[Mapping[str, object]], *, profile: BehaviorProfile,
                 policy: CompositionPolicy, now: datetime, size: int | None = None
                 ) -> tuple[LanedCandidate, ...]:
    """Assemble the candidate window the ranker will reorder.

    Per-source and per-aggregator caps apply across the WHOLE window, so no
    single firehose route can fill the page no matter which lane it lands in.
    A lane whose pool runs out ships short: there is no cross-lane borrow here,
    because borrowing at this stage is what silently deletes the variety.
    """
    window_size = policy.candidate_window_size if size is None else size
    laned = [LanedCandidate(str(row["story_id"]), *assign_lane(row, profile=profile, policy=policy, now=now), row)
             for row in rows if row.get("story_id")]
    pools: dict[str, list[LanedCandidate]] = {lane: [] for lane in policy.lane_priority}
    for candidate in laned:
        pools[candidate.lane].append(candidate)
    for lane in pools:
        pools[lane].sort(key=lambda item: (-item.lane_score, item.story_id))

    quotas = lane_window_quotas(policy, window_size)
    per_source: dict[str, int] = {}
    chosen: list[LanedCandidate] = []
    seen: set[str] = set()
    for lane in policy.lane_priority:
        taken = 0
        for candidate in pools[lane]:
            if taken >= quotas[lane] or len(chosen) >= window_size:
                break
            if candidate.story_id in seen:
                continue
            source_id = str(candidate.row.get("source_id", ""))
            cap = (policy.per_aggregator_cap_per_window if candidate.row.get("source_is_aggregator")
                   else policy.per_source_cap_per_window)
            if per_source.get(source_id, 0) >= cap:
                continue
            per_source[source_id] = per_source.get(source_id, 0) + 1
            seen.add(candidate.story_id)
            chosen.append(candidate)
            taken += 1
    priority = {lane: index for index, lane in enumerate(policy.lane_priority)}
    chosen.sort(key=lambda item: (priority[item.lane], -item.lane_score, item.story_id))
    return tuple(chosen)
