"""One aggregate, count-only receipt per composition pass. Never owner data.

Window counts bracket admission; rejected source-cap attempts count each story
once even when backfill retries it. Finalization removals and emitted cards sum
across its internal pages, while remaining is the last page's deferred tail.
append_selected is before persistence, not proof that cards were served. Stages
not applicable to the current phase stay zero. No per-card or per-page arrays.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable

LANES = ("updates", "hot", "interested", "surprise", "more")
STAGES = (
    "window_input", "window_selected", "source_cap_rejected", "pre_finalize",
    "opened_removed", "duplicate_removed", "finalized", "finalize_remaining",
    "frozen_duplicate_removed", "append_selected",
)


class LaneDiagnostics:
    def __init__(self):
        self.counts = {stage: dict.fromkeys(LANES, 0) for stage in STAGES}

    def add(self, stage: str, lanes: Iterable[str]) -> None:
        counts = self.counts[stage]
        for lane in lanes:
            if lane in LANES:
                counts[lane] += 1

    def record(self, stage: str, lanes: Iterable[str]) -> None:
        self.counts[stage] = dict.fromkeys(LANES, 0)
        self.add(stage, lanes)

    def emit(self, phase: str) -> None:
        if phase not in ("rank", "continuation"):
            return
        # Reconstruct the allowlisted schema. Do not serialize caller mappings,
        # candidates, policy labels, profile fields, or exception text.
        counts = {stage: {lane: value if type(value := self.counts[stage].get(lane)) is int
                         and value >= 0 else 0 for lane in LANES} for stage in STAGES}
        payload = json.dumps({"event": "m2_lane_counts", "phase": phase,
                              "counts": counts}, separators=(",", ":")) + "\n"
        try:
            sys.stderr.write(payload)
            sys.stderr.flush()
        except (OSError, ValueError):
            # An unavailable log sink must not change the feed or spend path.
            pass
