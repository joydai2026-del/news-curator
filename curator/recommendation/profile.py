"""The owner's behavior profile: a pure function of one history snapshot.

Built once per reading run and frozen on the run row, so every page inside the
run is explainable after the fact from a single stored version. Nothing here
calls a provider, reads a clock it was not given, or mutates its input.

Learning off means an EMPTY profile, not a default one: the aligned pool then
degrades to fresh and the surprise pool stays empty, because a story cannot be
"off profile" when there is no profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .composition import CompositionPolicy, half_life_weight

# Captured event type -> the weighted action it counts as. Every entry has a
# live capture path (the three M2 write RPCs). An event type absent from this
# map contributes query context to the prompt and nothing to the profile.
EVENT_ACTIONS = {
    "read_more": "open",
    "open_original": "read_original",
    "save": "save",
    "more_like_this": "more_like_this",
    "less_like_this": "less_like_this",
}


@dataclass(frozen=True)
class BehaviorProfile:
    source_affinity: Mapping[str, float] = field(default_factory=dict)
    topic_affinity: Mapping[str, float] = field(default_factory=dict)
    # The owner's own topic proportions, from positive actions only. This is the
    # reference distribution the B6 calibration alarm measures the page against.
    observed_topic_mix: Mapping[str, float] = field(default_factory=dict)
    # Sources and topics the owner explicitly asked for less of. They never block
    # a source globally; they lower matched features and filter inside a run.
    suppressed_sources: frozenset[str] = frozenset()
    suppressed_topics: frozenset[str] = frozenset()
    event_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.source_affinity and not self.topic_affinity

    def affinity(self, *, source_id: str, category_ids: Sequence[str]) -> float:
        score = self.source_affinity.get(source_id, 0.0)
        for category_id in category_ids:
            score += self.topic_affinity.get(category_id, 0.0)
        return score

    def as_snapshot(self) -> dict[str, object]:
        """The frozen form stored on the reading run row."""
        return {
            "schema_version": 1,
            "source_affinity": dict(self.source_affinity),
            "topic_affinity": dict(self.topic_affinity),
            "observed_topic_mix": dict(self.observed_topic_mix),
            "suppressed_sources": sorted(self.suppressed_sources),
            "suppressed_topics": sorted(self.suppressed_topics),
            "event_count": self.event_count,
        }

    @classmethod
    def from_snapshot(cls, snapshot: object) -> "BehaviorProfile":
        if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 1:
            return cls()

        def numbers(key: str) -> dict[str, float]:
            value = snapshot.get(key)
            if not isinstance(value, Mapping):
                return {}
            return {str(name): float(weight) for name, weight in value.items()
                    if isinstance(weight, (int, float)) and not isinstance(weight, bool)}

        def names(key: str) -> frozenset[str]:
            value = snapshot.get(key)
            return frozenset(str(item) for item in value) if isinstance(value, list) else frozenset()

        count = snapshot.get("event_count")
        return cls(source_affinity=numbers("source_affinity"), topic_affinity=numbers("topic_affinity"),
                   observed_topic_mix=numbers("observed_topic_mix"), suppressed_sources=names("suppressed_sources"),
                   suppressed_topics=names("suppressed_topics"),
                   event_count=count if isinstance(count, int) and not isinstance(count, bool) else 0)


def build_profile(snapshot: Mapping[str, object], *, policy: CompositionPolicy, now: datetime) -> BehaviorProfile:
    """Weight each captured event by its configured value and its age.

    Gated on ``learning_enabled`` exactly as the raw prompt history already is,
    so consent-off never produces a personalized pool.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not snapshot.get("learning_enabled"):
        return BehaviorProfile()
    events = snapshot.get("events") or ()
    if not isinstance(events, Sequence):
        return BehaviorProfile()

    sources: dict[str, float] = {}
    topics: dict[str, float] = {}
    positive_topics: dict[str, float] = {}
    suppressed_sources: set[str] = set()
    suppressed_topics: set[str] = set()
    counted = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        action = EVENT_ACTIONS.get(str(event.get("event_type")))
        if action is None:
            continue
        weight = policy.engagement_weights.get(action)
        if weight is None:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        # An un-save is the withdrawal of a save, not another save.
        if action == "save" and payload.get("saved") is False:
            weight = -abs(weight)
        occurred = _timestamp(event.get("occurred_at"))
        if occurred is None:
            continue
        age_hours = max(0.0, (now - occurred).total_seconds() / 3600.0)
        value = weight * half_life_weight(age_hours, policy.decay_half_life_hours)
        # Suppression is a recent act, not a permanent verdict. Outside the
        # window the event still lowers affinity through the decay above; it just
        # stops removing the source from the page outright.
        suppressing = age_hours <= policy.negative_suppression_days * 24
        counted += 1
        source_id = event.get("source_id")
        if isinstance(source_id, str) and source_id:
            sources[source_id] = sources.get(source_id, 0.0) + value
            if action == "less_like_this" and suppressing:
                suppressed_sources.add(source_id)
        topic_id = payload.get("topic_id")
        if isinstance(topic_id, str) and topic_id:
            topics[topic_id] = topics.get(topic_id, 0.0) + value
            if action == "less_like_this" and suppressing:
                suppressed_topics.add(topic_id)
            elif value > 0:
                positive_topics[topic_id] = positive_topics.get(topic_id, 0.0) + value
    total = sum(positive_topics.values())
    mix = {topic: value / total for topic, value in positive_topics.items()} if total > 0 else {}
    return BehaviorProfile(source_affinity=sources, topic_affinity=topics, observed_topic_mix=mix,
                           suppressed_sources=frozenset(suppressed_sources),
                           suppressed_topics=frozenset(suppressed_topics), event_count=counted)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
