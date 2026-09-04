"""Candidate contract: lane candidates, merge, scoring, and slate assembly.

Declarative only. No behavior, no I/O.
Freezes: plan "Candidate generation and slate assembly" and SC-08, SC-08A,
SC-09, SC-10, SC-24, SC-25.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import BandVerdict, Lane, PublicationClass, ScorerKind
from .tenant import Ownership


@dataclass(frozen=True)
class LaneCandidate(Ownership):
    """One generator's proposal for one story in one lane.

    A story may appear once per lane. The primary lane is decided at merge, not
    here, so a generator can never award itself quota.
    """

    run_id: str
    lane: Lane
    story_id: str
    story_cluster_id: str
    lane_score: float
    reason: str
    generator_version: str
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StoryRecord(Ownership):
    """The consolidated story cluster (`story_records`, plan "Core records").

    One row per canonical story cluster, independent of any single lane
    assignment or edition. Candidate generation and search read this record;
    it never branches on a provider name.
    """

    story_cluster_id: str
    publication_class: PublicationClass
    canonical_source_document_id: str
    source_document_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    synthesis_evidence_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MergedCandidate(Ownership):
    """One canonical story after dedupe, carrying every lane it qualified for.

    SC-24 reproducibility: primary_lane must be recomputable from the persisted
    ``lane_scores``, the policy's lane priority, and ``story_id`` alone, with no
    generator rerun. Secondary reasons survive so the explanation stays whole.
    """

    run_id: str
    story_id: str
    story_cluster_id: str
    primary_lane: Lane
    # Every lane this story qualified for, with its score. The primary lane must
    # be a member of this mapping.
    lane_scores: tuple[tuple[Lane, float], ...]
    lane_reasons: tuple[tuple[Lane, str], ...]
    # Tie-break order actually applied: lane priority, then lane_score, then
    # story_id. Persisted so a replay can prove which rule decided.
    tie_break_applied: str
    secondary_lanes: tuple[Lane, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ComponentScores:
    """The transparent scorer's decomposition. Every field is persisted.

    The composition is configurable: a component may be disabled in policy, in
    which case its value here is 0.0 and the policy records the disablement
    explicitly rather than by omission.
    """

    relevance: float
    freshness: float
    trend: float
    editor_consensus: float
    deliberate_surprise: float
    diversity: float
    repetition_penalty: float
    source_fatigue_penalty: float
    final_score: float


@dataclass(frozen=True)
class ScoredCandidate:
    """One story's score set from one scorer.

    Only ``ScorerKind.TRANSPARENT`` may be authoritative. A shadow set is
    stored with ``authoritative=False`` and can never feed slate assembly.
    """

    run_id: str
    story_id: str
    scorer_kind: ScorerKind
    scorer_version: str
    authoritative: bool
    components: ComponentScores
    plain_reason: str


@dataclass(frozen=True)
class BandResult:
    """One edition band's configured bounds and achieved value.

    A DISABLED verdict is only legal when the policy carries a recorded,
    versioned exception for that band.
    """

    band: str
    active: bool
    floor: float | None
    cap: float | None
    achieved: float | None
    verdict: BandVerdict
    exception_reason: str = ""


@dataclass(frozen=True)
class SlateEntry:
    """One settled position in the final constrained order."""

    position: int
    story_id: str
    primary_lane: Lane
    final_score: float
    plain_reason: str
    # True when this entry replaced a rejected item through deterministic
    # backfill from the same primary lane's remaining ranked pool.
    backfilled: bool = False


@dataclass(frozen=True)
class Slate(Ownership):
    """The settled edition. Only the final invariant verifier permits this.

    ``verifier_verdict`` gates settlement: a slate whose verifier failed cannot
    be published or described as balanced.
    """

    run_id: str
    edition_date: str
    built_at: datetime
    policy_revision: int
    profile_snapshot_id: str | None
    entries: tuple[SlateEntry, ...]
    bands: tuple[BandResult, ...]
    verifier_verdict: BandVerdict
    lane_quotas: tuple[tuple[Lane, int], ...] = field(default_factory=tuple)
    # Lane quotas are CAPS, never exact counts. When a lane's ranked pool is
    # exhausted, the edition ships short rather than borrowing another
    # lane's quota (config/ranking-policy-r1.yaml: allow_cross_lane_borrow
    # is false). A short edition records why, rather than leaving it to be
    # inferred from a mismatch between entry count and quota totals.
    short_reason_code: str = ""
