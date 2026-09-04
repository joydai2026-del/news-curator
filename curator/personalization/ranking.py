"""Credential-free saved-interest scores bound to one source snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from ..models import Item
from ..normalize import fold_text

if TYPE_CHECKING:
    from ..config import Config


MAX_ARTIFACT_BYTES = 2_000_000
MAX_SCORE_ROWS = 20_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ARTIFACT_FIELDS = {
    "schema_version",
    "generated_at",
    "source_snapshot_digest",
    "configuration_digest",
    "preference_revision",
    "interest_count",
    "matched_story_count",
    "scores",
}


class InterestArtifactError(ValueError):
    """A low-information rejection of an invalid interest-ranking artifact."""


@dataclass(frozen=True)
class InterestProfile:
    revision: int
    interests: tuple[str, ...]


@dataclass(frozen=True)
class InterestArtifact:
    generated_at: str
    source_snapshot_digest: str
    configuration_digest: str
    preference_revision: int
    interest_count: int
    matched_story_count: int
    scores: Mapping[str, float]


def story_key(item: Item) -> str:
    """Bind a score to the exact source headline that will receive it."""

    identity = item.canonical_url or item.url
    headline = fold_text(item.title).casefold()
    return hashlib.sha256(f"{identity}\0{headline}".encode("utf-8")).hexdigest()


def ranking_config_digest(cfg: "Config") -> str:
    """Bind an artifact to every editable input used by the ranker."""

    payload = {
        "algorithm_version": 1,
        "ranking": cfg.ranking,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def interest_score(item: Item, interests: Sequence[str]) -> float:
    """Diminishing score for saved-interest phrases visible in the headline."""

    hits = {
        fold_text(term).casefold()
        for term in interests
        if _find_interest(item.title, term)
    }
    if not hits:
        return 0.0
    return min(1.0, math.log1p(len(hits)) / math.log1p(3))


def _is_cjk(character: str) -> bool:
    return (
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
    )


@lru_cache(maxsize=4096)
def _mixed_script_pattern(term: str) -> re.Pattern[str]:
    """Literal CJK matching with boundaries around each ASCII word segment."""

    normalized = fold_text(term)
    pieces: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if character.isspace():
            end = index + 1
            while end < len(normalized) and normalized[end].isspace():
                end += 1
            previous = normalized[index - 1] if index else ""
            following = normalized[end] if end < len(normalized) else ""
            crosses_cjk_boundary = bool(previous and following) and (
                _is_cjk(previous) != _is_cjk(following)
            )
            pieces.append(r"\s*" if crosses_cjk_boundary else r"\s+")
            index = end
            continue
        if not _is_cjk(character):
            if index and _is_cjk(normalized[index - 1]):
                pieces.append(r"\s*")
            end = index + 1
            while end < len(normalized):
                candidate = normalized[end]
                if candidate.isspace() or _is_cjk(candidate):
                    break
                end += 1
            segment = normalized[index:end]
            escaped = re.escape(segment)
            if any(char.isascii() and (char.isalnum() or char == "_") for char in segment):
                escaped = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
            pieces.append(escaped)
            index = end
            continue
        if index and not normalized[index - 1].isspace() and not _is_cjk(normalized[index - 1]):
            pieces.append(r"\s*")
        pieces.append(re.escape(character))
        index += 1
    return re.compile("".join(pieces), re.IGNORECASE)


def _find_interest(title: str, term: str) -> re.Match[str] | None:
    if not fold_text(term):
        return None
    return _mixed_script_pattern(term).search(fold_text(title))


def build_interest_artifact(
    profile: InterestProfile,
    items: Iterable[Item],
    *,
    source_snapshot_digest: str,
    configuration_digest: str,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build a score-only artifact. Raw interests and user identity never leave the job."""

    when = generated_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    scores: dict[str, float] = {}
    for item in items:
        score = interest_score(item, profile.interests)
        if score <= 0:
            continue
        key = story_key(item)
        scores[key] = max(scores.get(key, 0.0), score)
    return {
        "schema_version": 1,
        "generated_at": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_snapshot_digest": source_snapshot_digest,
        "configuration_digest": configuration_digest,
        "preference_revision": profile.revision,
        "interest_count": len(profile.interests),
        "matched_story_count": len(scores),
        "scores": dict(sorted(scores.items())),
    }


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InterestArtifactError("interest ranking artifact is invalid")
    return value


def load_interest_artifact(
    path: Path,
    *,
    expected_source_snapshot_digest: str,
    expected_configuration_digest: str,
    allowed_story_keys: set[str] | None = None,
) -> InterestArtifact:
    """Load an exact, bounded artifact and bind it to this build's snapshot."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InterestArtifactError("interest ranking artifact is unavailable") from exc
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise InterestArtifactError("interest ranking artifact is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InterestArtifactError("interest ranking artifact is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _ARTIFACT_FIELDS:
        raise InterestArtifactError("interest ranking artifact is invalid")
    if payload["schema_version"] != 1:
        raise InterestArtifactError("interest ranking artifact is invalid")

    generated_at = payload["generated_at"]
    source_digest = payload["source_snapshot_digest"]
    config_digest = payload["configuration_digest"]
    if not isinstance(generated_at, str) or not _TIMESTAMP.fullmatch(generated_at):
        raise InterestArtifactError("interest ranking artifact is invalid")
    if not isinstance(source_digest, str) or not _DIGEST.fullmatch(source_digest):
        raise InterestArtifactError("interest ranking artifact is invalid")
    if not isinstance(config_digest, str) or not _DIGEST.fullmatch(config_digest):
        raise InterestArtifactError("interest ranking artifact is invalid")
    if source_digest != expected_source_snapshot_digest or config_digest != expected_configuration_digest:
        raise InterestArtifactError("interest ranking artifact does not belong to this build")

    revision = _nonnegative_int(payload["preference_revision"])
    interest_count = _nonnegative_int(payload["interest_count"])
    matched_count = _nonnegative_int(payload["matched_story_count"])
    if not 0 <= interest_count <= 20:
        raise InterestArtifactError("interest ranking artifact is invalid")
    scores = payload["scores"]
    if not isinstance(scores, dict) or len(scores) > MAX_SCORE_ROWS or matched_count != len(scores):
        raise InterestArtifactError("interest ranking artifact is invalid")
    if interest_count == 0 and scores:
        raise InterestArtifactError("interest ranking artifact is invalid")
    checked: dict[str, float] = {}
    for key, value in scores.items():
        if (
            not isinstance(key, str)
            or not _DIGEST.fullmatch(key)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            raise InterestArtifactError("interest ranking artifact is invalid")
        try:
            score = float(value)
        except (OverflowError, ValueError):
            raise InterestArtifactError("interest ranking artifact is invalid") from None
        if not math.isfinite(score) or not 0 < score <= 1:
            raise InterestArtifactError("interest ranking artifact is invalid")
        checked[key] = score
    if allowed_story_keys is not None and not checked.keys() <= allowed_story_keys:
        raise InterestArtifactError("interest ranking artifact contains an unknown story")
    return InterestArtifact(
        generated_at=generated_at,
        source_snapshot_digest=source_digest,
        configuration_digest=config_digest,
        preference_revision=revision,
        interest_count=interest_count,
        matched_story_count=matched_count,
        scores=checked,
    )


def measure_ranking_impact(
    ordinary: Mapping[str, Sequence[Item]],
    personalized: Mapping[str, Sequence[Item]],
) -> dict[str, int]:
    """Count position changes without exposing titles, URLs, or interests."""

    moved = 0
    maximum = 0
    for category, ordinary_items in ordinary.items():
        personalized_items = personalized.get(category, ())
        before = {story_key(item): index for index, item in enumerate(ordinary_items)}
        after = {story_key(item): index for index, item in enumerate(personalized_items)}
        missing_position = max(len(before), len(after))
        for key in before.keys() | after.keys():
            delta = abs(
                before.get(key, missing_position)
                - after.get(key, missing_position)
            )
            if delta:
                moved += 1
                maximum = max(maximum, delta)
    return {"moved_rows": moved, "max_position_delta": maximum}
