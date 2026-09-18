"""The hard diversity pass: duplicate removal, spacing, quotas, calibration.

Deliberately NOT delegated to the ranking model. An unconstrained LLM reranker
has been measured amplifying what it was fed; diversity that matters is enforced
as a deterministic post-pass over the model's order, where it can be asserted.

Pure function of (scored order, lane labels, config, owner states). No clock, no
randomness, so a replay of the same inputs produces byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from curator.dedup import normalize_title

from .composition import CompositionPolicy
from .profile import BehaviorProfile
from .recipe import LanedCandidate


@dataclass(frozen=True)
class FinalizedPage:
    cards: tuple[LanedCandidate, ...]
    # story_id -> the outlets whose duplicate rows collapsed into this one.
    also_covered_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # One entry per lane that could not reach its quota. Recorded, never silent.
    short_lane_reasons: tuple[Mapping[str, object], ...] = ()
    calibration_kl: float | None = None
    calibration_alarm: bool = False


def page_quotas(policy: CompositionPolicy, size: int) -> dict[str, int]:
    quotas = {lane: int(size * policy.lane_ratios[lane]) for lane in policy.lane_priority}
    remainder = size - sum(quotas.values())
    order = sorted(policy.lane_priority,
                   key=lambda lane: (-(size * policy.lane_ratios[lane] - quotas[lane]),
                                     policy.lane_priority.index(lane)))
    for lane in order[:max(0, remainder)]:
        quotas[lane] += 1
    return quotas


def _duplicate_keys(candidate: LanedCandidate) -> tuple[tuple[str, str], ...]:
    row = candidate.row
    keys: list[tuple[str, str]] = [("title", normalize_title(str(row.get("title", ""))))]
    url = row.get("canonical_url")
    if isinstance(url, str) and url:
        keys.append(("url", url))
    group = row.get("event_group_id")
    if isinstance(group, str) and group:
        keys.append(("group", group))
    return tuple(keys)


def _topics(candidate: LanedCandidate) -> tuple[str, ...]:
    value = candidate.row.get("category_ids")
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _primary_topic(candidate: LanedCandidate) -> str:
    """One topic per card for spacing purposes.

    Spacing compares primary topics, not whole category sets. A story commonly
    carries three or four categories and the configured vocabulary is small, so
    comparing full sets would make nearly every card collide with the previous
    one and the page would ship short on every request.
    """
    topics = _topics(candidate)
    return min(topics) if topics else ""


def _spacing_legal(emitted: Sequence[LanedCandidate], candidate: LanedCandidate,
                   policy: CompositionPolicy) -> bool:
    source_id = str(candidate.row.get("source_id", ""))
    group = candidate.row.get("event_group_id")
    for previous in emitted[-policy.same_source_window:]:
        if source_id and str(previous.row.get("source_id", "")) == source_id:
            return False
        if isinstance(group, str) and group and previous.row.get("event_group_id") == group:
            return False
    topic = _primary_topic(candidate)
    if topic and any(_primary_topic(previous) == topic for previous in emitted[-policy.topic_window_k:]):
        return False
    return True


def finalize_page(ordered: Sequence[LanedCandidate], *, policy: CompositionPolicy,
                  owner_states: Mapping[str, Mapping[str, object]], page_size: int,
                  profile: BehaviorProfile | None = None) -> FinalizedPage:
    # 1. Drop already-opened. Hard removal, never a demotion.
    survivors: list[LanedCandidate] = []
    for candidate in ordered:
        if policy.hide_already_opened and owner_states.get(candidate.story_id, {}).get("read_at"):
            continue
        survivors.append(candidate)

    # 2. Drop duplicates. The non-aggregator member survives and the loser is
    # attached to it, so the coverage is shown rather than deleted.
    winners: dict[tuple[str, str], LanedCandidate] = {}
    kept: list[LanedCandidate] = []
    also: dict[str, list[str]] = {}
    for candidate in survivors:
        keys = _duplicate_keys(candidate)
        clash = next((winners[key] for key in keys if key in winners), None)
        if clash is None:
            for key in keys:
                winners[key] = candidate
            kept.append(candidate)
            continue
        loser, winner = candidate, clash
        if clash.row.get("source_is_aggregator") and not candidate.row.get("source_is_aggregator"):
            # The publisher outranks the aggregator; swap which row survives.
            position = next(index for index, item in enumerate(kept) if item.story_id == clash.story_id)
            kept[position] = candidate
            for key in _duplicate_keys(clash) + keys:
                winners[key] = candidate
            also[candidate.story_id] = also.pop(clash.story_id, [])
            loser, winner = clash, candidate
        name = str(loser.row.get("source_name") or loser.row.get("source_id") or "")
        if name and name not in also.setdefault(winner.story_id, []):
            also[winner.story_id].append(name)

    # 3 and 4. Emit under hard spacing, filling each lane from its own pool.
    quotas = page_quotas(policy, page_size)
    counts = {lane: 0 for lane in policy.lane_priority}
    emitted: list[LanedCandidate] = []
    remaining = list(kept)
    donor_used = 0
    while len(emitted) < page_size:
        choice = next((item for item in remaining
                       if counts[item.lane] < quotas[item.lane] and _spacing_legal(emitted, item, policy)), None)
        if choice is None:
            # 5. Short-pool rule. Aligned is the only donor: borrowing from hot
            # or surprise would quietly delete the variety this exists to add.
            choice = next((item for item in remaining
                           if item.lane == "interested" and _spacing_legal(emitted, item, policy)), None)
            if choice is not None:
                donor_used += 1
        if choice is None:
            break
        remaining.remove(choice)
        emitted.append(choice)
        counts[choice.lane] += 1

    short: list[Mapping[str, object]] = []
    for lane in policy.lane_priority:
        served = sum(1 for item in emitted if item.lane == lane)
        if served < quotas[lane]:
            short.append({"lane": lane, "quota": quotas[lane], "served": served,
                          "shortfall": quotas[lane] - served,
                          "reason": "lane_pool_exhausted" if donor_used or len(emitted) < page_size
                                    else "spacing_constraint"})

    kl, alarm = _calibration(emitted, profile, policy)
    return FinalizedPage(cards=tuple(emitted),
                         also_covered_by={story: tuple(names) for story, names in also.items() if names},
                         short_lane_reasons=tuple(short), calibration_kl=kl, calibration_alarm=alarm)


def _calibration(emitted: Sequence[LanedCandidate], profile: BehaviorProfile | None,
                 policy: CompositionPolicy) -> tuple[float | None, bool]:
    """KL divergence of the aligned block's topic mix from the owner's own mix.

    Measured and reported. It never reorders the page: a silent reorder driven by
    a divergence number is exactly the kind of invisible behavior this codebase
    keeps out of the ranking path.
    """
    if profile is None or not profile.observed_topic_mix:
        return None, False
    counts: dict[str, float] = {}
    for candidate in emitted:
        if candidate.lane != "interested":
            continue
        for topic in _topics(candidate):
            counts[topic] = counts.get(topic, 0.0) + 1.0
    total = sum(counts.values())
    if total <= 0:
        return None, False
    from math import log
    divergence = 0.0
    for topic, weight in counts.items():
        page_share = weight / total
        reference = profile.observed_topic_mix.get(topic, 0.0)
        # An unseen topic is smoothed rather than treated as impossible: a reader
        # meeting a new topic is the point of the feed, not an infinite error.
        reference = reference if reference > 0 else 1e-6
        divergence += page_share * log(page_share / reference)
    divergence = round(max(0.0, divergence), 6)
    return divergence, divergence > policy.calibration_alarm_kl
