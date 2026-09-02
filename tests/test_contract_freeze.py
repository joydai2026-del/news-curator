"""Contract-freeze validation (SC-41).

Three jobs:

1. Inventory. Every one of the twelve frozen contracts has a prose file, a
   typed module, and at least one valid plus one invalid fixture.
2. Fixtures. Every ``valid`` fixture satisfies its typed definition and every
   named invariant; every ``invalid`` fixture is rejected for the reason it
   claims. An invalid fixture that quietly passes is itself a failure.
3. Policy revision 1. Every SC-20 band exists with concrete values, every
   SC-08A band is active, and the policy's own vocabularies agree with the
   frozen enums.

The structural validator lives HERE, not in ``curator/contracts``. That package
is declarative by design: dataclasses, Protocols, and Enums with no behavior, so
a contract cannot drift because its validator changed.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
import typing
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest
import yaml

from curator import contracts
from curator.contracts import ACTION_MATRIX, FROZEN_CONTRACTS, PUBLICATION_TRANSITIONS
from curator.contracts.enums import (
    ConfidenceBand,
    EventType,
    EvidenceClass,
    Lane,
    ReadbackVerdict,
    Scope,
    ScorerKind,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "contracts"
POLICY_PATH = REPO_ROOT / "config" / "ranking-policy-r1.yaml"

#: SC-20 names seven bands. All seven must exist in policy revision 1.
SC20_BANDS = (
    "relevance",
    "freshness",
    "trend",
    "deliberate_surprise",
    "source_diversity",
    "topic_diversity",
    "repetition",
)

#: SC-08A: these four must be ACTIVE in a live edition policy. Disabling one is
#: a recorded, versioned exception, never a config default.
SC08A_REQUIRED_ACTIVE = ("relevance", "freshness", "source_diversity", "repetition")

#: The transparent scorer's composition, split by sign. ``final_score`` is the
#: positives minus the penalties (config/ranking-policy-r1.yaml's own formula).
POSITIVE_COMPONENTS = (
    "relevance",
    "freshness",
    "trend",
    "editor_consensus",
    "deliberate_surprise",
    "diversity",
)
PENALTY_COMPONENTS = ("repetition_penalty", "source_fatigue_penalty")

#: Persisted component values are rounded for readability, so the composition
#: check needs a tolerance. Small enough that a real disagreement fails.
COMPOSITION_TOLERANCE = 1e-6

#: The seven projections a deletion receipt must resolve before it can settle
#: (plan "Deletion settles only after every derived projection is handled").
DELETION_PROJECTION_VOCABULARY = (
    "profile_and_ranking",
    "search_index",
    "knowledge_artifacts",
    "caches",
    "exports",
    "public_projections",
    "mirrors",
)


#: Absolute home paths. An owner's machine layout is not contract content.
#: Assembled rather than written whole, for the same reason as the initials
#: pattern below: this file is itself inside the scanned frozen set.
OWNER_PATH_TOKENS = ("/" + "Users" + "/", "C:" + "\\" + "Users")

#: The owner's initials, assembled rather than written, so this pattern cannot
#: match itself and quietly pass while the real leak sits two files away.
_OWNER_INITIALS = re.compile(r"\b" + "j" + "j" + r"\b", re.IGNORECASE)

#: Provider names that must never appear in a CORE contract field name, enum
#: member, or field value. ``docs/contracts`` and the fixtures ship to the
#: public repo, and SC-36's whole point is that adding or removing a provider
#: is an adapter-config change: a provider baked into a core field makes that
#: false. Substrings, matched case-insensitively.
PROVIDER_NAME_TOKENS = (
    "google",
    "notion",
    "supabase",
    "cloudflare",
    "openai",
    "anthropic",
    "gemini",
    "beehiiv",
    "substack",
    # Nostr relay hostnames, plus the relay URL scheme itself so an unlisted
    # relay host is caught by its transport rather than by name.
    "wss://",
    "relay.damus.io",
    "nos.lol",
    "relay.snort.social",
    "nostr.wine",
    "relay.primal.net",
)

#: The adapter-identity exception, as (file, field) pairs.
#:
#: A field whose documented PURPOSE is to name one adapter, one configured
#: source route, or one destination may of course carry a provider name: that
#: is the field's content, not a leak. ``MirrorReceipt.adapter_id`` naming a
#: document-workspace provider is the system working; ``BandResult.band``
#: naming one is a vendor in the core ranking contract.
#:
#: Encoded as PAIRS rather than a bare list of field names so the exemption is
#: scoped to the file that documents it: a new field elsewhere carrying a
#: provider name fails, and so does a provider name in any OTHER field of these
#: same files. Files are paths relative to the repository root; a fixture that
#: legitimately names a real provider in one of these fields adds its own
#: (fixture path, field) pair here with a one-line reason.
#:
#: No fixture entry exists today on purpose: revision 1's fixtures use neutral
#: adapter ids (``document-workspace``, ``vault-wiki``, ``longform-relay``), so
#: the exemption is exercised by ``test_adapter_identity_allowlist_exempts_only
#: _the_named_field`` instead of by shipping a vendor name in the public set.
ADAPTER_IDENTITY_ALLOWLIST = frozenset(
    {
        # Mirror targets: which external system, and which object on it.
        ("curator/contracts/mirror.py", "adapter_id"),
        ("curator/contracts/mirror.py", "target_id"),
        ("curator/contracts/mirror.py", "precondition_kind"),
        # Output adapters: the destination and the publishing identity bound to it.
        ("curator/contracts/output_adapter.py", "adapter_id"),
        ("curator/contracts/output_adapter.py", "destination"),
        ("curator/contracts/output_adapter.py", "publisher_identity_ref"),
        ("curator/contracts/output_adapter.py", "acknowledged_targets"),
        ("curator/contracts/publication.py", "destination"),
        ("curator/contracts/publication.py", "publisher_identity_ref"),
        # Source plugins: the configured route identity and the article's own URL.
        ("curator/contracts/source_plugin.py", "plugin_id"),
        ("curator/contracts/source_plugin.py", "source_id"),
        ("curator/contracts/source_plugin.py", "original_item_id"),
        ("curator/contracts/source_plugin.py", "url"),
        ("curator/contracts/source_plugin.py", "canonical_url"),
        ("curator/contracts/source_plugin.py", "image_url"),
        # Receipts: which mirrored target, and whose meter the budget reading came from.
        ("curator/contracts/receipt.py", "target_ref"),
        ("curator/contracts/receipt.py", "mirrored_targets"),
        ("curator/contracts/receipt.py", "meter_source"),
        # Evidence: where a raw import's bytes physically live, and the article URL.
        ("curator/contracts/evidence.py", "storage_reference"),
        ("curator/contracts/evidence.py", "canonical_url"),
        # Search: the configured source routes a query is filtered to.
        ("curator/contracts/search.py", "source_ids"),
    }
)


class ContractViolation(AssertionError):
    """Raised by the structural validator or by a named invariant."""


def _freeze_paths() -> list[Path]:
    """Every file in the frozen set, including this validator itself."""
    paths = sorted((REPO_ROOT / "docs" / "contracts").glob("*.md"))
    paths += sorted((REPO_ROOT / "curator" / "contracts").glob("*.py"))
    paths += FIXTURE_PATHS
    paths.append(POLICY_PATH)
    paths.append(Path(__file__).resolve())
    return paths


def _provider_tokens_in(text: str) -> list[str]:
    lowered = text.lower()
    return [token for token in PROVIDER_NAME_TOKENS if token in lowered]


def _provider_leaks_in_json(node: object, file_key: str, field: str = "") -> list[str]:
    """Every provider name in a JSON tree's keys and string values.

    ``field`` carries the nearest enclosing key, so a provider name inside a
    list under an allowlisted field (``mirrored_targets``, ``source_ids``) is
    exempted with its field rather than escaping the pair check.
    """
    if field and (file_key, field) in ADAPTER_IDENTITY_ALLOWLIST:
        return []
    leaks: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            # Fixture envelope prose (note, violates) explains the contract; it
            # is not a contract field, so only the payload tree is scanned.
            if not field and key in ("note", "violates", "contract", "dataclass", "expect"):
                continue
            if (file_key, key) not in ADAPTER_IDENTITY_ALLOWLIST:
                leaks += [f"{field or 'field name'}.{key}={hit}" for hit in _provider_tokens_in(key)]
            leaks += _provider_leaks_in_json(value, file_key, key)
    elif isinstance(node, list):
        for element in node:
            leaks += _provider_leaks_in_json(element, file_key, field)
    elif isinstance(node, str):
        leaks += [f"{field}={hit}" for hit in _provider_tokens_in(node)]
    return leaks


# ---------------------------------------------------------------------------
# Structural validation against the typed definitions
# ---------------------------------------------------------------------------


def _hints(cls: type) -> dict[str, object]:
    return typing.get_type_hints(cls)


def _check_value(value: object, hint: object, path: str) -> None:
    origin = typing.get_origin(hint)

    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        errors = []
        for arg in typing.get_args(hint):
            if arg is type(None):
                if value is None:
                    return
                continue
            try:
                _check_value(value, arg, path)
                return
            except ContractViolation as exc:  # try the next member
                errors.append(str(exc))
        raise ContractViolation(f"{path}: matches no member of {hint} ({errors})")

    if origin in (tuple, list):
        if not isinstance(value, list):
            raise ContractViolation(f"{path}: expected a JSON array, got {type(value).__name__}")
        args = typing.get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            for index, element in enumerate(value):
                _check_value(element, args[0], f"{path}[{index}]")
            return
        if len(args) != len(value):
            raise ContractViolation(f"{path}: expected {len(args)} elements, got {len(value)}")
        for index, (element, arg) in enumerate(zip(value, args)):
            _check_value(element, arg, f"{path}[{index}]")
        return

    if isinstance(hint, type):
        if issubclass(hint, Enum):
            members = {member.value for member in hint}
            if not isinstance(value, str) or value not in members:
                raise ContractViolation(
                    f"{path}: {value!r} is not a member of {hint.__name__}"
                )
            return
        if hint is datetime:
            if not isinstance(value, str):
                raise ContractViolation(f"{path}: expected an ISO-8601 string")
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ContractViolation(f"{path}: not an ISO-8601 timestamp") from exc
            return
        if dataclasses.is_dataclass(hint):
            validate_payload(hint, value, path)
            return
        if hint is bool:
            if not isinstance(value, bool):
                raise ContractViolation(f"{path}: expected a boolean")
            return
        if hint is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractViolation(f"{path}: expected an integer")
            return
        if hint is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractViolation(f"{path}: expected a number")
            return
        if hint is str:
            if not isinstance(value, str):
                raise ContractViolation(f"{path}: expected a string")
            return

    raise ContractViolation(f"{path}: no validation rule for {hint!r}")


def validate_payload(cls: type, payload: object, path: str = "") -> None:
    """Structural check of one JSON payload against one frozen dataclass."""
    if not dataclasses.is_dataclass(cls):
        raise ContractViolation(f"{cls!r} is not a frozen contract dataclass")
    if not isinstance(payload, dict):
        raise ContractViolation(f"{path or cls.__name__}: expected a JSON object")

    hints = _hints(cls)
    fields = {field.name: field for field in dataclasses.fields(cls)}

    unknown = sorted(set(payload) - set(fields))
    if unknown:
        raise ContractViolation(
            f"{path or cls.__name__}: unknown field {unknown[0]}"
        )

    for name, field in fields.items():
        prefix = f"{path}.{name}" if path else f"{cls.__name__}.{name}"
        if name not in payload:
            has_default = (
                field.default is not dataclasses.MISSING
                or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
            )
            if not has_default:
                raise ContractViolation(f"{prefix}: required field {name} is absent")
            continue
        _check_value(payload[name], hints[name], prefix)


# ---------------------------------------------------------------------------
# Named invariants: the rules a type signature cannot express
# ---------------------------------------------------------------------------


def _pairs(rows: list, key_index: int = 0) -> list:
    return [row[key_index] for row in rows]


def _primary_lane_from_tiebreak(lane_scores: dict, priority: list) -> str:
    """SC-24's frozen rule for one story's primary lane.

    Level 1: the lane with the best (lowest-index) configured priority wins.
    Level 2: among lanes tied at level 1, the higher lane_score wins.

    Revision 1's configured priority (``config/ranking-policy-r1.yaml``'s
    ``edition.lane_priority``) is a strict permutation over the four lanes, so
    within one story level 1 always resolves alone in production; level 2 is
    still frozen here, generically, so the rule is correct rather than
    aspirational for a future policy whose priority is not a strict order
    (see ``test_primary_lane_tiebreak_*`` below, which exercises both levels
    directly).

    The tie_break list's third level, stable story id, cannot break a tie
    between LANES for one story: story_id is a single constant value for the
    whole candidate, so it cannot distinguish between its own lanes. That
    level is enforced where it is actually reachable instead: ordering
    same-lane, same-score entries inside one settled slate (see
    ``_invariant_slate``).
    """
    if not lane_scores:
        raise ContractViolation("invariant: cannot select a primary lane with no qualified lanes")

    def rank(lane: str) -> int:
        return priority.index(lane) if lane in priority else len(priority)

    best_rank = min(rank(lane) for lane in lane_scores)
    tied = sorted(lane for lane in lane_scores if rank(lane) == best_rank)
    if len(tied) == 1:
        return tied[0]
    best_score = max(lane_scores[lane] for lane in tied)
    winners = sorted(lane for lane in tied if lane_scores[lane] == best_score)
    return winners[0]


def _check_bands_are_complete_and_recomputed(bands: list) -> None:
    """Shared by Slate and RankingReceipt: no self-certified band verdicts.

    Every settled run must carry all seven SC-20 bands (an empty or partial
    list is rejected, not silently accepted), and every active band's
    ``verdict`` must equal what ``achieved`` actually says against its own
    ``floor``/``cap`` -- the receipt cannot simply assert ``pass``.
    """
    band_names = {b["band"] for b in bands}
    missing = set(SC20_BANDS) - band_names
    if missing:
        raise ContractViolation(
            f"invariant: all seven SC-20 bands must be present, missing {sorted(missing)}"
        )
    for band in bands:
        if not band["active"]:
            if band["verdict"] == "disabled" and not band.get("exception_reason", "").strip():
                raise ContractViolation(
                    f"invariant: band {band['band']} verdict disabled requires a non-empty exception_reason"
                )
            continue
        achieved = band.get("achieved")
        if achieved is None:
            raise ContractViolation(
                f"invariant: active band {band['band']} must carry a non-null achieved value"
            )
        floor = band.get("floor")
        cap = band.get("cap")
        within_bounds = (floor is None or achieved >= floor) and (cap is None or achieved <= cap)
        expected_verdict = "pass" if within_bounds else "fail"
        if band["verdict"] != expected_verdict:
            raise ContractViolation(
                f"invariant: band {band['band']} verdict must equal the recomputed "
                f"achieved-vs-bound result ({expected_verdict})"
            )


def _publication_idempotency_key(identity: dict) -> str:
    """The frozen derivation: identity alone, never the content digest."""
    return (
        f"pub-{identity['tenant_id']}|{identity['publisher_identity_ref']}|"
        f"{identity['destination']}|{identity['issue_date']}"
    )


def _invariant_event_semantics(p: dict) -> None:
    weak_or_passive = (
        p["default_confidence"] == ConfidenceBand.WEAK.value
        or p["default_evidence_class"] == EvidenceClass.PASSIVE.value
    )
    if weak_or_passive and p.get("can_mark_read", False):
        raise ContractViolation(
            "invariant: a passive or weak event may not set can_mark_read"
        )
    if p["event_type"] == EventType.LESS_LIKE_THIS.value and p.get(
        "creates_global_source_block", False
    ):
        raise ContractViolation(
            "invariant: less_like_this may not create a global source block"
        )


def _invariant_search_response(p: dict) -> None:
    if p["outcome"] == "error" and not p.get("error_code"):
        raise ContractViolation("invariant: outcome error requires a non-empty error_code")
    if p["outcome"] == "ok" and p.get("error_code"):
        raise ContractViolation("invariant: outcome ok must carry no error_code")


def _invariant_merged_candidate(p: dict) -> None:
    scored_lanes = set(_pairs(p["lane_scores"]))
    if p["primary_lane"] not in scored_lanes:
        raise ContractViolation("invariant: primary_lane must appear in lane_scores")
    for lane in p.get("secondary_lanes", []):
        if lane not in scored_lanes:
            raise ContractViolation("invariant: every secondary lane must appear in lane_scores")
    # A primary_lane that IS in lane_scores can still be the WRONG lane: the
    # validator must recompute the tie-break winner rather than trust the
    # claim (reproduced: a receipt naming any other qualified lane passed).
    scores = {lane: score for lane, score in p["lane_scores"]}
    priority = _policy()["edition"]["lane_priority"]
    expected = _primary_lane_from_tiebreak(scores, priority)
    if p["primary_lane"] != expected:
        raise ContractViolation(
            "invariant: primary_lane must be the recomputed tie-break winner "
            "from lane_priority then lane_score"
        )


def _check_component_scores(components: dict) -> None:
    """SC-08 transparency stated as arithmetic, not as a promise.

    The persisted per-component value is the WEIGHTED contribution (the
    scorer's 0.0-1.0 normalized value times that component's policy weight),
    so two things are checkable straight from policy revision 1:

    1. A contribution can never exceed ``weight * cap``, its ceiling.
    2. ``final_score`` must equal the signed composition of the contributions,
       positives minus penalties.

    Without (2) a "transparent, authoritative" score set can carry eight zeroed
    components under a ``final_score`` of 0.99, which is the number the ranking
    receipt replays the whole edition from.
    """
    policy_components = _policy()["components"]
    for name in POSITIVE_COMPONENTS + PENALTY_COMPONENTS:
        value = components[name]
        configured = policy_components[name]
        ceiling = configured["weight"] * configured["cap"]
        if value < -COMPOSITION_TOLERANCE:
            raise ContractViolation(
                f"invariant: component {name} must not be negative; a penalty is "
                "recorded as a positive magnitude and subtracted"
            )
        if value > ceiling + COMPOSITION_TOLERANCE:
            raise ContractViolation(
                f"invariant: component {name} exceeds its policy ceiling of "
                f"weight times cap ({ceiling})"
            )
        if not configured["enabled"] and abs(value) > COMPOSITION_TOLERANCE:
            raise ContractViolation(
                f"invariant: component {name} is disabled in policy and must record 0.0"
            )
    composed = sum(components[name] for name in POSITIVE_COMPONENTS) - sum(
        components[name] for name in PENALTY_COMPONENTS
    )
    if abs(components["final_score"] - composed) > COMPOSITION_TOLERANCE:
        raise ContractViolation(
            "invariant: final_score must equal the weighted composition of its "
            f"components ({composed})"
        )


def _invariant_scored_candidate(p: dict) -> None:
    if p["authoritative"] and p["scorer_kind"] != ScorerKind.TRANSPARENT.value:
        raise ContractViolation(
            "invariant: only ScorerKind.TRANSPARENT may be authoritative"
        )
    _check_component_scores(p["components"])


def _invariant_slate(p: dict) -> None:
    # Every Slate fixture represents a SETTLED edition: only the final
    # invariant verifier permits a Slate to exist at all. Self-certified
    # bands (an empty list, or a verdict that doesn't match achieved) are
    # rejected before anything else is checked.
    _check_bands_are_complete_and_recomputed(p["bands"])
    failing = [b for b in p["bands"] if b["active"] and b["verdict"] == "fail"]
    if failing and p["verifier_verdict"] == "pass":
        raise ContractViolation(
            "invariant: a slate with any failing active band cannot carry a passing verifier verdict"
        )
    quotas = {lane: count for lane, count in p.get("lane_quotas", [])}
    if quotas:
        total_quota = sum(quotas.values())
        # Quotas are CAPS, not exact counts: a lane whose ranked pool is
        # exhausted ships short rather than borrowing another lane's quota.
        if len(p["entries"]) > total_quota:
            raise ContractViolation(
                "invariant: the settled entry count must not exceed the summed lane quotas"
            )
        if len(p["entries"]) < total_quota and not p.get("short_reason_code", "").strip():
            raise ContractViolation(
                "invariant: an edition shorter than its summed lane quotas must record a short_reason_code"
            )
        for lane in quotas:
            used = sum(1 for entry in p["entries"] if entry["primary_lane"] == lane)
            if used > quotas[lane]:
                raise ContractViolation(f"invariant: lane {lane} exceeded its quota")
    positions = [entry["position"] for entry in p["entries"]]
    if positions != list(range(1, len(positions) + 1)):
        raise ContractViolation("invariant: slate positions must be 1..n with no gaps")
    story_ids = [entry["story_id"] for entry in p["entries"]]
    if len(set(story_ids)) != len(story_ids):
        raise ContractViolation("invariant: a story may appear at most once in a slate")
    # The tie_break list's third level, stable story id: reachable HERE.
    # Within one lane, entries must be ordered by final_score descending then
    # story_id ascending, so a floating-point or backfill tie still replays
    # deterministically.
    by_lane: dict[str, list[dict]] = {}
    for entry in p["entries"]:
        by_lane.setdefault(entry["primary_lane"], []).append(entry)
    for lane, lane_entries in by_lane.items():
        expected_order = sorted(lane_entries, key=lambda e: (-e["final_score"], e["story_id"]))
        if [e["story_id"] for e in lane_entries] != [e["story_id"] for e in expected_order]:
            raise ContractViolation(
                f"invariant: lane {lane} entries must be ordered by final_score "
                "descending then story_id ascending"
            )


def _invariant_artifact_version(p: dict) -> None:
    parent = p.get("parent_version")
    if parent is not None and parent >= p["version"]:
        raise ContractViolation("invariant: parent_version must be strictly less than version")


def _invariant_mirror_receipt(p: dict) -> None:
    if p["state"] == "settled":
        if not p.get("readback_checksum") or p["readback_checksum"] != p["attempted_checksum"]:
            raise ContractViolation(
                "invariant: state settled requires readback_checksum == attempted_checksum"
            )
        if not p.get("settled_at"):
            raise ContractViolation("invariant: state settled requires settled_at")
    if p["state"] in ("conflict", "unknown") and p.get("settled_at"):
        raise ContractViolation("invariant: conflict and unknown never settle")
    # A receipt that names a prior attempt which ended in conflict or unknown
    # cannot become settled or writing without a recorded human (or otherwise
    # out-of-band) resolution. Its absence is exactly the automatic retry the
    # state machine forbids (reproduced: a same-idempotency-key retry off a
    # conflicted receipt passed and silently overwrote the target).
    if p.get("prior_receipt_ids") and p.get("prior_attempt_state") in ("conflict", "unknown"):
        if p["state"] in ("settled", "writing") and not p.get("resolution_ref"):
            raise ContractViolation(
                "invariant: resolving a conflict or unknown attempt into settled or "
                "writing requires a recorded resolution_ref"
            )


def _invariant_mirror_descriptor(p: dict) -> None:
    if p["write_mode"] == "overwrite_compare_and_set" and not p["proves_atomic_conditional_write"]:
        raise ContractViolation(
            "invariant: overwrite_compare_and_set requires proves_atomic_conditional_write"
        )


def _invariant_output_receipt(p: dict) -> None:
    if p["state"] == "unknown" and p["receipt_state"] != "unknown":
        raise ContractViolation("invariant: publication state unknown requires receipt_state unknown")
    if p["state"] == "settled":
        if p["receipt_state"] != "settled":
            raise ContractViolation("invariant: publication state settled requires receipt_state settled")
        # A write acknowledgement alone never settles a publish, the same rule
        # mirror.md enforces for mirrors: an acknowledgement_ref and settled_at
        # are both required, not just a matching receipt_state (reproduced: a
        # settled receipt with neither passed).
        if not p.get("acknowledgement_ref"):
            raise ContractViolation(
                "invariant: state settled requires a non-empty acknowledgement_ref"
            )
        if not p.get("settled_at"):
            raise ContractViolation("invariant: state settled requires settled_at")


def _invariant_output_descriptor(p: dict) -> None:
    if p.get("emits_public_content") and not p.get("requires_approval"):
        raise ContractViolation("invariant: an adapter emitting public content must require approval")


def _invariant_publication_record(p: dict) -> None:
    if p["state"] == "settled" and not p.get("authorization_id"):
        raise ContractViolation("invariant: state settled requires a non-null authorization_id")
    if p["state"] == "publishing" and not p.get("idempotency_key"):
        raise ContractViolation("invariant: state publishing requires an idempotency key")
    # The at-most-once key is a typed derivation, not a free-form string: it
    # must equal tenant|publisher|destination|issue_date and MUST NOT embed
    # the digest, which is exactly what would double-send an issue whose
    # basket changed and was re-authorized.
    if p.get("idempotency_key"):
        expected_key = _publication_idempotency_key(p["identity"])
        if p["idempotency_key"] != expected_key:
            raise ContractViolation(
                "invariant: idempotency_key must be derived from the publication "
                "identity alone, never the content digest"
            )
    # Transition legality, wired into validation instead of left as a dead
    # constant: an illegal edge (in particular unknown -> publishing, which
    # would be a silent retry of an ambiguous send) is rejected.
    prior = p.get("prior_state")
    if prior:
        if (prior, p["state"]) not in PUBLICATION_TRANSITIONS:
            raise ContractViolation(
                f"invariant: illegal publication transition from {prior} to {p['state']}"
            )
        if prior == "unknown":
            verdict = p.get("readback_verdict")
            if p["state"] == "settled" and verdict != ReadbackVerdict.POSITIVE.value:
                raise ContractViolation(
                    "invariant: leaving unknown for settled requires a positive readback_verdict"
                )
            if p["state"] == "failed-safe" and verdict != ReadbackVerdict.NEGATIVE_CONCLUSIVE.value:
                raise ContractViolation(
                    "invariant: leaving unknown for failed-safe requires a "
                    "negative_conclusive readback_verdict"
                )


def _invariant_publishing_basket(p: dict) -> None:
    if p.get("readiness_signal") and len(p["item_story_ids"]) < p["required_item_count"]:
        raise ContractViolation("invariant: readiness requires the configured item count")
    # A basket is a container of items: draft or ready, never further. Every
    # state past ready (authorized, publishing, settled, failed-safe, unknown)
    # belongs to the PublicationRecord, not the container -- a basket in one
    # of those states would let a container of items impersonate a
    # publication record (should-fix: only "authorized" was checked before).
    if p["state"] not in ("draft", "ready"):
        raise ContractViolation(
            "invariant: a basket is draft or ready at most; every state past "
            "ready lives on the publication record"
        )


def _invariant_ranking_receipt(p: dict) -> None:
    if p["envelope"]["state"] == "settled":
        authoritative_ids = {
            score["story_id"]
            for score in p["transparent_scores"]
            if score["authoritative"] and score["scorer_kind"] == ScorerKind.TRANSPARENT.value
        }
        if not authoritative_ids:
            raise ContractViolation(
                "invariant: a settled ranking receipt requires at least one authoritative transparent score"
            )
        final_ids = {entry["story_id"] for entry in p["final_order"]}
        missing_scores = final_ids - authoritative_ids
        if missing_scores:
            raise ContractViolation(
                "invariant: every final_order entry requires an authoritative transparent "
                f"score, missing {sorted(missing_scores)}"
            )
    for score in p.get("shadow_scores", []):
        if score["authoritative"]:
            raise ContractViolation("invariant: a shadow score set is never authoritative")
    # Every persisted score set on the receipt composes, shadow ones included:
    # a shadow set with an uncomposed final_score would be evaluated against a
    # number its own components never produced.
    for score in list(p.get("transparent_scores", [])) + list(p.get("shadow_scores", [])):
        _check_component_scores(score["components"])
    scored = {(row[0], row[1]) for row in p["lane_scores"]}
    for story_id, lane in p["primary_lane_by_story"]:
        if (story_id, lane) not in scored:
            raise ContractViolation(
                "invariant: every primary lane must be replayable from the persisted lane scores"
            )
    # A primary lane that IS a scored (story_id, lane) pair can still be the
    # WRONG lane for that story: recompute the winner from this receipt's own
    # lane_priority, don't trust the claim (reproduced: naming the wrong
    # qualified lane as primary passed).
    per_story_scores: dict[str, dict[str, float]] = {}
    for story_id, lane, score in p["lane_scores"]:
        per_story_scores.setdefault(story_id, {})[lane] = score
    priority = list(p["lane_priority"])
    for story_id, lane in p["primary_lane_by_story"]:
        expected = _primary_lane_from_tiebreak(per_story_scores[story_id], priority)
        if lane != expected:
            raise ContractViolation(
                "invariant: primary_lane_by_story must be the recomputed tie-break "
                "winner from lane_priority then lane_score"
            )
    if p["envelope"]["state"] == "settled":
        _check_bands_are_complete_and_recomputed(p["bands"])


def _invariant_deletion_receipt(p: dict) -> None:
    unresolved = [row for row in p["projections"] if not row["resolved"]]
    if p["envelope"]["state"] == "settled":
        if unresolved:
            raise ContractViolation(
                "invariant: a deletion receipt with any unresolved projection cannot settle"
            )
        # A settled receipt that enumerates NOTHING (or only some of the seven
        # projections) is the false-deletion case the receipt exists to catch:
        # a row that is never listed is not "unresolved", so leaving one out
        # cannot be used to get a clean-looking green receipt.
        present = {row["projection"] for row in p["projections"]}
        missing = set(DELETION_PROJECTION_VOCABULARY) - present
        if missing:
            raise ContractViolation(
                f"invariant: a settled deletion receipt must enumerate all seven "
                f"projections, missing {sorted(missing)}"
            )
        if p["zero_contribution_verdict"] is not True:
            raise ContractViolation(
                "invariant: a settled deletion receipt requires zero_contribution_verdict true"
            )
        if p.get("audit_chain_queryable") is not True:
            raise ContractViolation(
                "invariant: a settled deletion receipt requires audit_chain_queryable true"
            )
        # Mirror reconciliation: every mirrored_targets entry must be
        # accounted for by the mirrors projection's own target_ref rows.
        mirrored = set(p.get("mirrored_targets", ()))
        mirrors_targets = {
            row.get("target_ref", "") for row in p["projections"] if row["projection"] == "mirrors"
        }
        unaccounted = mirrored - mirrors_targets
        if unaccounted:
            raise ContractViolation(
                "invariant: every mirrored_targets entry must appear as a mirrors "
                f"projection target_ref, missing {sorted(unaccounted)}"
            )
        settled_at = p["envelope"].get("settled_at")
        if not settled_at:
            raise ContractViolation(
                "invariant: a settled deletion receipt requires envelope.settled_at"
            )
        if datetime.fromisoformat(settled_at) < datetime.fromisoformat(p["correction_watermark"]):
            raise ContractViolation(
                "invariant: settled_at must not precede correction_watermark"
            )
    for row in unresolved:
        if not row.get("user_visible_disclosure"):
            raise ContractViolation(
                "invariant: an unresolved projection must carry a user-visible disclosure"
            )


def _invariant_principal_claims(p: dict) -> None:
    if datetime.fromisoformat(p["expires_at"]) <= datetime.fromisoformat(p["issued_at"]):
        raise ContractViolation("invariant: expires_at must be after issued_at")


def _invariant_source_checkpoint(p: dict) -> None:
    if p["state"] == "settled" and not p.get("cursor"):
        raise ContractViolation("invariant: a settled checkpoint requires a cursor")
    if p["state"] == "settled" and not p.get("health_receipt_id"):
        raise ContractViolation("invariant: a settled checkpoint requires a health_receipt_id")
    if p["state"] == "uninitialized" and (p.get("cursor") or p.get("watermark")):
        raise ContractViolation("invariant: an uninitialized checkpoint carries no cursor")


def _invariant_import_inventory_receipt(p: dict) -> None:
    if not p.get("import_enabled"):
        return
    if p["envelope"]["state"] != "settled":
        raise ContractViolation("invariant: import_enabled requires a settled receipt")
    if not p.get("credential_verified"):
        raise ContractViolation("invariant: import_enabled requires credential_verified true")
    if p.get("coverage_window_start") is None or p.get("coverage_window_end") is None:
        raise ContractViolation("invariant: import_enabled requires a complete coverage window")
    if p.get("missing_fields"):
        raise ContractViolation("invariant: import_enabled requires no missing_fields")
    if p.get("evidence_grade") not in ("A", "B"):
        raise ContractViolation(
            "invariant: import_enabled requires an accepted evidence_grade of A or B"
        )


def _invariant_evidence_item(p: dict) -> None:
    if p.get("corroborated") and not p.get("corroborating_evidence_ids"):
        raise ContractViolation(
            "invariant: corroborated requires at least one corroborating_evidence_id"
        )


def _invariant_learning_event(p: dict) -> None:
    """SC-11 / SC-11B stated on the DATA, not only the reference table.

    ``EventSemantics`` freezes the shape; this checks that one recorded event
    actually matches its event_type's frozen row in policy revision 1's
    ``event_weights`` (class, origin, confidence), so a weak imported visit
    cannot be written as a live explicit strong event. The one legal
    departure is the recorded corroboration promotion (``promotable_to``),
    never an arbitrary confidence bump.
    """
    weights = _policy()["event_weights"]
    expected = weights.get(p["event_type"])
    if expected is None:
        raise ContractViolation(
            f"invariant: {p['event_type']} has no frozen event-semantics row in policy revision 1"
        )
    if p["evidence_class"] != expected["class"] or p["origin"] != expected["origin"]:
        raise ContractViolation(
            "invariant: evidence_class and origin must match the frozen event-semantics "
            "row for this event_type"
        )
    allowed_confidence = {expected["confidence"]}
    promoted = expected.get("promotable_to")
    if promoted:
        allowed_confidence.add(promoted)
    if p["confidence"] not in allowed_confidence:
        raise ContractViolation(
            "invariant: confidence must match the frozen event-semantics row, or its "
            "recorded promotion, for this event_type"
        )


INVARIANTS = {
    "EventSemantics": _invariant_event_semantics,
    "SearchResponse": _invariant_search_response,
    "MergedCandidate": _invariant_merged_candidate,
    "ScoredCandidate": _invariant_scored_candidate,
    "Slate": _invariant_slate,
    "ArtifactVersion": _invariant_artifact_version,
    "MirrorReceipt": _invariant_mirror_receipt,
    "MirrorAdapterDescriptor": _invariant_mirror_descriptor,
    "OutputReceipt": _invariant_output_receipt,
    "OutputAdapterDescriptor": _invariant_output_descriptor,
    "PublicationRecord": _invariant_publication_record,
    "PublishingBasket": _invariant_publishing_basket,
    "RankingReceipt": _invariant_ranking_receipt,
    "DeletionReceipt": _invariant_deletion_receipt,
    "ImportInventoryReceipt": _invariant_import_inventory_receipt,
    "PrincipalClaims": _invariant_principal_claims,
    "SourceCheckpoint": _invariant_source_checkpoint,
    "EvidenceItem": _invariant_evidence_item,
    "LearningEvent": _invariant_learning_event,
}


def validate_fixture(document: dict) -> None:
    """Full check: structure first, then the named invariants for that type."""
    name = document["dataclass"]
    cls = getattr(contracts, name, None)
    if cls is None:
        raise ContractViolation(f"{name} is not exported by curator.contracts")
    validate_payload(cls, document["payload"])
    invariant = INVARIANTS.get(name)
    if invariant is not None:
        invariant(document["payload"])


# ---------------------------------------------------------------------------
# Fixture collection
# ---------------------------------------------------------------------------


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_ROOT.rglob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


FIXTURE_PATHS = _fixture_paths()


def _fixture_id(path: Path) -> str:
    return str(path.relative_to(FIXTURE_ROOT))


def _normalize_reason(text: str) -> str:
    """Compare rejection reasons without quoting or spacing noise."""
    return " ".join(text.replace("'", "").replace('"', "").lower().split())


# ---------------------------------------------------------------------------
# 1. Inventory
# ---------------------------------------------------------------------------


def test_twelve_contracts_are_frozen():
    assert len(FROZEN_CONTRACTS) == 12
    names = [row[0] for row in FROZEN_CONTRACTS]
    assert len(set(names)) == 12


@pytest.mark.parametrize("name,module,doc", FROZEN_CONTRACTS, ids=[r[0] for r in FROZEN_CONTRACTS])
def test_each_contract_has_prose_module_and_both_fixture_kinds(name, module, doc):
    assert (REPO_ROOT / doc).is_file(), f"{doc} is missing"
    __import__(module)
    directory = FIXTURE_ROOT / name
    assert directory.is_dir(), f"no fixture directory for {name}"
    files = [p.name for p in directory.glob("*.json")]
    assert any(f.startswith("valid-") for f in files), f"{name} has no valid fixture"
    assert any(f.startswith("invalid-") for f in files), f"{name} has no invalid fixture"


def test_fixture_corpus_is_non_trivial():
    assert len(FIXTURE_PATHS) >= 24


# ---------------------------------------------------------------------------
# 2. Fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=[_fixture_id(p) for p in FIXTURE_PATHS])
def test_fixture_envelope_is_well_formed(path):
    document = _load(path)
    for key in ("contract", "dataclass", "expect", "note", "payload"):
        assert key in document, f"{path.name} is missing {key}"
    assert document["expect"] in ("valid", "invalid")
    assert document["contract"] == path.parent.name
    assert path.name.startswith(document["expect"] + "-")
    assert document["note"].strip(), "every fixture states why it exists"
    if document["expect"] == "invalid":
        assert document["violates"].strip(), "an invalid fixture names what it violates"


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=[_fixture_id(p) for p in FIXTURE_PATHS])
def test_fixture_matches_its_declared_verdict(path):
    document = _load(path)
    if document["expect"] == "valid":
        validate_fixture(document)
        return
    with pytest.raises(ContractViolation) as caught:
        validate_fixture(document)
    # The rejection reason must be the one the fixture claims, not an
    # accidental second defect that would let the real violation slip through.
    claimed = _normalize_reason(document["violates"])
    actual = _normalize_reason(str(caught.value))
    assert claimed in actual, (
        f"{path.name} was rejected for {caught.value!r}, not for {document['violates']!r}"
    )


def test_validator_has_a_positive_control():
    """A seeded defect must be detected before any clean result counts."""
    good = _load(FIXTURE_ROOT / "tenant" / "valid-private-tenant.json")
    validate_fixture(good)
    seeded = json.loads(json.dumps(good))
    seeded["payload"]["default_publication_class"] = "shared"
    with pytest.raises(ContractViolation):
        validate_fixture(seeded)


def test_overlapping_lane_fixture_exists_and_is_reproducible():
    """SC-24: the overlap case is the one the whole tie-break rule exists for."""
    document = _load(
        FIXTURE_ROOT / "candidate" / "valid-merged-candidate-overlap-updates-hot.json"
    )
    payload = document["payload"]
    scores = dict(payload["lane_scores"])
    assert Lane.UPDATES.value in scores and Lane.HOT.value in scores
    # Hot scores higher, yet Updates wins on lane priority. If a future change
    # made score the first tie-break, this assertion is what catches it.
    assert scores[Lane.HOT.value] > scores[Lane.UPDATES.value]
    assert payload["primary_lane"] == Lane.UPDATES.value
    assert payload["tie_break_applied"] == "lane_priority"

    policy = _policy()
    priority = policy["edition"]["lane_priority"]
    replayed = min(scores, key=lambda lane: priority.index(lane))
    assert replayed == payload["primary_lane"]


def test_primary_lane_tiebreak_priority_level_decides():
    """Level 1: configured priority wins even against a higher lane_score."""
    winner = _primary_lane_from_tiebreak(
        {"updates": 0.5, "hot": 0.9}, ["updates", "hot", "interested", "surprise"]
    )
    assert winner == "updates"


def test_primary_lane_tiebreak_score_level_decides():
    """Level 2: reachable only when priority ties, which revision 1's strict
    permutation priority never does in production -- exercised directly here
    so the branch is proven correct rather than aspirational."""
    winner = _primary_lane_from_tiebreak({"hot": 0.4, "interested": 0.9}, ["updates"])
    assert winner == "interested"


def test_primary_lane_tiebreak_is_deterministic_when_priority_and_score_both_tie():
    """Neither level decides; the rule still returns one stable answer,
    independent of dict iteration order."""
    assert _primary_lane_from_tiebreak({"hot": 0.5, "interested": 0.5}, ["updates"]) == "hot"
    assert _primary_lane_from_tiebreak({"interested": 0.5, "hot": 0.5}, ["updates"]) == "hot"


def test_action_matrix_has_fourteen_rows_covering_every_scope():
    assert len(ACTION_MATRIX) == 14
    actions = [row.action for row in ACTION_MATRIX]
    assert len(set(actions)) == 14, "every action name must be unique"
    scopes_covered = {row.required_scope for row in ACTION_MATRIX}
    assert scopes_covered == set(Scope), "every scope must be covered by at least one action"


def test_action_matrix_delete_requires_idempotency_and_separate_credential():
    delete_row = next(row for row in ACTION_MATRIX if row.action == "delete_data")
    assert delete_row.requires_idempotency_key is True
    assert delete_row.requires_separate_credential is True
    assert delete_row.requires_dry_run_first is True


def test_action_matrix_approve_and_execute_never_share_a_row():
    approve_actions = {row.action for row in ACTION_MATRIX if row.required_scope == Scope.PUBLISH_APPROVE}
    execute_actions = {row.action for row in ACTION_MATRIX if row.required_scope == Scope.PUBLISH_EXECUTE}
    assert approve_actions.isdisjoint(execute_actions)


def test_action_matrix_denies_an_action_absent_from_the_matrix():
    """SC-34: an action absent from the matrix fails closed. There is no
    fallback lookup path in ACTION_MATRIX for an unknown action name."""
    known_actions = {row.action for row in ACTION_MATRIX}
    assert "delete_everything_no_scope_required" not in known_actions


def test_no_absolute_owner_paths_or_provider_names_in_fixtures():
    """Core contract fixtures stay vendor-neutral and carry no private paths.

    Two halves, both implemented:

    1. No absolute owner home path.
    2. No provider name in a CORE field name or a CORE field value. The
       adapter-identity exception is the allowlist below; everything else
       fails, so a NEW provider leak cannot ride in on an old exemption.
    """
    for path in FIXTURE_PATHS:
        text = path.read_text(encoding="utf-8")
        for token in OWNER_PATH_TOKENS:
            assert token not in text, f"{path.name} contains {token}"
        leaks = _provider_leaks_in_json(
            _load(path), file_key=str(path.relative_to(REPO_ROOT))
        )
        assert not leaks, f"{path.name}: provider name in a core field: {leaks}"


def test_no_provider_names_in_typed_core_definitions():
    """The other half of the same rule, stated on the typed definitions.

    A fixture is data; ``curator/contracts`` is the shape itself. A provider
    name in a dataclass field name, a string default, an enum member name, or
    an enum value would bake a vendor into the contract, which is exactly what
    ``enums.py``'s own docstring forbids.
    """
    leaks: list[str] = []
    for module_path in sorted((REPO_ROOT / "curator" / "contracts").glob("*.py")):
        if module_path.stem == "__init__":
            continue
        file_key = str(module_path.relative_to(REPO_ROOT))
        module = importlib.import_module(f"curator.contracts.{module_path.stem}")
        for attribute_name, obj in vars(module).items():
            if attribute_name.startswith("_") or getattr(obj, "__module__", None) != module.__name__:
                continue
            if isinstance(obj, type) and issubclass(obj, Enum):
                for member in obj:
                    leaks += [
                        f"{file_key}:{obj.__name__}.{member.name}={hit}"
                        for hit in _provider_tokens_in(member.name) + _provider_tokens_in(str(member.value))
                    ]
                continue
            if dataclasses.is_dataclass(obj):
                for field in dataclasses.fields(obj):
                    if (file_key, field.name) in ADAPTER_IDENTITY_ALLOWLIST:
                        continue
                    candidates = [field.name]
                    if isinstance(field.default, str):
                        candidates.append(field.default)
                    for candidate in candidates:
                        leaks += [
                            f"{file_key}:{obj.__name__}.{field.name}={hit}"
                            for hit in _provider_tokens_in(candidate)
                        ]
    assert not leaks, f"provider names in the typed core definitions: {leaks}"


def test_adapter_identity_allowlist_exempts_only_the_named_field():
    """The exception is proven, not assumed.

    Revision 1's fixtures deliberately use neutral adapter ids, so no fixture
    exercises the allowlist today. This drives the helper directly: the
    allowlisted field may carry a provider name; the SAME payload's other
    fields may not.
    """
    exempt_file, exempt_field = "curator/contracts/mirror.py", "adapter_id"
    assert (exempt_file, exempt_field) in ADAPTER_IDENTITY_ALLOWLIST
    payload = {"payload": {exempt_field: "notion-workspace"}}
    assert _provider_leaks_in_json(payload, file_key=exempt_file) == []
    payload = {"payload": {"reason_code": "notion-workspace"}}
    assert _provider_leaks_in_json(payload, file_key=exempt_file) != []


def test_owner_identifiers_are_absent_from_the_whole_frozen_set():
    """SC-01, widened past the absolute home path (should-fix SF-2).

    ``docs/contracts`` ships to the PUBLIC repo, so the owner's initials are
    as much a leak as a home path: they were in every fixture's tenant id and
    in three product-decision comments. The tenant id is now
    ``tenant-owner-private`` and the comments say "the owner". This test is
    what stops either from coming back, and it covers the whole frozen set,
    not only the fixtures.
    """
    offenders: list[str] = []
    for path in _freeze_paths():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT)
        for token in OWNER_PATH_TOKENS:
            if token in text:
                offenders.append(f"{relative}: {token}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _OWNER_INITIALS.search(line):
                offenders.append(f"{relative}:{line_number}: owner initials")
    assert not offenders, f"owner identifiers in the frozen set: {offenders}"


# ---------------------------------------------------------------------------
# 3. Policy revision 1
# ---------------------------------------------------------------------------


def _policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_policy_file_exists_and_is_revision_one():
    policy = _policy()
    assert policy["policy"]["revision"] == 1
    assert policy["policy"]["status"] == "frozen"


def test_every_sc20_band_is_present_with_concrete_values():
    bands = _policy()["bands"]
    assert set(bands) == set(SC20_BANDS)
    for name, band in bands.items():
        assert isinstance(band["active"], bool), f"{name}: active must be explicit"
        assert band["measure"], f"{name}: needs a named measure"
        has_bound = band.get("floor") is not None or band.get("cap") is not None
        assert has_bound, f"{name}: a band with neither floor nor cap bounds nothing"
        if band.get("floor") is not None and band.get("cap") is not None:
            assert band["floor"] <= band["cap"], f"{name}: floor above cap"


def test_sc08a_required_bands_are_active_without_exception():
    policy = _policy()
    bands = policy["bands"]
    exceptions = policy["band_exceptions"] or []
    exception_bands = {row["band"] for row in exceptions} if exceptions else set()
    for name in SC08A_REQUIRED_ACTIVE:
        assert bands[name]["required_active"] is True, f"{name} must be marked required_active"
        assert bands[name]["active"] is True, f"{name} must be ACTIVE (SC-08A)"
        assert name not in exception_bands, f"{name} carries an exception but is required active"


def test_band_exceptions_are_recorded_not_implied():
    """A required band may be disabled only through a recorded exception row."""
    policy = _policy()
    exceptions = policy["band_exceptions"] or []
    for row in exceptions:
        assert row["band"] in SC20_BANDS
        assert row.get("reason", "").strip(), "an exception without a reason is a silent default"
        assert row.get("policy_revision") == policy["policy"]["revision"]
    for name, band in policy["bands"].items():
        if band["required_active"] and not band["active"]:
            assert any(row["band"] == name for row in exceptions), (
                f"{name} is required-active and disabled with no recorded exception"
            )


def test_transparent_scorer_components_are_complete_and_weighted():
    components = _policy()["components"]
    expected = {
        "relevance",
        "freshness",
        "trend",
        "editor_consensus",
        "deliberate_surprise",
        "diversity",
        "repetition_penalty",
        "source_fatigue_penalty",
    }
    assert set(components) == expected
    for name, component in components.items():
        assert isinstance(component["enabled"], bool), f"{name}: enablement must be explicit"
        assert component["weight"] >= 0.0
        assert component["cap"] > 0.0
    positive = [
        "relevance",
        "freshness",
        "trend",
        "editor_consensus",
        "deliberate_surprise",
        "diversity",
    ]
    total = round(sum(components[name]["weight"] for name in positive), 6)
    assert total == 1.0, f"positive component weights sum to {total}, not 1.0"


def test_protected_exploration_allocation_is_real_and_gated():
    policy = _policy()
    exploration = policy["exploration"]
    assert exploration["protected_min_slots"] >= 1, "exploration must be structurally protected"
    assert exploration["protected_min_slots"] <= exploration["protected_max_slots"]
    assert exploration["protected_max_slots"] <= policy["edition"]["size"]
    gates = exploration["gates"]
    for key in ("max_age_hours", "min_source_weight", "require_echo_eligible"):
        assert key in gates, f"exploration gate {key} is missing"
    # Random low-quality content is not exploration.
    assert gates["max_age_hours"] > 0
    assert gates["require_echo_eligible"] is True


def test_repetition_and_fatigue_windows_are_configured():
    policy = _policy()
    repetition = policy["repetition"]
    assert repetition["key"] == "story_cluster_id", "repetition clusters stories, never raw URLs"
    assert repetition["window_hours"] > 0
    assert repetition["max_appearances_per_window"] >= 1
    fatigue = policy["source_fatigue"]
    assert fatigue["window_hours"] > 0
    # Measured need: the highest-volume route is ~20.6% of the in-window pool.
    per_edition = fatigue["max_items_per_source_per_edition"]
    assert per_edition / policy["edition"]["size"] < 0.206, (
        "the per-source cap must sit below the share the top route would otherwise take"
    )
    assert fatigue["max_items_per_aggregator_per_edition"] <= per_edition


def test_lane_quotas_and_priority_agree_with_the_edition():
    policy = _policy()
    quotas = policy["lane_quotas"]
    lanes = {lane.value for lane in Lane}
    assert set(quotas) == lanes
    assert sum(quotas.values()) == policy["edition"]["size"]
    priority = policy["edition"]["lane_priority"]
    assert sorted(priority) == sorted(lanes), "lane priority must cover every lane exactly once"
    assert policy["edition"]["default_lane"] == Lane.UPDATES.value, "fresh visits open on Updates"
    # Surprise quota and protected exploration must not contradict each other.
    assert quotas[Lane.SURPRISE.value] >= policy["exploration"]["protected_min_slots"]
    assert policy["backfill"]["allow_cross_lane_borrow"] is False


def test_every_event_type_has_a_weight_and_a_frozen_class():
    weights = _policy()["event_weights"]
    assert set(weights) == {event.value for event in EventType}
    classes = {member.value for member in EvidenceClass}
    bands = {member.value for member in ConfidenceBand}
    for name, row in weights.items():
        assert row["class"] in classes, f"{name}: {row['class']} is not an SC-04 evidence class"
        assert row["confidence"] in bands, f"{name}: unknown confidence band"
        assert row["origin"] in ("live", "imported")
        assert isinstance(row["weight"], (int, float))


def test_passive_events_cannot_mark_read_and_imported_events_start_disabled():
    weights = _policy()["event_weights"]
    for name in ("dwell", "scroll"):
        assert weights[name]["can_mark_read"] is False, f"{name} may never mark a story read"
    assert weights["less_like_this"]["creates_global_source_block"] is False
    assert weights["imported_mail_unread_state"]["can_create_opened_at"] is False
    for name in ("imported_mail_unread_state", "imported_browser_visit"):
        assert weights[name]["origin"] == "imported"
        assert weights[name]["confidence"] == ConfidenceBand.WEAK.value
        assert weights[name]["enabled"] is False, (
            f"{name} must stay disabled until its inventory receipt is complete"
        )


def test_browser_corroboration_policy_is_present_and_fail_closed():
    """SC-11B: both fields required, and their absence keeps a visit weak."""
    corroboration = _policy()["browser_corroboration"]
    assert corroboration["browser_session_gap_minutes"] > 0
    assert corroboration["browser_return_min_distinct_sessions"] >= 2
    assert corroboration["require_both_policy_fields"] is True
    assert corroboration["promoting_events"], "an independent explicit action must promote"
    weights = _policy()["event_weights"]
    for event_name in corroboration["promoting_events"]:
        assert event_name in weights, f"{event_name} is not a known event type"


def test_decay_windows_are_ordered_by_confidence():
    decay = _policy()["decay"]
    assert decay["strong_half_life_days"] > decay["medium_half_life_days"]
    assert decay["medium_half_life_days"] > decay["weak_half_life_days"]
    assert 0.0 < decay["floor"] < 1.0
