"""Validated composition policy: the feed recipe's operational values.

Boot fails on an out-of-range value rather than clamping it, so a typo in the
policy file is a startup error and never a silently different feed. Every value
the recipe depends on is loaded here; code carries a safe default only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

# The four candidate pools, using the frozen Lane vocabulary from
# curator/contracts/enums.py rather than a second name for the same thing.
LANES = ("updates", "hot", "interested", "surprise")

# The fifth chip. It is NOT a pool and it carries no quota: it is what a card
# says when it met no lane's rule and is on the page because the page would
# otherwise be short. Calling such a card "fresh" was a lie the reader could see:
# a quiet-hour probe found 21 of 25 cards chipped fresh at 10 to 15 hours old
# against an updates window of 6.
BACKFILL_LANE = "more"

# The actions the product actually captures today. An engagement weight for
# anything else (ask_question, dwell, dismiss) fails validation instead of being
# silently ignored: those arrive with their capture surface, not before.
CAPTURED_ACTIONS = ("open", "read_original", "save", "more_like_this", "less_like_this")


class CompositionPolicyError(ValueError):
    """An operational value is missing, mistyped, or out of its declared range."""


@dataclass(frozen=True)
class CompositionPolicy:
    lane_ratios: Mapping[str, float]
    lane_priority: tuple[str, ...]
    page_size: int
    candidate_window_size: int
    per_source_cap_per_window: int
    per_aggregator_cap_per_window: int
    updates_max_age_hours: int
    exploration_max_age_hours: int
    exploration_min_independent_sources: int
    exploration_require_non_aggregator: bool
    surprise_label_enabled: bool
    surprise_label_text: str
    trend_window_hours: int
    trend_min_independent_sources: int
    engagement_weights: Mapping[str, float]
    gate_action: str
    negatives_additive: bool
    decay_half_life_hours: float
    same_source_window: int
    topic_window_k: int
    hide_already_opened: bool
    calibration_alarm_kl: float
    idle_minutes: int
    max_run_minutes: int
    negative_suppression_days: int
    max_pages_per_run: int
    immediate_negative_filter: bool
    exclusive_promote_to_all_max: int
    default_display_language: str
    other_lane_enabled: bool
    lane_labels: Mapping[str, str]
    exclusive_label_template: str
    language_names: Mapping[str, str]

    def lane_quota(self, lane: str, size: int) -> int:
        """Slots this lane owns on a page of ``size``, by the configured ratio."""
        return int(round(size * self.lane_ratios[lane]))

    def label_for(self, lane: str) -> str:
        return self.lane_labels[lane]

    def quota_for(self, lane: str, size: int) -> int:
        """Backfill has no quota: it fills what the four pools could not."""
        return self.lane_quota(lane, size) if lane in self.lane_ratios else 0

    def exclusive_label(self, display_language: str) -> str:
        """"Only in Chinese press" is DERIVED, never stored as English text.

        The label names the OTHER language, so switching the reader's display
        language renames the section without a code change.
        """
        other = "zh" if display_language == "en" else "en"
        return self.exclusive_label_template.format(language=self.language_names[other])


_NUMERIC_RANGES = {
    "composition.page_size": (int, 1, 25),
    "composition.candidate_window_size": (int, 10, 100),
    "composition.per_source_cap_per_window": (int, 1, 10),
    "composition.per_aggregator_cap_per_window": (int, 1, 10),
    "composition.updates_max_age_hours": (int, 1, 72),
    "trend.window_hours": (int, 1, 72),
    "trend.min_independent_sources": (int, 1, 10),
    "exploration.max_age_hours": (int, 1, 168),
    "exploration.min_independent_sources": (int, 0, 10),
    "decay.half_life_hours": (float, 0.5, 8760.0),
    "diversity.same_source_window": (int, 1, 10),
    "diversity.topic_window_k": (int, 1, 100),
    "diversity.calibration_alarm_kl": (float, 0.0, 2.0),
    "run.idle_minutes": (int, 5, 1440),
    "run.max_minutes": (int, 15, 240),
    "run.negative_suppression_days": (int, 1, 90),
    "run.max_pages_per_run": (int, 1, 20),
    "lane.exclusive_promote_to_all_max": (int, 0, 5),
}
_BOOLEAN_KEYS = (
    "composition.surprise_label_enabled",
    "scoring.negatives_additive",
    "diversity.hide_already_opened",
    "run.immediate_negative_filter",
    "language.other_lane_enabled",
    "exploration.require_non_aggregator",
)


def _at(document: Mapping[str, object], dotted: str) -> object:
    node: object = document
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise CompositionPolicyError(f"{dotted} must be configured")
        node = node[part]
    return node


def _number(document: Mapping[str, object], dotted: str) -> int | float:
    kind, low, high = _NUMERIC_RANGES[dotted]
    value = _at(document, dotted)
    # A bool is an int in Python. A boolean where a count belongs is a config
    # error, not a 1, so it is rejected before the range check.
    if isinstance(value, bool):
        raise CompositionPolicyError(f"{dotted} must be a {kind.__name__}")
    if kind is int and type(value) is not int:
        raise CompositionPolicyError(f"{dotted} must be an integer")
    if kind is float and not isinstance(value, (int, float)):
        raise CompositionPolicyError(f"{dotted} must be a number")
    value = kind(value)
    if not low <= value <= high:
        raise CompositionPolicyError(f"{dotted} must be between {low} and {high}")
    return value


def _boolean(document: Mapping[str, object], dotted: str) -> bool:
    value = _at(document, dotted)
    if not isinstance(value, bool):
        raise CompositionPolicyError(f"{dotted} must be a boolean")
    return value


def _text(document: Mapping[str, object], dotted: str, *, maximum: int) -> str:
    value = _at(document, dotted)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CompositionPolicyError(f"{dotted} must be text of at most {maximum} characters")
    return value


def parse_composition_policy(document: object, *, retention_days: int | None = None) -> CompositionPolicy:
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise CompositionPolicyError("invalid composition policy")

    ratios_raw = _at(document, "composition.lane_ratios")
    if not isinstance(ratios_raw, Mapping) or set(ratios_raw) != set(LANES):
        raise CompositionPolicyError("composition.lane_ratios must name every lane exactly once")
    ratios = {}
    for lane in LANES:
        value = ratios_raw[lane]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise CompositionPolicyError(f"composition.lane_ratios.{lane} must be between 0.0 and 1.0")
        ratios[lane] = float(value)
    # Check 1: the ratios must describe one whole page.
    if abs(sum(ratios.values()) - 1.0) > 0.001:
        raise CompositionPolicyError("composition.lane_ratios must sum to 1.0")

    priority = _at(document, "lane_priority")
    if not isinstance(priority, list) or tuple(priority) != tuple(dict.fromkeys(priority)) or set(priority) != set(LANES):
        raise CompositionPolicyError("lane_priority must order every lane exactly once")

    weights_raw = _at(document, "engagement_weights")
    if not isinstance(weights_raw, Mapping) or not weights_raw:
        raise CompositionPolicyError("engagement_weights must be configured")
    # ALL FIVE or none. A policy missing "save" does not weight saves at zero,
    # it silently drops the strongest positive signal the product captures, and
    # nothing downstream can tell that apart from a deliberate zero.
    missing = [action for action in CAPTURED_ACTIONS if action not in weights_raw]
    if missing:
        raise CompositionPolicyError(
            "engagement_weights must name every captured action; missing " + ", ".join(missing))
    weights = {}
    for action, value in weights_raw.items():
        # Check 7: a weight for an action nothing captures is a design error.
        if action not in CAPTURED_ACTIONS:
            raise CompositionPolicyError(f"engagement_weights.{action} names an action the product does not capture")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -500.0 <= float(value) <= 500.0:
            raise CompositionPolicyError(f"engagement_weights.{action} must be between -500.0 and 500.0")
        weights[action] = float(value)

    gate_action = _at(document, "scoring.gate_action")
    # Check 3: the gate must name an action the prediction schema returns.
    if gate_action not in CAPTURED_ACTIONS:
        raise CompositionPolicyError("scoring.gate_action must name a predicted action")

    labels_raw = _at(document, "labels")
    if not isinstance(labels_raw, Mapping):
        raise CompositionPolicyError("labels must be configured")
    lane_labels = {}
    for lane in (*LANES, BACKFILL_LANE):
        value = labels_raw.get(lane)
        if not isinstance(value, str) or not value.strip() or len(value) > 40:
            raise CompositionPolicyError(f"labels.{lane} must be text of at most 40 characters")
        lane_labels[lane] = value
    template = _text(document, "labels.exclusive_template", maximum=80)
    if "{language}" not in template:
        raise CompositionPolicyError("labels.exclusive_template must carry a {language} placeholder")
    names_raw = _at(document, "labels.language_names")
    if not isinstance(names_raw, Mapping) or set(names_raw) != {"en", "zh"} or any(
            not isinstance(value, str) or not value.strip() for value in names_raw.values()):
        raise CompositionPolicyError("labels.language_names must name en and zh")

    display = _at(document, "language.default_display")
    if display not in ("en", "zh"):
        raise CompositionPolicyError("language.default_display must be a supported language")

    numbers = {key: _number(document, key) for key in _NUMERIC_RANGES}
    booleans = {key: _boolean(document, key) for key in _BOOLEAN_KEYS}

    page_size = int(numbers["composition.page_size"])
    window = int(numbers["composition.candidate_window_size"])
    pages = int(numbers["run.max_pages_per_run"])
    # Check 4: a window smaller than a page cannot fill one.
    if window < page_size:
        raise CompositionPolicyError("composition.candidate_window_size must be at least composition.page_size")
    # Check 5: a page count the window cannot supply is a promise, not a config.
    if pages != window // page_size:
        raise CompositionPolicyError("run.max_pages_per_run must equal candidate_window_size // page_size")
    # Check 8: the corpus must still hold the window the feed reads.
    #
    # The prune deletes by published_at, and trend.window_hours may be set as
    # high as 72. A retention of two days with a three-day trend window would
    # delete rows the hot lane is still counting, and hot would quietly read as
    # zero. Today's shipped values are safe; the allowed config SPACE was not,
    # and that is what a validator is for.
    trend_hours = int(numbers["trend.window_hours"])
    exploration_hours = int(numbers["exploration.max_age_hours"])
    if retention_days is not None:
        needed = max(trend_hours, exploration_hours)
        if retention_days * 24 < needed:
            raise CompositionPolicyError(
                f"coverage.observations_retention_days ({retention_days}) keeps "
                f"{retention_days * 24} hours, but the feed reads back {needed} hours "
                "(the larger of trend.window_hours and exploration.max_age_hours). "
                "Raise the retention, or lower the window.")

    return CompositionPolicy(
        lane_ratios=ratios, lane_priority=tuple(priority), page_size=page_size,
        candidate_window_size=window,
        per_source_cap_per_window=int(numbers["composition.per_source_cap_per_window"]),
        per_aggregator_cap_per_window=int(numbers["composition.per_aggregator_cap_per_window"]),
        updates_max_age_hours=int(numbers["composition.updates_max_age_hours"]),
        exploration_max_age_hours=int(numbers["exploration.max_age_hours"]),
        exploration_min_independent_sources=int(numbers["exploration.min_independent_sources"]),
        exploration_require_non_aggregator=booleans["exploration.require_non_aggregator"],
        surprise_label_enabled=booleans["composition.surprise_label_enabled"],
        surprise_label_text=_text(document, "composition.surprise_label_text", maximum=80),
        trend_window_hours=int(numbers["trend.window_hours"]),
        trend_min_independent_sources=int(numbers["trend.min_independent_sources"]),
        engagement_weights=weights, gate_action=str(gate_action),
        negatives_additive=booleans["scoring.negatives_additive"],
        decay_half_life_hours=float(numbers["decay.half_life_hours"]),
        same_source_window=int(numbers["diversity.same_source_window"]),
        topic_window_k=int(numbers["diversity.topic_window_k"]),
        hide_already_opened=booleans["diversity.hide_already_opened"],
        calibration_alarm_kl=float(numbers["diversity.calibration_alarm_kl"]),
        idle_minutes=int(numbers["run.idle_minutes"]),
        max_run_minutes=int(numbers["run.max_minutes"]),
        negative_suppression_days=int(numbers["run.negative_suppression_days"]),
        max_pages_per_run=pages,
        immediate_negative_filter=booleans["run.immediate_negative_filter"],
        exclusive_promote_to_all_max=int(numbers["lane.exclusive_promote_to_all_max"]),
        default_display_language=str(display),
        other_lane_enabled=booleans["language.other_lane_enabled"],
        lane_labels=lane_labels, exclusive_label_template=template,
        language_names={key: str(value) for key, value in names_raw.items()},
    )


def load_composition_policy(path: str | Path, *, retention_days: int | None = None) -> CompositionPolicy:
    return parse_composition_policy(yaml.safe_load(Path(path).read_text(encoding="utf-8")),
                                    retention_days=retention_days)


def configured_retention_days(sources_path: str | Path = "sources.yaml") -> int | None:
    """The corpus retention window, read from the file that owns it.

    Returns None when the file or the key is absent, so a deployment that has
    not adopted the key yet boots exactly as before rather than failing on a
    cross-file check it cannot satisfy.
    """
    path = Path(sources_path)
    if not path.is_file():
        return None
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    coverage = document.get("coverage") if isinstance(document, Mapping) else None
    value = coverage.get("observations_retention_days") if isinstance(coverage, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def half_life_weight(age_hours: float, half_life_hours: float) -> float:
    """Exponential recency decay, used identically by the profile and the prompt."""
    if age_hours <= 0:
        return 1.0
    return float(math.pow(0.5, age_hours / half_life_hours))
