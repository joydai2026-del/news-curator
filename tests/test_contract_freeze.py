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
import unicodedata
from collections import Counter
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest
import yaml

from curator import contracts
from curator.contracts import (
    ACTION_MATRIX,
    FROZEN_CONTRACTS,
    KIND_BOUND_RECORDS,
    OWNED_RECORDS,
    OWNERSHIP_CLASSIFICATION_REASONS,
    PUBLICATION_TRANSITIONS,
    INVISIBLE_ID_CODE_POINT_RANGES,
    INVISIBLE_ID_CODE_POINTS,
    RECEIPT_KIND_TIERS,
    RECEIPT_WRAPPER_KINDS,
    SUBJECT_BOUND_RECEIPT_KINDS,
    SUBJECT_BOUND_RECORDS,
    SUBJECTLESS_RECEIPT_KINDS,
    SUBJECTLESS_RECORDS,
    Ownership,
)
from curator.ownership import (
    INVISIBLE_ID_SQL_CLASS,
    _IDENTITY_ID_FIELDS,
    identity_violations,
    is_receipt_wrapper,
    noncanonical_id_reason,
    ownership_id_sql_check,
    ownership_violations,
    receipt_wrapper_violations,
)
from curator.contracts.enums import (
    ActorKind,
    ConfidenceBand,
    EventType,
    EvidenceClass,
    Lane,
    ReadbackVerdict,
    Scope,
    ScorerKind,
)
from scripts.render_contract_tables import GENERATED_TABLES, generated_markers

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


def _parse_iso8601(value: str) -> datetime:
    """Parse the frozen wire format consistently on Python 3.10 and later."""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed


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
                _parse_iso8601(value)
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

    # The shared ownership rule, applied by ISSUBCLASS rather than by a list of
    # names, so a private record added later cannot skip it by not being
    # listed. It runs here rather than only in ``validate_fixture`` so that a
    # NESTED owned record (a receipt's ``envelope``) is checked with the same
    # rule as a top-level one.
    if issubclass(cls, Ownership):
        _invariant_ownership(payload, cls)
    _invariant_receipt_wrapper_kind(payload, cls)


def test_datetime_validation_accepts_the_utc_z_wire_format():
    _check_value("2025-08-24T01:46:40.000Z", datetime, "created_at")
    _invariant_principal_claims(
        {
            "issued_at": "2025-08-24T01:46:40.000Z",
            "expires_at": "2025-08-24T02:46:40.000Z",
        }
    )
    with pytest.raises(ContractViolation, match="not an ISO-8601 timestamp"):
        _check_value("2025-08-24T01:46:40.000", datetime, "created_at")


# ---------------------------------------------------------------------------
# Named invariants: the rules a type signature cannot express
# ---------------------------------------------------------------------------


_WRAPPER_SPEC_BY_CLASS = {
    cls: (field_name, kind)
    for cls, field_name, kind in RECEIPT_WRAPPER_KINDS
}


def _is_envelope_annotation(annotation: object) -> bool:
    """Static check for ``ReceiptEnvelope`` in a frozen annotation tree.

    A total recursive walk, not a one-level ``get_args`` check: the one-level
    form saw ``ReceiptEnvelope | None`` but not ``list[ReceiptEnvelope | None]``,
    whose single arg is the union object rather than ``ReceiptEnvelope``
    itself. Round 6 reproduced that hole end to end through
    ``InMemoryLedgerStore``. Runtime code deliberately has no equivalent walk.
    """
    if annotation is contracts.ReceiptEnvelope:
        return True
    return any(_is_envelope_annotation(arg) for arg in typing.get_args(annotation))


def _envelope_field_names(cls: type) -> tuple[str, ...] | None:
    """Every field of ``cls`` whose RESOLVED TYPE is ``ReceiptEnvelope``.

    Not "a field called ``envelope``". Detection by NAME is what let an
    unlisted fifth wrapper fail OPEN twice: first because it was not one of
    the four pinned class names, then because it named its field
    ``receipt_envelope``. Annotations are resolved with ``get_type_hints``
    because every contract module uses ``from __future__ import annotations``,
    so ``field.type`` is a string. A UNION containing ``ReceiptEnvelope`` counts,
    so ``ReceiptEnvelope | None`` is an envelope field rather than an invisible
    one. Called only by the static freeze test over the closed wrapper tuple.

    ``None`` means a frozen class's annotations could not be resolved, which
    makes the static gate fail instead of guessing.
    """
    if not dataclasses.is_dataclass(cls):
        return ()
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        return None
    return tuple(
        field.name
        for field in dataclasses.fields(cls)
        if _is_envelope_annotation(hints.get(field.name))
    )


def _is_receipt_wrapper_class(cls: type) -> bool:
    """True only for an exact class in the frozen wrapper set."""
    return any(cls is wrapper_class for wrapper_class, _, _ in RECEIPT_WRAPPER_KINDS)


def _pinned_envelope_kind(cls: type) -> str | None:
    """The kind this exact frozen wrapper class is pinned to."""
    spec = _WRAPPER_SPEC_BY_CLASS.get(cls)
    return None if spec is None else spec[1]


def _invariant_receipt_wrapper_kind(p: dict, cls: type) -> None:
    """A receipt wrapper carries only the one envelope kind it is named for.

    Ownership alone cannot catch this: a ``DeletionReceipt`` whose envelope is
    stamped ``ranking`` is a TYPE MISMATCH, not a tier hole. Both kinds are
    subject-bound and both demand a non-blank ``user_id``, so every ownership
    check passed while the receipt's type and its envelope disagreed about what
    the receipt proves. Applied here rather than in ``INVARIANTS``
    so that ``LimitReceipt``, which has no other named invariant, is covered by
    the same rule as the other three.

    The exact class and field name come from the frozen map. The separate
    static freeze test proves every listed class has exactly one envelope-typed
    field; runtime and fixture validation do not infer wrapper identity from
    annotations.
    """
    spec = _WRAPPER_SPEC_BY_CLASS.get(cls)
    if spec is None:
        return
    field_name, expected = spec
    envelope = p.get(field_name)
    kind = envelope.get("kind") if isinstance(envelope, dict) else None
    if kind != expected:
        raise ContractViolation(
            f"invariant: {cls.__name__} requires an envelope of kind "
            f"{expected!r}, got {kind!r}"
        )


def _payload_is_subject_bound(p: dict, cls: type) -> bool:
    """The two-tier rule, decided from the frozen tuples, never from a hunch.

    Raises ``ContractViolation`` for an owned class in none of the three
    tuples, so a new private record cannot land unclassified and fall into the
    permissive tier by default.
    """
    if any(cls is frozen_class for frozen_class in SUBJECT_BOUND_RECORDS):
        return True
    if any(cls is frozen_class for frozen_class in SUBJECTLESS_RECORDS):
        return False
    if any(cls is frozen_class for frozen_class in KIND_BOUND_RECORDS):
        kind = p.get("kind")
        if kind in SUBJECT_BOUND_RECEIPT_KINDS:
            return True
        if kind in SUBJECTLESS_RECEIPT_KINDS:
            return False
        raise ContractViolation(
            f"invariant: {cls.__name__} receipt kind {kind!r} is classified in neither "
            "SUBJECT_BOUND_RECEIPT_KINDS nor SUBJECTLESS_RECEIPT_KINDS"
        )
    raise ContractViolation(
        f"invariant: {cls.__name__} inherits Ownership but is classified in none of "
        "SUBJECT_BOUND_RECORDS, SUBJECTLESS_RECORDS, KIND_BOUND_RECORDS"
    )


def _is_blank(value: object) -> bool:
    """The runtime guard's ``_blank``, restated on the JSON side."""
    return not isinstance(value, str) or not value.strip()


def _id_problem(value: object, field: str, blank_message: str) -> str | None:
    """Blank first, then CANONICAL. The runtime guard's ``_id_violations``.

    Non-blank was never enough: ``" user-1 "`` and ``"user-1"`` are two
    encodings of one subject, so a per-person delete keyed on the canonical
    spelling misses the padded row. Invisible characters are the same hazard
    one step further in, since the two ids render identically.
    """
    if _is_blank(value):
        return blank_message
    assert isinstance(value, str)
    reason = noncanonical_id_reason(value)
    return None if reason is None else f"{field} {reason}"


def _invariant_ownership(p: dict, cls: type) -> None:
    """Every private record's four ownership fields, checked as VALUES.

    Structural validation already proves the four KEYS are present (they have
    no defaults on ``Ownership``). This is the value half:

    - ``tenant_id`` and ``actor_id`` non-blank after trimming. ``not null`` and
      ``non-blank`` are different rules, and an empty ``actor_id`` was the old
      ``ReceiptEnvelope`` default, so a receipt that named nobody used to be
      the shape's own default value.
    - ``actor_kind`` a real ``ActorKind``.
    - ``user_id`` never blank when present. Three encodings of "no human"
      (null, "", "   ") where the contract says there is one would let a
      deletion sweep filtering ``user_id is null`` miss the blank rows.
    - the record's TIER is resolved first, for every owned payload, so an
      envelope whose ``kind`` is outside the frozen vocabulary is rejected
      whatever its ``user_id`` says.
    - ``user_id`` REQUIRED non-blank on a SUBJECT-BOUND record, whatever wrote
      it, because a per-person delete must be able to find that row. On a
      subjectless record it may be null, and then only for a ``system`` actor.
    - a nested key that carries its own ``tenant_id`` must name the SAME
      tenant, so an approval cannot key one tenant and be owned by another.

    ``curator.ledger.ownership.ownership_violations`` implements the same rule
    over dataclass instances for the runtime write paths;
    ``test_the_two_ownership_validators_agree_on_every_fixture`` runs both over
    the whole corpus so the two cannot drift.
    """
    # ``isinstance(x, str)``, not ``str(x)``: coercing meant 12345, True and
    # {"a": 1} all became non-blank strings and passed here while
    # ``ownership_violations`` rejected them, so the two validators disagreed
    # on 240 of the fuzzed value combinations, every one of them fixture-lax /
    # runtime-strict. Same predicate on both sides now.
    for field, blank_message in (
        ("tenant_id", "every private record requires a non-empty tenant_id"),
        ("actor_id", "every private record requires a non-empty actor_id"),
    ):
        problem = _id_problem(p.get(field), field, blank_message)
        if problem is not None:
            raise ContractViolation(f"invariant: {problem}")
    kind = p.get("actor_kind")
    if kind not in {member.value for member in ActorKind}:
        raise ContractViolation(
            f"invariant: actor_kind {kind!r} is not a member of ActorKind"
        )
    # Resolved for EVERY owned payload, before anything branches on user_id.
    # Consulting the tier only on the null-user_id branch let an envelope with
    # an unclassified kind (a typo such as "rankng") pass whenever some
    # non-blank user_id happened to be present. An unknown kind fails CLOSED.
    subject_bound = _payload_is_subject_bound(p, cls)
    user_id = p.get("user_id")
    if user_id is not None:
        problem = _id_problem(
            user_id, "user_id", "user_id must be a non-blank id or null, never blank"
        )
        if problem is not None:
            raise ContractViolation(f"invariant: {problem}")
    if user_id is None:
        if subject_bound:
            raise ContractViolation(
                "invariant: this record is about a person, so user_id is required "
                "non-blank whatever wrote it"
            )
        if kind != ActorKind.SYSTEM.value:
            raise ContractViolation(
                "invariant: a human or agent actor requires a non-empty user_id; "
                "only a system actor may act for no human"
            )
    _invariant_nested_tenant(p, cls)


def _invariant_nested_tenant(p: dict, cls: type) -> None:
    """A nested key carrying its own tenant must agree with the owner's."""
    for field in dataclasses.fields(cls):
        value = p.get(field.name)
        if not isinstance(value, dict):
            continue
        nested = value.get("tenant_id")
        if nested is None:
            continue
        if nested != p.get("tenant_id"):
            raise ContractViolation(
                f"invariant: {field.name}.tenant_id must equal the record's tenant_id"
            )


def _invariant_identity(p: dict, cls: type) -> None:
    """Every id an IDENTITY record carries must be canonical, not merely non-blank.

    ``Tenant``, ``User``, ``Actor`` and ``TenantMembership`` are exempt from
    ``Ownership`` (they are the identity graph an owned record's
    ``tenant_id``/``actor_id``/``user_id`` names, not owned records
    themselves), so ``_invariant_ownership`` never runs on them and, before
    this function existed, none of the canonical checks did either: a padded,
    zero-width, or NFD-encoded ``Actor.actor_id`` or ``User.user_id`` was
    accepted structurally and by every ownership guard. The delete argument is
    the module's own: two spellings that render identically are two encodings
    of one subject, and if the identity row THAT NAMES the subject can itself
    be non-canonical, the key an owned-record sweep uses to find it is already
    ambiguous.

    Mirrors ``curator.ownership.identity_violations`` field-for-field;
    ``test_the_identity_and_ownership_validators_agree_on_every_fixture`` runs
    both over the corpus.
    """
    for field in _IDENTITY_ID_FIELDS.get(cls, ()):
        value = p.get(field)
        if field == "user_id" and value is None:
            continue
        problem = _id_problem(value, field, f"{cls.__name__}.{field} is required non-blank")
        if problem is not None:
            raise ContractViolation(f"invariant: {problem}")


def _invariant_actor(p: dict) -> None:
    """``Actor`` is exempt from ``Ownership`` but obeys the same null rule AND
    the same canonical-id rule as an owned record (see ``_invariant_identity``).

    It is the identity record an ``actor_id`` points AT, so it cannot be owned
    by one. But it is the binding that makes an agent-written record
    attributable to a human, so an agent ``Actor`` with no ``user_id`` would
    silently unbind every record that actor writes.
    """
    _invariant_identity(p, contracts.Actor)
    kind = p.get("actor_kind")
    user_id = p.get("user_id")
    if user_id is not None and (not isinstance(user_id, str) or not user_id.strip()):
        raise ContractViolation(
            "invariant: Actor.user_id must be a non-blank id or null, never blank"
        )
    if kind != ActorKind.SYSTEM.value and user_id is None:
        raise ContractViolation(
            "invariant: a human or agent Actor requires a non-empty user_id; "
            "only a system Actor may act for no human"
        )


def _invariant_tenant(p: dict) -> None:
    """``Tenant`` obeys the same canonical-id rule as an owned record."""
    _invariant_identity(p, contracts.Tenant)


def _invariant_user(p: dict) -> None:
    """``User`` obeys the same canonical-id rule as an owned record."""
    _invariant_identity(p, contracts.User)


def _invariant_tenant_membership(p: dict) -> None:
    """``TenantMembership`` obeys the same canonical-id rule as an owned record."""
    _invariant_identity(p, contracts.TenantMembership)


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


def _check_bands_match_policy(bands: list, policy_revision: int) -> None:
    """A settled receipt's bands must equal the policy revision it names.

    Reproduced: a settled ranking receipt whose bands carried floor 0.0 for
    relevance and freshness passed even though the frozen policy requires
    0.55 and 0.50 -- a receipt can silently self-certify looser bounds than
    the policy it claims to have run under.
    """
    policy = _policy()
    frozen_revision = policy["policy"]["revision"]
    if policy_revision != frozen_revision:
        raise ContractViolation(
            f"invariant: policy_revision must equal the frozen policy revision ({frozen_revision})"
        )
    policy_bands = policy["bands"]
    for band in bands:
        configured = policy_bands.get(band["band"])
        if configured is None:
            continue
        for key in ("active", "floor", "cap"):
            expected = configured.get(key)
            actual = band.get(key)
            if actual != expected:
                raise ContractViolation(
                    f"invariant: band {band['band']} {key} must equal the policy "
                    f"revision {frozen_revision} value ({expected!r}), got {actual!r}"
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
    # A resolution_ref with no predecessor linkage is a claim of "this
    # followed a readback" that names no readback: the retry must declare
    # which prior attempt it resolves.
    if p.get("resolution_ref") and not p.get("prior_receipt_ids"):
        raise ContractViolation(
            "invariant: resolution_ref requires prior_receipt_ids naming the "
            "attempt it resolves"
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
    state = p["state"]
    if state == "settled" and not p.get("authorization_id"):
        raise ContractViolation("invariant: state settled requires a non-null authorization_id")
    # Publication lineage is mandatory. Every state past draft must carry a
    # prior_state and the typed at-most-once key, checked unconditionally
    # rather than only when the fields happen to be present (reproduced:
    # removing prior_state and readback_verdict from an unknown -> settled
    # payload made it valid, and a settled record passed with an empty
    # idempotency_key).
    if state != "draft":
        if not p.get("prior_state"):
            raise ContractViolation(
                "invariant: every state past draft requires a non-empty prior_state"
            )
        if not p.get("idempotency_key"):
            raise ContractViolation(
                "invariant: every state past draft requires a non-empty idempotency_key"
            )
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
    # Transition legality, checked on every record that names a prior_state,
    # not only when it happens to be present: an illegal edge (in particular
    # unknown -> publishing, which would be a silent retry of an ambiguous
    # send) is rejected.
    prior = p.get("prior_state")
    if prior:
        if (prior, state) not in PUBLICATION_TRANSITIONS:
            raise ContractViolation(
                f"invariant: illegal publication transition from {prior} to {state}"
            )
        if prior == "unknown":
            # Any exit from unknown requires a readback receipt reference, not
            # just a claimed verdict with nothing behind it.
            if not p.get("readback_receipt_ref"):
                raise ContractViolation(
                    "invariant: leaving unknown requires a non-empty readback_receipt_ref"
                )
            verdict = p.get("readback_verdict")
            if not verdict:
                raise ContractViolation("invariant: leaving unknown requires a readback_verdict")
            if state == "settled" and verdict != ReadbackVerdict.POSITIVE.value:
                raise ContractViolation(
                    "invariant: leaving unknown for settled requires a positive readback_verdict"
                )
            if state == "failed-safe" and verdict != ReadbackVerdict.NEGATIVE_CONCLUSIVE.value:
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
        _check_bands_match_policy(p["bands"], p["envelope"]["policy_revision"])


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
        if _parse_iso8601(settled_at) < _parse_iso8601(p["correction_watermark"]):
            raise ContractViolation(
                "invariant: settled_at must not precede correction_watermark"
            )
    for row in unresolved:
        if not row.get("user_visible_disclosure"):
            raise ContractViolation(
                "invariant: an unresolved projection must carry a user-visible disclosure"
            )


def _invariant_principal_claims(p: dict) -> None:
    if _parse_iso8601(p["expires_at"]) <= _parse_iso8601(p["issued_at"]):
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


def _invariant_limit_receipt(p: dict) -> None:
    """A limit receipt settles green only when every meter was actually read.

    Receipt invariant 2: "a receipt whose meters cannot be read settles
    ``unknown``, not ``settled``". ``LimitReceipt`` had no registered
    invariant, so the corpus accepted a receipt stamped ``settled`` whose
    per-invocation ceiling was stale with ``value: null``. A shed policy would
    then have been evaluated against a budget nobody could read, and shedding
    cannot rescue a per-invocation ceiling at all.

    "Settled" is claimed in two places (the envelope's state and the receipt's
    ``final_state``); either claim is enough to require readable meters, and
    the two must agree. At least ONE reading is required as well: an empty
    ``readings`` list would settle green by vacuous truth, which is the same
    false-completeness hole the deletion receipt closes for its projections.
    """
    envelope_state = p["envelope"]["state"]
    final_state = p.get("final_state", "")
    if final_state and final_state != envelope_state:
        raise ContractViolation(
            f"invariant: a limit receipt's final_state {final_state!r} must "
            f"equal its envelope state {envelope_state!r}"
        )
    if "settled" not in (envelope_state, final_state):
        return
    # A settled receipt that reads NO meter is the strongest instance of
    # "meters cannot be read", not an exception to it: the empty list makes the
    # loop below vacuous and settles green while proving nothing was measured.
    # The sibling deletion receipt already closes the identical hole (an empty
    # or partial ``projections`` list settling green); this is the meter half.
    if not p["readings"]:
        raise ContractViolation(
            "invariant: a settled limit receipt must read at least one meter; "
            "an empty readings list settles green while proving nothing was read"
        )
    for reading in p["readings"]:
        if reading["value"] is None or reading["freshness_verdict"] != "fresh":
            raise ContractViolation(
                "invariant: a settled limit receipt requires every reading fresh "
                f"with a non-null value, got {reading['meter']!r} at "
                f"{reading['freshness_verdict']!r} with value {reading['value']!r}"
            )


INVARIANTS = {
    "Actor": _invariant_actor,
    "Tenant": _invariant_tenant,
    "User": _invariant_user,
    "TenantMembership": _invariant_tenant_membership,
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
    "LimitReceipt": _invariant_limit_receipt,
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


#: The ONLY contract dataclasses that may carry a ``tenant_id`` or an
#: ``actor_id`` without inheriting ``Ownership``, each with the reason it is
#: not a private RECORD. Encoded as data so adding an exemption is a visible,
#: reasoned edit rather than a quiet omission.
OWNERSHIP_EXEMPT = {
    "Tenant": "the isolation boundary itself; it cannot be owned by one",
    "User": "the identity record a user_id points AT",
    "Actor": "the identity record an actor_id points AT",
    "TenantMembership": "binds a principal to a tenant; it is the membership, not a record inside one",
    "PublicationIdentity": "an at-most-once KEY (tenant, publisher, destination, issue date), not a stored record",
    "SearchQuery": "a request in flight, never persisted as a row",
    "SearchResult": "a response row computed per request, never persisted",
    "PrincipalClaims": "the caller's ASSERTED identity, verified by the server; it is the input to authorization, not an owned row",
    "SearchResponse": "a response envelope computed per request; its tenant is visible only through the SearchResult rows it wraps, which are themselves exempt",
}

#: Name SUFFIXES that mean tenant, actor, user, owner, subject, or principal
#: semantics. Membership in the gate is decided by PATTERN, not by a handful of
#: exact names: a future record spelling its fields ``owner_tenant_id`` /
#: ``written_by_actor_id`` / ``created_by_user`` carries the same semantics and
#: used to be invisible to this gate. Matched case-insensitively.
OWNERSHIP_FIELD_SUFFIXES = (
    "tenant_id",
    "actor_id",
    "user_id",
    "_actor",
    "_tenant",
    "_user",
    "_principal",
    "_owner",
    "_subject",
)

#: Whole field NAMES with the same semantics that no suffix above catches.
#: ``actor_identity`` is the one the re-review actually demonstrated: it named
#: the actor binding, matched nothing, and would have let a future private
#: record skip both the ownership shape and the exemption review in silence.
OWNERSHIP_FIELD_NAMES = (
    "actor_identity",
    "actor_ref",
    "tenant_ref",
    "user_ref",
    "principal_id",
    "owner_id",
    "subject_id",
)


def _is_ownership_semantic_name(name: str) -> bool:
    """One matcher, used by the gate and by its own red-gate test."""
    lowered = name.lower()
    return lowered.endswith(OWNERSHIP_FIELD_SUFFIXES) or lowered in OWNERSHIP_FIELD_NAMES


def _ownership_semantic_fields(cls: type, depth: int = 0) -> set[str]:
    """Fields with tenant/actor/user semantics, including NESTED ones.

    Nesting matters because ``PublicationRecord`` and
    ``PublicationAuthorization`` carried their tenant inside
    ``identity: PublicationIdentity`` and were therefore skipped in silence by
    a flat-field-name check.

    A nested value that ITSELF inherits ``Ownership`` is not descended into:
    that inner record carries the shape on its own behalf (a receipt wrapper
    owns through its ``envelope``), so the wrapper is not the offender.
    """
    hits = {
        f.name for f in dataclasses.fields(cls)
        if _is_ownership_semantic_name(f.name)
    }
    if depth >= 3:
        return hits
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        annotation = hints.get(field.name)
        candidates = (annotation,) + tuple(typing.get_args(annotation) or ())
        for candidate in candidates:
            for inner in typing.get_args(candidate) or (candidate,):
                if (
                    isinstance(inner, type)
                    and dataclasses.is_dataclass(inner)
                    and not issubclass(inner, Ownership)
                ):
                    hits |= {
                        f"{field.name}.{nested}"
                        for nested in _ownership_semantic_fields(inner, depth + 1)
                    }
    return hits


def _contract_dataclasses() -> dict[str, type]:
    found: dict[str, type] = {}
    for module_path in sorted((REPO_ROOT / "curator" / "contracts").glob("*.py")):
        if module_path.stem == "__init__":
            continue
        module = importlib.import_module(f"curator.contracts.{module_path.stem}")
        for attribute_name, obj in vars(module).items():
            if attribute_name.startswith("_") or getattr(obj, "__module__", None) != module.__name__:
                continue
            if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                found[obj.__name__] = obj
    return found


def test_every_private_record_inherits_the_shared_ownership_shape():
    """The gate. A private record cannot ship with a partial ownership shape.

    Closes the fourth of the four optional-guard bypasses the contract-freeze
    re-review found: three guard fields were made required, but there was no
    SHARED ownership shape, so ``ReceiptEnvelope.actor_id`` defaulted to "",
    ``EvidenceItem`` carried no actor at all, ``RawImport`` spelled it
    ``owner_actor_id``, ``ArtifactVersion`` had no ``tenant_id``, and nothing
    carried ``user_id`` even though the plan's privacy boundary requires all
    three.

    Membership is DERIVED by NAME PATTERN over the field list, nested fields
    included, so a new contract dataclass with tenant, actor, or user
    semantics fails here until it either inherits ``Ownership`` or is exempted
    above with a reason. Detecting two exact names was the loophole: it missed
    ``PublicationAuthorization`` (tenant nested in ``identity``, actor spelled
    ``approved_by_actor_id``) and ``PublicationRecord`` entirely.
    """
    offenders = []
    for name, cls in sorted(_contract_dataclasses().items()):
        if cls is Ownership:
            continue
        if not _ownership_semantic_fields(cls):
            continue
        if issubclass(cls, Ownership):
            continue
        if name in OWNERSHIP_EXEMPT:
            assert OWNERSHIP_EXEMPT[name].strip(), f"{name}: an exemption without a reason"
            continue
        offenders.append(name)
    assert not offenders, (
        "these carry tenant, actor, or user semantics (flat or nested) but "
        "neither inherit Ownership "
        f"nor carry a recorded exemption: {offenders}"
    )


def test_owner_actor_id_spelling_is_gone():
    """One field, one spelling, with NO allowlist.

    ``RawImport.owner_actor_id`` and ``PublicationAuthorization.approved_by_actor_id``
    are the two removed ones. The second used to sit in this test's own
    allowlist, which made "one field, one spelling" true only for the spellings
    nobody had shipped yet.
    """
    duplicates = []
    for name, cls in sorted(_contract_dataclasses().items()):
        for field in dataclasses.fields(cls):
            if field.name.endswith("actor_id") and field.name != "actor_id":
                duplicates.append(f"{name}.{field.name}")
    assert not duplicates, f"one-off spellings of the shared actor_id: {duplicates}"


def test_ownership_fields_are_required_with_no_defaults():
    """A guard field with a default is a guard that can be skipped by omission."""
    fields = {f.name: f for f in dataclasses.fields(Ownership)}
    assert list(fields) == ["tenant_id", "actor_id", "actor_kind", "user_id"]
    for name, field in fields.items():
        assert field.default is dataclasses.MISSING, f"Ownership.{name} carries a default"
        assert field.default_factory is dataclasses.MISSING, (  # type: ignore[misc]
            f"Ownership.{name} carries a default_factory"
        )


def test_owned_records_tuple_matches_the_derived_set():
    """``OWNED_RECORDS`` is documentation only if it can drift from the truth."""
    derived = {
        cls for cls in _contract_dataclasses().values()
        if issubclass(cls, Ownership) and cls is not Ownership
    }
    assert set(OWNED_RECORDS) == derived
    assert len(OWNED_RECORDS) == len(set(OWNED_RECORDS))
    assert "Ownership" in contracts.__all__ and "OWNED_RECORDS" in contracts.__all__


def test_ownership_invariant_rejects_each_way_it_can_be_broken():
    """The invariant's own positive control, one seeded defect per branch."""
    good = {
        "tenant_id": "tenant-owner-private",
        "actor_id": "actor-human-owner",
        "actor_kind": "human",
        "user_id": "user-owner",
    }
    subject_bound = contracts.LearningEvent
    subjectless = contracts.SourceCheckpoint
    _invariant_ownership(good, subject_bound)
    _invariant_ownership(good, subjectless)
    # Null is legal ONLY on a subjectless record written by the system.
    _invariant_ownership({**good, "actor_kind": "system", "user_id": None}, subjectless)
    for seeded, cls in (
        ({**good, "tenant_id": ""}, subject_bound),
        ({**good, "tenant_id": "   "}, subject_bound),
        ({**good, "actor_id": ""}, subject_bound),
        ({**good, "actor_id": "   "}, subject_bound),
        ({**good, "actor_kind": "robot"}, subject_bound),
        ({**good, "actor_kind": "Human"}, subject_bound),
        ({**good, "user_id": None}, subject_bound),
        ({**good, "actor_kind": "system", "user_id": None}, subject_bound),
        ({**good, "user_id": ""}, subject_bound),
        ({**good, "user_id": "   "}, subject_bound),
        ({**good, "actor_kind": "system", "user_id": ""}, subject_bound),
        ({**good, "actor_kind": "system", "user_id": "   "}, subjectless),
        ({**good, "user_id": None}, subjectless),
        ({**good, "actor_kind": "agent", "user_id": ""}, subjectless),
    ):
        with pytest.raises(ContractViolation):
            _invariant_ownership(seeded, cls)


def _envelope_kinds_in_valid_fixtures() -> set[str]:
    """Every ``envelope.kind`` a VALID fixture carries.

    Invalid fixtures are excluded on purpose: one of them seeds an
    unclassified kind as the positive control for the unknown-kind gate.
    """
    kinds: set[str] = set()
    for path in FIXTURE_PATHS:
        document = _load(path)
        if document.get("expect") != "valid":
            continue
        payload = document["payload"]
        if not isinstance(payload, dict):
            continue
        envelope = payload.get("envelope")
        if isinstance(envelope, dict) and isinstance(envelope.get("kind"), str):
            kinds.add(envelope["kind"])
    return kinds


def test_every_owned_record_is_classified_exactly_once():
    """No owned record may land unclassified, and none may sit in two tiers.

    The two-tier rule only holds if EVERY owned class has an answer to "must a
    per-person delete find this row". A class in none of the three tuples used
    to fall into the permissive tier by default, which is the silent-omission
    failure this whole change exists to close.
    """
    tiers = {
        "SUBJECT_BOUND_RECORDS": list(SUBJECT_BOUND_RECORDS),
        "SUBJECTLESS_RECORDS": list(SUBJECTLESS_RECORDS),
        "KIND_BOUND_RECORDS": list(KIND_BOUND_RECORDS),
    }
    owned = set(OWNED_RECORDS)
    placements: dict[type, list[str]] = {}
    for tier, classes in tiers.items():
        assert len(classes) == len(set(classes)), f"{tier} lists a class twice"
        for cls in classes:
            placements.setdefault(cls, []).append(tier)

    unclassified = sorted(cls.__name__ for cls in owned - set(placements))
    assert not unclassified, (
        "these owned records are in none of the three classification tuples, so "
        f"nothing decides whether a per-person delete must find them: {unclassified}"
    )
    twice = sorted(
        cls.__name__ for cls, tiers_hit in placements.items() if len(tiers_hit) > 1
    )
    assert not twice, f"classified in more than one tier: {twice}"
    stray = sorted(cls.__name__ for cls in set(placements) - owned)
    assert not stray, f"classified but not an owned record: {stray}"

    reasons = dict(OWNERSHIP_CLASSIFICATION_REASONS)
    owned_names = {cls.__name__ for cls in owned}
    assert set(reasons) == owned_names, (
        "every owned record needs a written reason for its tier; missing "
        f"{sorted(owned_names - set(reasons))}, "
        f"stray {sorted(set(reasons) - owned_names)}"
    )
    for name, reason in reasons.items():
        assert reason.strip(), f"{name}: a classification without a reason"

    # Receipt kinds are the field-level half of the same rule.
    kinds = list(SUBJECT_BOUND_RECEIPT_KINDS) + list(SUBJECTLESS_RECEIPT_KINDS)
    assert len(kinds) == len(set(kinds)), "a receipt kind is classified twice"
    # VALID fixtures only: an invalid fixture seeded with kind "rankng" is the
    # positive control for the unknown-kind gate, so an unclassified kind is
    # exactly what it is supposed to carry.
    seen_kinds = _envelope_kinds_in_valid_fixtures()
    assert seen_kinds <= set(kinds), (
        f"receipt kinds in the corpus with no classification: {sorted(seen_kinds - set(kinds))}"
    )


def _stub_owned_record(cls: type, payload: dict):
    """Rebuild ``cls`` from a fixture payload well enough to ownership-check it.

    Deliberately not a full constructor: the corpus holds INVALID payloads that
    a real constructor would refuse for unrelated reasons, and the only thing
    being compared is the ownership verdict.
    """
    record = object.__new__(cls)
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        if field.name not in payload:
            continue
        value = payload[field.name]
        if field.name == "actor_kind":
            try:
                value = ActorKind(value)
            except ValueError:
                pass
        elif isinstance(value, dict):
            for nested in _annotated_dataclasses(hints.get(field.name)):
                value = _stub_owned_record(nested, value)
                break
        object.__setattr__(record, field.name, value)
    return record


def _annotated_dataclasses(annotation: object) -> tuple[type, ...]:
    """Every dataclass reachable from one annotation, containers unwrapped.

    ``EvidenceItem | None`` and ``tuple[EvidenceItem, ...]`` both hold an
    owned record; a check that only accepted a bare dataclass annotation saw
    neither. The ownership PATTERN gate already unwrapped both, so the walk
    that compares the two validators had a smaller reach than the gate that
    decides which classes are in scope at all.
    """
    found: list[type] = []
    for candidate in (annotation,) + tuple(typing.get_args(annotation) or ()):
        for inner in typing.get_args(candidate) or (candidate,):
            if isinstance(inner, type) and dataclasses.is_dataclass(inner):
                found.append(inner)
    return tuple(dict.fromkeys(found))


def _owned_payloads(cls: type, payload: dict):
    """Every owned record inside one fixture, the nested envelopes included.

    Descends into optional and tuple-contained dataclasses exactly as
    ``_ownership_semantic_fields`` does, so a record the gate can see is never
    a record this walk silently skips.
    """
    if not isinstance(payload, dict):
        return
    if issubclass(cls, Ownership):
        yield cls, payload
    hints = typing.get_type_hints(cls)
    for field in dataclasses.fields(cls):
        value = payload.get(field.name)
        for nested in _annotated_dataclasses(hints.get(field.name)):
            if isinstance(value, dict):
                yield from _owned_payloads(nested, value)
            elif isinstance(value, list):
                for element in value:
                    if isinstance(element, dict):
                        yield from _owned_payloads(nested, element)


def test_the_two_ownership_validators_agree_on_every_fixture():
    """One rule, two implementations, checked against each other on real data.

    ``_invariant_ownership`` guards the frozen fixture corpus;
    ``curator.ledger.ownership.ownership_violations`` guards the runtime write
    paths. Two implementations of one rule drift silently unless something runs
    both over the same corpus and compares verdicts, which is what this does.
    """
    compared = 0
    disagreements = []
    for path in FIXTURE_PATHS:
        document = _load(path)
        cls = getattr(contracts, document["dataclass"], None)
        if cls is None:
            continue
        for owned_cls, payload in _owned_payloads(cls, document["payload"]):
            compared += 1
            try:
                _invariant_ownership(payload, owned_cls)
                fixture_verdict = True
            except ContractViolation:
                fixture_verdict = False
            runtime_verdict = not ownership_violations(
                _stub_owned_record(owned_cls, payload)
            )
            if fixture_verdict != runtime_verdict:
                disagreements.append(
                    f"{path.name}:{owned_cls.__name__} fixture-ok={fixture_verdict} "
                    f"runtime-ok={runtime_verdict}"
                )
    assert compared > 50, f"only {compared} owned records reached; the walk is not finding them"
    assert not disagreements, (
        "the fixture validator and the runtime guard disagree, so one of them is "
        f"already drifting: {disagreements}"
    )


def test_the_identity_and_ownership_validators_agree_on_every_fixture():
    """``_invariant_identity`` and ``curator.ownership.identity_violations``,
    checked against each other on every ``Tenant``/``User``/``Actor``/
    ``TenantMembership`` fixture, exactly as
    ``test_the_two_ownership_validators_agree_on_every_fixture`` does for
    owned records.
    """
    compared = 0
    disagreements = []
    for path in FIXTURE_PATHS:
        document = _load(path)
        name = document["dataclass"]
        cls = getattr(contracts, name)
        if cls not in _IDENTITY_ID_FIELDS:
            continue
        payload = document["payload"]
        compared += 1
        try:
            _invariant_identity(payload, cls)
            fixture_verdict = True
        except ContractViolation:
            fixture_verdict = False
        runtime_verdict = not identity_violations(_stub_owned_record(cls, payload))
        if fixture_verdict != runtime_verdict:
            disagreements.append(
                f"{path.name}:{name} fixture-ok={fixture_verdict} runtime-ok={runtime_verdict}"
            )
    assert compared >= 6, f"only {compared} identity fixtures reached"
    assert not disagreements, disagreements


def test_actor_user_id_is_required_with_no_default():
    """The identity record obeys the same guard rule as the records it binds."""
    fields = {f.name: f for f in dataclasses.fields(contracts.Actor)}
    assert fields["user_id"].default is dataclasses.MISSING
    assert fields["user_id"].default_factory is dataclasses.MISSING  # type: ignore[misc]
    assert "Actor" in INVARIANTS


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


# ---------------------------------------------------------------------------
# Receipt-kind vocabulary and the wrapper binding (round-2 re-review)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _RenamedIdentityRecord:
    """Red-gate specimen: an actor binding under a name no suffix catches."""

    row_id: str
    actor_identity: str


@dataclasses.dataclass(frozen=True)
class _NeutralRecord:
    """Red-gate control: nothing here has ownership semantics."""

    row_id: str
    flavor_text: str


@dataclasses.dataclass(frozen=True)
class _TupleOfOwnedRecords:
    """Walk specimen: an owned record reachable only through a tuple."""

    envelopes: tuple[contracts.ReceiptEnvelope, ...]


@dataclasses.dataclass(frozen=True)
class _OptionalOwnedRecord:
    """Walk specimen: an owned record reachable only through an optional."""

    envelope: contracts.ReceiptEnvelope | None


def _owned_envelope_payload(**overrides) -> dict:
    payload = {
        "tenant_id": "tenant-owner-private",
        "actor_id": "actor-human-owner",
        "actor_kind": "human",
        "user_id": "user-owner",
        "receipt_id": "drec-900001",
        "kind": "deletion",
        "state": "settled",
        "created_at": "2026-09-01T12:00:00+00:00",
        "policy_revision": 1,
        "reason_code": "",
        "settled_at": "2026-09-01T12:05:00+00:00",
    }
    payload.update(overrides)
    return payload


def _envelope(**overrides) -> contracts.ReceiptEnvelope:
    payload = _owned_envelope_payload(**overrides)
    return contracts.ReceiptEnvelope(
        tenant_id=payload["tenant_id"],
        actor_id=payload["actor_id"],
        actor_kind=ActorKind(payload["actor_kind"]),
        user_id=payload["user_id"],
        receipt_id=payload["receipt_id"],
        kind=payload["kind"],
        state=payload["state"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        policy_revision=payload["policy_revision"],
    )


def test_the_receipt_kind_vocabulary_is_frozen_closed_and_derived():
    """One closed list of kinds, with the two tiers DERIVED from it.

    Two hand-maintained tuples could disagree, and a kind in neither was the
    permissive default until the tier began to be resolved for every record.
    """
    kinds = [kind for kind, _ in RECEIPT_KIND_TIERS]
    tiers = {tier for _, tier in RECEIPT_KIND_TIERS}
    assert len(kinds) == len(set(kinds)), "a receipt kind is listed twice"
    assert tiers == {"subject_bound", "subjectless"}
    assert set(SUBJECT_BOUND_RECEIPT_KINDS) | set(SUBJECTLESS_RECEIPT_KINDS) == set(kinds)
    assert set(SUBJECT_BOUND_RECEIPT_KINDS) & set(SUBJECTLESS_RECEIPT_KINDS) == set()
    # Every frozen kind is exercised by at least one fixture, so the vocabulary
    # describes the corpus rather than only itself.
    seen = _envelope_kinds_in_valid_fixtures()
    assert set(kinds) <= seen, f"frozen kinds with no fixture: {sorted(set(kinds) - seen)}"


def test_an_unknown_receipt_kind_fails_closed_whatever_user_id_says():
    """Must-fix 1. A typo'd kind is a violation even with a real user_id.

    ``kind="rankng"`` used to be accepted by both validators because the tier
    was only consulted on the null-user_id branch.
    """
    payload = _owned_envelope_payload(kind="rankng")
    with pytest.raises(ContractViolation):
        _invariant_ownership(payload, contracts.ReceiptEnvelope)
    problems = ownership_violations(_envelope(kind="rankng"))
    assert any("rankng" in problem for problem in problems), problems
    # Positive control: the same payload with a classified kind is clean.
    _invariant_ownership(_owned_envelope_payload(kind="ranking"), contracts.ReceiptEnvelope)
    assert ownership_violations(_envelope(kind="ranking")) == ()


def test_each_receipt_wrapper_pins_one_canonical_kind():
    """Must-fix 2. The wrapper's type and its envelope cannot disagree."""
    bindings = {
        cls: (field_name, kind)
        for cls, field_name, kind in RECEIPT_WRAPPER_KINDS
    }
    vocabulary = {kind for kind, _ in RECEIPT_KIND_TIERS}
    kinds = {kind for _, kind in bindings.values()}
    assert kinds <= vocabulary
    assert len(kinds) == len(bindings), "two wrappers share a kind"
    for cls, (field_name, kind) in bindings.items():
        assert getattr(contracts, cls.__name__, None) is cls
        assert field_name in {f.name for f in dataclasses.fields(cls)}
        good = {field_name: _owned_envelope_payload(kind=kind)}
        _invariant_receipt_wrapper_kind(good, cls)
        wrong = "ranking" if kind != "ranking" else "deletion"
        with pytest.raises(ContractViolation):
            _invariant_receipt_wrapper_kind(
                {field_name: _owned_envelope_payload(kind=wrong)}, cls
            )


def test_the_runtime_guard_rejects_a_wrapper_whose_envelope_kind_is_wrong():
    """The same binding, on the dataclass side rather than the JSON side."""
    receipt = contracts.DeletionReceipt(
        envelope=_envelope(kind="ranking"),
        target_kind="evidence_item",
        target_ids=("ev-000101",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-900001",
        zero_contribution_verdict=True,
        projections=(),
    )
    assert receipt_wrapper_violations(receipt), "a ranking envelope is not a deletion receipt"
    assert ownership_violations(receipt), "ownership_violations must surface the binding too"
    clean = dataclasses.replace(receipt, envelope=_envelope(kind="deletion"))
    assert receipt_wrapper_violations(clean) == ()
    assert ownership_violations(clean) == ()


def _valid_deletion_receipt() -> contracts.DeletionReceipt:
    return contracts.DeletionReceipt(
        envelope=_envelope(kind="deletion"),
        target_kind="user",
        target_ids=("user-owner",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-1",
        zero_contribution_verdict=True,
        projections=(),
    )


def _deletion_receipt_values() -> dict[str, object]:
    receipt = _valid_deletion_receipt()
    return {field.name: getattr(receipt, field.name) for field in dataclasses.fields(receipt)}


_LearningEventImpostor = dataclasses.make_dataclass(
    "LearningEvent",
    (),
    bases=(Ownership,),
    frozen=True,
    namespace={"__module__": "types"},
)


def test_same_name_owned_impostor_is_refused_by_guard_and_ledger():
    """A class name cannot impersonate a frozen owned record."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    impostor = _LearningEventImpostor(
        tenant_id="tenant-owner-private",
        actor_id="actor-owner",
        actor_kind=ActorKind.AGENT,
        user_id="user-owner",
    )
    assert ownership_violations(impostor), "class-name matching accepted an unknown type"
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().append_event(impostor)


_DeletionReceiptImpostor = dataclasses.make_dataclass(
    "DeletionReceipt",
    (
        ("envelope", contracts.ReceiptEnvelope),
        ("projections", tuple[contracts.ProjectionResolution, ...]),
    ),
    frozen=True,
    namespace={"__module__": "types"},
)


def test_same_name_deletion_receipt_impostor_is_refused_by_store():
    """A class name cannot impersonate a frozen wrapper pin."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    impostor = _DeletionReceiptImpostor(
        envelope=_envelope(kind="deletion"),
        projections=(),
    )
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().record_deletion_receipt(impostor)


@dataclasses.dataclass(frozen=True)
class _OverriddenEnvelopeReceipt(contracts.DeletionReceipt):
    envelope: object


def test_wrapper_subclass_with_overridden_envelope_is_refused_by_store():
    """Frozen wrappers are exact types and are never subclassed."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    wrapper = _OverriddenEnvelopeReceipt(
        **{
            **_deletion_receipt_values(),
            "envelope": _envelope(kind="deletion"),
        }
    )
    assert not is_receipt_wrapper(wrapper)
    assert ownership_violations(wrapper), "a frozen-wrapper subclass is an unknown record"
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().record_deletion_receipt(wrapper)


_RogueEnvelopeAlias = typing.NewType("_RogueEnvelopeAlias", contracts.ReceiptEnvelope)


@dataclasses.dataclass(frozen=True)
class _NewTypeWrappedRogueReceipt(contracts.DeletionReceipt):
    rogue_envelope: _RogueEnvelopeAlias = None  # type: ignore[assignment,valid-type]


def test_newtype_wrapped_rogue_envelope_subclass_is_refused_by_store():
    """The exact-type rule closes aliases without runtime annotation walking."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    rogue = _envelope(kind="ranking", actor_kind=ActorKind.AGENT, user_id=None)
    assert ownership_violations(rogue), "the rogue envelope must itself be invalid"
    wrapper = _NewTypeWrappedRogueReceipt(
        **_deletion_receipt_values(),
        rogue_envelope=typing.cast(_RogueEnvelopeAlias, rogue),
    )
    assert not is_receipt_wrapper(wrapper)
    assert ownership_violations(wrapper), "a frozen-wrapper subclass is an unknown record"
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().record_deletion_receipt(wrapper)


def test_frozen_wrapper_requires_exact_receipt_envelope_type():
    """A real wrapper passes; a mapping that looks like its envelope does not."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    valid = _valid_deletion_receipt()
    assert receipt_wrapper_violations(valid) == ()
    assert InMemoryLedgerStore().record_deletion_receipt(valid) is valid

    invalid = dataclasses.replace(valid, envelope=dataclasses.asdict(valid.envelope))
    assert receipt_wrapper_violations(invalid)
    with pytest.raises(LedgerError, match="must be exactly ReceiptEnvelope"):
        InMemoryLedgerStore().record_deletion_receipt(invalid)


def test_the_generic_guard_rejects_a_wrapper_whose_envelope_names_no_human():
    """A wrapper owns THROUGH its envelope, so the guard must recurse.

    ``DeletionReceipt`` does not inherit ``Ownership``, so the generic guard
    used to return clean for a correctly named deletion receipt whose envelope
    carried a ``system`` actor and a null ``user_id``. The deletion store
    happened to check the envelope itself; nothing made the next wrapper's
    author do the same.
    """
    receipt = contracts.DeletionReceipt(
        envelope=_envelope(kind="deletion", actor_kind=ActorKind.SYSTEM, user_id=None),
        target_kind="evidence_item",
        target_ids=("ev-000101",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-900001",
        zero_contribution_verdict=True,
        projections=(),
    )
    assert receipt_wrapper_violations(receipt) == (), "the KIND is right; only the subject is missing"
    problems = ownership_violations(receipt)
    assert any("user_id is required" in problem for problem in problems), problems
    assert all(problem.startswith("envelope: ") for problem in problems), problems


def test_every_receipt_wrapper_is_bound_to_a_kind():
    """The wrapper list is a closed set of exact class objects."""
    wrappers = {cls for cls, _, _ in RECEIPT_WRAPPER_KINDS}
    assert wrappers <= set(_contract_dataclasses().values())
    for cls in wrappers:
        values = _deletion_receipt_values() if cls is contracts.DeletionReceipt else None
        if values is not None:
            assert is_receipt_wrapper(cls(**values))
    assert not is_receipt_wrapper(_envelope(kind="ranking"))


def test_the_receipt_tier_semantics_are_frozen_exactly():
    """Not "the tiers are well formed": the exact assignments, pinned.

    ``test_the_receipt_kind_vocabulary_is_frozen_closed_and_derived`` proves
    the two tuples are derived and disjoint, which stays true after flipping
    ``deletion`` to subjectless. That flip makes a deletion receipt with a
    null ``user_id`` legal in every layer, so the assignments themselves are
    frozen here.
    """
    assert dict(RECEIPT_KIND_TIERS) == {
        "deletion": "subject_bound",
        "host_limits": "subjectless",
        "import_inventory": "subject_bound",
        "ranking": "subject_bound",
    }
    assert RECEIPT_WRAPPER_KINDS == (
        (contracts.DeletionReceipt, "envelope", "deletion"),
        (contracts.ImportInventoryReceipt, "envelope", "import_inventory"),
        (contracts.LimitReceipt, "envelope", "host_limits"),
        (contracts.RankingReceipt, "envelope", "ranking"),
    )
    wrapper_kinds = {kind for _, _, kind in RECEIPT_WRAPPER_KINDS}
    assert wrapper_kinds == {
        "deletion",
        "import_inventory",
        "host_limits",
        "ranking",
    }
    # Every wrapper's kind is a real kind AND every kind has a wrapper: a
    # subset check would let a kind exist that no receipt type can carry.
    assert wrapper_kinds == {kind for kind, _ in RECEIPT_KIND_TIERS}


def test_the_record_tiers_are_frozen_exactly():
    """The same pinning for the class-level tiers.

    ``Slate`` and the ``ranking`` kind moved here on 2026-09-02; the reason is
    written in ``OWNERSHIP_CLASSIFICATION_REASONS`` so a later round cannot
    flip them back without answering it.
    """
    assert set(SUBJECT_BOUND_RECORDS) == {
        contracts.ArtifactRelation,
        contracts.ArtifactVersion,
        contracts.AuthorizationAudit,
        contracts.CorrectionEvent,
        contracts.EvidenceItem,
        contracts.KnowledgeArtifact,
        contracts.LearningEvent,
        contracts.MirrorReceipt,
        contracts.OutputReceipt,
        contracts.ProfileSnapshot,
        contracts.PublicationAuthorization,
        contracts.PublicationRecord,
        contracts.PublishingBasket,
        contracts.RawImport,
        contracts.Slate,
    }
    assert set(SUBJECTLESS_RECORDS) == {
        contracts.LaneCandidate,
        contracts.MergedCandidate,
        contracts.NormalizedSourceDocument,
        contracts.SourceCheckpoint,
        contracts.SourcePluginRegistration,
        contracts.StoryRecord,
    }
    assert KIND_BOUND_RECORDS == (contracts.ReceiptEnvelope,)


def test_a_personalized_slate_and_its_ranking_receipt_require_a_subject():
    """The reclassification, asserted on both sides of the move."""
    slate = _load(FIXTURE_ROOT / "candidate" / "valid-slate-full-edition.json")
    assert slate["payload"]["user_id"], "a personalized slate names its reader"
    seeded = json.loads(json.dumps(slate["payload"]))
    seeded["user_id"] = None
    with pytest.raises(ContractViolation):
        _invariant_ownership(seeded, contracts.Slate)
    envelope = _owned_envelope_payload(kind="ranking", actor_kind="system", user_id=None)
    with pytest.raises(ContractViolation):
        _invariant_ownership(envelope, contracts.ReceiptEnvelope)
    problems = ownership_violations(
        _envelope(kind="ranking", actor_kind=ActorKind.SYSTEM, user_id=None)
    )
    assert any("user_id is required" in problem for problem in problems), problems


def test_a_settled_limit_receipt_cannot_carry_an_unreadable_meter():
    """Receipt invariant 2, which nothing enforced until 2026-09-02."""
    good = _load(FIXTURE_ROOT / "receipt" / "valid-limit-receipt-unknown-meter.json")
    validate_fixture(good)
    seeded = json.loads(json.dumps(good))
    seeded["payload"]["envelope"]["state"] = "settled"
    seeded["payload"]["final_state"] = "settled"
    with pytest.raises(ContractViolation):
        validate_fixture(seeded)
    # A settled receipt whose meters ARE readable is legal.
    settled = json.loads(json.dumps(seeded))
    for reading in settled["payload"]["readings"]:
        reading["value"] = 1.0
        reading["freshness_verdict"] = "fresh"
    settled["payload"]["envelope"]["settled_at"] = "2026-09-01T12:05:00+00:00"
    settled["payload"]["envelope"]["reason_code"] = ""
    validate_fixture(settled)


# ---------------------------------------------------------------------------
# The PROSE is the freeze, so the prose is tested (round-3 must-fix 1)
# ---------------------------------------------------------------------------


_DOC_BY_MODULE = {module: doc for _, module, doc in FROZEN_CONTRACTS}
_OWNERSHIP_DOC_ROW = re.compile(r"^\|\s*`tenant_id`")
_DOC_HEADING = re.compile(r"^(#+)\s+(.*)$")


def _doc_sections(lines: list[str]) -> list[tuple[set[str], int, int]]:
    """Every heading's backticked names, and the line range it owns.

    A section ends at the next heading of the SAME or a higher level, so
    ``### `EvidenceItem` `` owns its field table and stops before the next
    record rather than swallowing the file.
    """
    sections: list[tuple[set[str], int, int]] = []
    heads = [
        (index, match.group(1), match.group(2))
        for index, line in enumerate(lines)
        if (match := _DOC_HEADING.match(line))
    ]
    for position, (index, hashes, text) in enumerate(heads):
        end = len(lines)
        for later_index, later_hashes, _ in heads[position + 1 :]:
            if len(later_hashes) <= len(hashes):
                end = later_index
                break
        sections.append((set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text)), index, end))
    return sections


def _expected_ownership_doc_sentence(cls: type) -> str:
    """The one sentence this record's field table may carry, DERIVED.

    Derived from the frozen tuples, never hand-written per file, so flipping a
    class or a receipt kind to the other tier turns every doc row that states
    the old rule red in the same run.
    """
    if any(cls is frozen_class for frozen_class in SUBJECT_BOUND_RECORDS):
        return (
            "Required, all four. SUBJECT-BOUND: `user_id` required, non-blank, "
            "regardless of writer. See [tenant.md](tenant.md#ownership)."
        )
    if any(cls is frozen_class for frozen_class in SUBJECTLESS_RECORDS):
        return (
            "Required, all four. SUBJECTLESS: `user_id` null only under a "
            "`system` actor. See [tenant.md](tenant.md#ownership)."
        )
    assert any(cls is frozen_class for frozen_class in KIND_BOUND_RECORDS), cls
    return (
        "Required, all four. Tier and subject rules come from the generated "
        "receipt-kind table in Freeze notes. "
        "See [tenant.md](tenant.md#ownership)."
    )


@pytest.mark.parametrize(
    "cls", OWNED_RECORDS, ids=[cls.__name__ for cls in OWNED_RECORDS]
)
def test_every_owned_records_doc_table_states_its_own_tier(cls):
    """The freeze is the PROSE, and the prose said the opposite of the code.

    Twelve subject-bound records' field tables carried the SUBJECTLESS
    sentence ("user_id may be null only for a system actor"), each one linking
    to a canonical table that classified it the other way. A phase-2
    implementer reading the field table first would have written a row that
    the runtime guard, the corpus, and the `not null` column all reject. Hand
    edits rot, so the row is asserted against the frozen tuples here.
    """
    doc = REPO_ROOT / _DOC_BY_MODULE[cls.__module__]
    lines = doc.read_text(encoding="utf-8").split("\n")
    sections = [s for s in _doc_sections(lines) if cls.__name__ in s[0]]
    assert len(sections) == 1, (
        f"{cls.__name__} needs exactly one section in {doc.name}, found {len(sections)}"
    )
    _, start, end = sections[0]
    rows = [lines[i] for i in range(start, end) if _OWNERSHIP_DOC_ROW.match(lines[i])]
    assert len(rows) == 1, (
        f"{cls.__name__}'s table in {doc.name} needs exactly one ownership row, "
        f"found {len(rows)}"
    )
    cells = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    assert cells[0] == "`tenant_id`, `actor_id`, `actor_kind`, `user_id`", cells[0]
    assert cells[1] == "inherited from `Ownership`", cells[1]
    assert cells[2] == _expected_ownership_doc_sentence(cls), (
        f"{doc.name} states the wrong tier for {cls.__name__}"
    )


def test_no_doc_field_table_states_the_subjectless_rule_for_a_subject_bound_record():
    """The same check from the other end: a repo-wide scan for the old text.

    The per-class test proves each row is right. This proves no OTHER row
    anywhere in the frozen prose still carries the pre-fix sentence, which is
    how the contradiction survived a whole review round.
    """
    stale = "may be null only for a system actor"
    offenders = [
        f"{path.name}:{number}"
        for path in sorted((REPO_ROOT / "docs" / "contracts").glob("*.md"))
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if stale in line
    ]
    assert offenders == [], f"pre-fix ownership sentence still in the freeze: {offenders}"


def test_the_two_validators_agree_on_non_string_identity_values():
    """The corpus can only fuzz values the corpus contains, so pin the rest.

    The fixture invariant coerced with ``str(x or "")``, so ``12345``,
    ``True``, ``{"a": 1}`` and ``["x"]`` all became non-blank strings and
    passed, while the runtime guard's ``isinstance(x, str)`` rejected them:
    240 disagreements in a differential fuzz, every one fixture-lax /
    runtime-strict. The corpus holds no such record, so only this test sees
    it. Both sides use the same predicate now; this pins that they do.
    """
    junk = (12345, True, {"a": 1}, ["x"], None, "", "   ")
    for value in junk:
        for field in ("tenant_id", "actor_id"):
            payload = _owned_envelope_payload(**{field: value})
            with pytest.raises(ContractViolation):
                _invariant_ownership(payload, contracts.ReceiptEnvelope)
            record = dataclasses.replace(_envelope(), **{field: value})
            assert ownership_violations(record), (field, value)
    for value in (12345, True, {"a": 1}, ["x"], "", "   "):
        payload = _owned_envelope_payload(user_id=value)
        with pytest.raises(ContractViolation):
            _invariant_ownership(payload, contracts.ReceiptEnvelope)
        assert ownership_violations(dataclasses.replace(_envelope(), user_id=value))


def test_the_ownership_pattern_gate_catches_renamed_identity_fields():
    """Must-fix 3's red gate: a renamed actor binding is visible again."""
    assert _ownership_semantic_fields(_RenamedIdentityRecord) == {"actor_identity"}
    assert _ownership_semantic_fields(_NeutralRecord) == set()
    for name in OWNERSHIP_FIELD_NAMES:
        assert _is_ownership_semantic_name(name)
        assert _is_ownership_semantic_name(name.upper())
    for suffix in ("_actor", "_tenant", "_user", "_principal", "_owner", "_subject"):
        assert _is_ownership_semantic_name("created_by" + suffix)
    assert not _is_ownership_semantic_name("flavor_text")
    assert not _is_ownership_semantic_name("subject_line")


def test_every_widened_gate_hit_is_owned_or_exempt_with_a_reason():
    """The widened matcher must not have quietly created an unreviewed class.

    ``Tenant.is_public_projection_tenant`` is the one new hit: a boolean flag
    whose name ends in ``_tenant``. ``Tenant`` was already exempt with a
    written reason, so the widening changes nothing about what ships.
    """
    for name, cls in sorted(_contract_dataclasses().items()):
        if cls is Ownership or issubclass(cls, Ownership):
            continue
        if not _ownership_semantic_fields(cls):
            continue
        assert name in OWNERSHIP_EXEMPT and OWNERSHIP_EXEMPT[name].strip(), name


def test_the_validator_agreement_walk_reaches_tuple_and_optional_records():
    """The should-fix: the walk descends wherever the pattern gate descends.

    No frozen contract nests an owned record inside a tuple today, so the
    proof is a specimen rather than a fixture: a corpus fixture would have to
    invent a contract shape that does not exist.
    """
    payload = {"envelopes": [_owned_envelope_payload(), _owned_envelope_payload(kind="ranking")]}
    reached = list(_owned_payloads(_TupleOfOwnedRecords, payload))
    assert [cls.__name__ for cls, _ in reached] == ["ReceiptEnvelope", "ReceiptEnvelope"]

    optional = {"envelope": _owned_envelope_payload()}
    reached_optional = list(_owned_payloads(_OptionalOwnedRecord, optional))
    assert [cls.__name__ for cls, _ in reached_optional] == ["ReceiptEnvelope"]
    assert _owned_payloads(_OptionalOwnedRecord, {"envelope": None}) is not None
    assert list(_owned_payloads(_OptionalOwnedRecord, {"envelope": None})) == []


# ---------------------------------------------------------------------------
# Round-4: canonical ids, derived canonical tables, layering
# ---------------------------------------------------------------------------


#: The four attacks round 4 named, plus the two the SQL layer used to accept.
#: Every one of them is a SECOND encoding of an id that already exists, which
#: is the whole hazard: a per-person delete keyed on the canonical spelling
#: does not find the row.
NONCANONICAL_IDS = (
    " user-1 ",
    "\tuser-1",
    "user​1",
    "　",
    "user-1\n",
    "user 1",
    "﻿user-1",
    # Zl / Zp, added 2026-09-02. Python's str.strip() removes a LEADING or
    # TRAILING U+2028, so those two were REJECTED by Python and ACCEPTED by
    # the SQL check: btrim is spaces-only and the class did not list them.
    # The interior pair is caught by the class, in both layers.
    "\u2028user-1",
    "user-1\u2028",
    "user\u2028-1",
    "user-1\u2029",
    "user\u2029-1",
    # The astral half of the SQL bracket class had never been attacked.
    # U+E0020 is a TAG character (Cf), the building block of emoji flag
    # sequences, and it exercises the 8-hex-digit \\UXXXXXXXX escape branch
    # rather than the 4-digit one every other value here exercises.
    "user\U000E0020-1",
    # NFD: "jose" + U+0301 renders identically to the NFC spelling in
    # CANONICAL_IDS below, so the two are one subject in two encodings and a
    # per-person delete keyed on either spelling misses the other row.
    "jose\u0301-1",
)

#: Ids that must be ACCEPTED. Without a positive corpus the canonical rule
#: could be "refuse everything" and every attack above would still pass.
CANONICAL_IDS = (
    "user-1",
    "jos\u00e9-1",  # the NFC spelling of the NFD attack above
    "\u7528\u6237-1",  # CJK
    "\ud55c\uad6d\uc5b4-1",  # Hangul
    "\u0645\u0633\u062a\u062e\u062f\u0645-1",  # Arabic
    "\u0915\u094d\u0937-1",  # Devanagari with a combining mark
    "user\U0001F600",  # a single emoji
    "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
)


def test_the_frozen_invisible_set_still_covers_every_unicode_invisible():
    """The frozen ranges are written out, so a Unicode upgrade must be noticed.

    Computing the set from ``unicodedata`` at import time would keep Python in
    step and silently leave the migration's CHECK text behind, because that
    text is frozen in a file. So the ranges are literal and this test is the
    alarm: it goes red when the running Python knows an invisible character the
    frozen list does not, and the fix is a deliberate edit to both.
    """
    import unicodedata

    live = {
        code
        for code in range(0x110000)
        if unicodedata.category(chr(code)) in {"Zs", "Zl", "Zp", "Cc", "Cf"}
    }
    assert live <= set(INVISIBLE_ID_CODE_POINTS), (
        "unicodedata contains an invisible point outside INVISIBLE_ID_CODE_POINT_RANGES; update the "
        "frozen ranges AND regenerate the migration's check text"
    )
    # The ones the reviews named by hand, pinned so a range edit cannot drop one.
    # U+2028 (Zl) and U+2029 (Zp) joined on 2026-09-02: the set was Zs/Cc/Cf
    # only, str.strip() removes them and btrim does not, so a trailing U+2028
    # was rejected by Python and accepted by the database.
    for named in (
        0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00A0, 0x3000, 0x2028, 0x2029,
    ):
        assert named in INVISIBLE_ID_CODE_POINTS, hex(named)
    assert unicodedata.category("\u2028") == "Zl"
    assert unicodedata.category("\u2029") == "Zp"


_MIGRATION = REPO_ROOT / "supabase" / "migrations" / "202609020001_learning_ledger.sql"
_UNICODE15_MIGRATION = (
    REPO_ROOT / "supabase" / "migrations" / "202609040001_unicode15_invisible_ids.sql"
)
_SQL_OWNERSHIP_CHECK = re.compile(r"^\s*(check \((\w+) = btrim\(.*\))(,?)$", re.M)
_SQL_CODE_POINT = re.compile(r"\\u([0-9A-F]{4})|\\U([0-9A-F]{8})")


def _sql_class_code_points(check_text: str) -> set[int]:
    """Every code point the check's bracket expression actually covers."""
    body = check_text.split("!~ '[", 1)[1].rsplit("]'", 1)[0]
    tokens = [
        int(match.group(1) or match.group(2), 16)
        for match in _SQL_CODE_POINT.finditer(body)
    ]
    # Ranges are written ``ꪪ-뮻``; a lone escape is a single point.
    pieces = body.split("-")
    covered: set[int] = set()
    index = 0
    while index < len(tokens):
        starts_range = (
            index + 1 < len(tokens)
            and f"\\u{tokens[index]:04X}-" in body
            or index + 1 < len(tokens)
            and f"\\U{tokens[index]:08X}-" in body
        )
        if starts_range:
            covered.update(range(tokens[index], tokens[index + 1] + 1))
            index += 2
        else:
            covered.add(tokens[index])
            index += 1
    assert pieces  # the split is only used to prove the body is non-empty
    return covered


def test_the_migration_ownership_checks_are_derived_from_the_frozen_set():
    """The SQL half of the canonical rule, regenerated and compared.

    ``btrim(x) <> ''`` strips SPACES ONLY, so the database accepted a tab, a
    newline, U+00A0 and U+3000 that both Python validators rejected: a live
    INSERT of an ideographic-space ``user_id`` succeeded and
    ``where user_id is null`` did not find it. Every ownership check is
    generated from the same frozen ranges now, and this test regenerates the
    text rather than eyeballing it.
    """
    sql = _MIGRATION.read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in sql.split("\n") if not line.lstrip().startswith("--")
    )
    assert "btrim(" in statements, "the checks still use btrim for the trim half"
    assert not re.search(r"btrim\(\w+\) <> ''", statements), (
        "a spaces-only blank check is back in the migration"
    )
    matches = _SQL_OWNERSHIP_CHECK.findall(sql)
    # 28, not 27: round 6 added tenant_members.tenant_id, the one column in
    # this migration that carried none of the three canonical clauses the
    # other 9 tables' 27 columns all did (9 tables x 3 columns).
    assert len(matches) == 28, f"expected 28 ownership checks, found {len(matches)}"

    owned_tables = (
        "learning_events",
        "correction_events",
        "raw_imports",
        "evidence_items",
        "knowledge_artifacts",
        "artifact_versions",
        "artifact_relations",
        "deletion_receipts",
        "mirror_receipts",
    )
    expected = Counter({("tenant_members", "tenant_id"): 1})
    expected.update((table, column) for table in owned_tables for column in (
        "tenant_id",
        "actor_id",
        "user_id",
    ))
    observed: Counter[tuple[str, str]] = Counter()
    for table, block in re.findall(
        r"create table public\.(\w+) \((.*?)\n\);", sql, re.S
    ):
        observed.update((table, column) for _, column, _ in _SQL_OWNERSHIP_CHECK.findall(block))
    assert observed == expected, (
        "ownership checks moved to the wrong table or column: "
        f"missing={expected - observed}, extra={observed - expected}"
    )
    for text, column, _ in matches:
        assert text == ownership_id_sql_check(column), (
            f"{column}'s check drifted from curator.ownership.ownership_id_sql_check"
        )
    # Not just "the text matches": the class must COVER every frozen code point.
    assert _sql_class_code_points(matches[0][0]) == set(INVISIBLE_ID_CODE_POINTS)
    assert {first for first, _ in INVISIBLE_ID_CODE_POINT_RANGES} <= set(
        INVISIBLE_ID_CODE_POINTS
    )


def test_the_unicode15_additive_migration_covers_every_ownership_column():
    """Already-deployed databases receive the same Unicode 15 guard."""
    sql = _UNICODE15_MIGRATION.read_text(encoding="utf-8")
    guard = "set standard_conforming_strings = on;"
    assert guard in sql
    assert sql.index(guard) < sql.index("add constraint")
    owned_tables = (
        "learning_events",
        "correction_events",
        "raw_imports",
        "evidence_items",
        "knowledge_artifacts",
        "artifact_versions",
        "artifact_relations",
        "deletion_receipts",
        "mirror_receipts",
    )
    expected = {("tenant_members", "tenant_id")}
    expected.update(
        (table, column)
        for table in owned_tables
        for column in ("tenant_id", "actor_id", "user_id")
    )
    observed = set(
        re.findall(
            r"alter table public\.(\w+) add constraint \w+ check "
            r"\((\w+) !~ '\[\\U00013439-\\U0001343F\]'\);",
            sql,
        )
    )
    assert observed == expected, (
        "Unicode 15 compatibility checks moved to the wrong table or column: "
        f"missing={expected - observed}, extra={observed - expected}"
    )
    assert sql.count("add constraint") == 28


#: The SQL bracket class, translated into a Python regex so the two layers can
#: be compared character by character in one process.
#:
#: CAVEAT, stated rather than hidden: Postgres uses ARE (advanced regular
#: expressions) and this is Python's ``re``. The translation is sound ONLY
#: because the generated class is a pure bracket expression of ``\uXXXX`` and
#: ``\UXXXXXXXX`` escapes and ``-`` ranges, a construct both engines spell and
#: read identically. The third-party ``regex`` module, which is closer to ARE,
#: is not installed in this venv, and this test may not install anything. What
#: this test therefore proves is that the FROZEN SET and the CLASS TEXT list the
#: same characters and that the Python predicate agrees with that list; what it
#: does not prove is Postgres's own parse of the text. That is the live
#: migration execution's job, and its attack list is the reason the astral and
#: separator values were added to ``NONCANONICAL_IDS``.
_SQL_CLASS_AS_PYTHON = re.compile(INVISIBLE_ID_SQL_CLASS)

#: Letters that must stay legal in an id, across the scripts this product
#: actually sees plus the shapes reviews have asked about.
_LEGITIMATE_ID_CHARACTERS = (
    ("Latin", "a"),
    ("Latin precomposed", "\u00e9"),
    ("CJK", "\u7528"),
    ("Hangul", "\ud55c"),
    ("Arabic", "\u0645"),
    ("Devanagari combining mark", "\u094d"),
    ("emoji", "\U0001F600"),
    ("digit", "7"),
    ("hyphen", "-"),
    ("underscore", "_"),
)


def test_the_python_predicate_and_the_sql_class_return_the_same_verdict():
    """Two layers, one list, compared character by character.

    The layers have diverged twice on exactly this question: first ``btrim``
    stripped spaces only while Python's ``strip()`` stripped all whitespace,
    then the frozen set omitted U+2028 and U+2029 (``Zl`` and ``Zp``) which
    ``strip()`` removes and ``btrim`` does not. Both times the disagreement was
    invisible because nothing compared the two verdicts value by value. This
    does, in both directions: every frozen code point must be refused by both,
    and every legitimate letter must be accepted by both.
    """
    for code in sorted(INVISIBLE_ID_CODE_POINTS):
        probe = f"user{chr(code)}-1"
        sql_refuses = bool(_SQL_CLASS_AS_PYTHON.search(probe))
        python_refuses = noncanonical_id_reason(probe) is not None
        assert sql_refuses, f"U+{code:04X} is frozen but the SQL class misses it"
        assert python_refuses == sql_refuses, (
            f"U+{code:04X}: python refuses={python_refuses}, sql refuses={sql_refuses}"
        )

    for label, char in _LEGITIMATE_ID_CHARACTERS:
        probe = unicodedata.normalize("NFC", f"user{char}1")
        sql_refuses = bool(_SQL_CLASS_AS_PYTHON.search(probe))
        python_refuses = noncanonical_id_reason(probe) is not None
        assert not sql_refuses, f"{label} is refused by the SQL class"
        assert not python_refuses, f"{label} is refused by the Python predicate"

    # The separators the frozen set used to omit, in all three positions. The
    # leading and trailing forms are the ones that used to SPLIT the layers.
    for separator in ("\u2028", "\u2029"):
        for probe in (f"{separator}user-1", f"user-1{separator}", f"user{separator}-1"):
            assert noncanonical_id_reason(probe) is not None, repr(probe)
            assert _SQL_CLASS_AS_PYTHON.search(probe), repr(probe)


@pytest.mark.parametrize("value", CANONICAL_IDS, ids=[repr(v) for v in CANONICAL_IDS])
def test_both_validators_accept_a_canonical_ownership_id(value):
    """The positive control for the whole canonical rule.

    Every attack test above stays green under a predicate that refuses
    everything. These ids (CJK, Hangul, Arabic, a Devanagari combining mark, an
    emoji, a uuid, and the NFC spelling of the NFD attack) must be ACCEPTED by
    both validators, so the rule is proven to be a filter and not a wall.
    """
    assert noncanonical_id_reason(value) is None, noncanonical_id_reason(value)
    for field in ("tenant_id", "actor_id", "user_id"):
        _invariant_ownership(_owned_envelope_payload(**{field: value}), contracts.ReceiptEnvelope)
        assert ownership_violations(dataclasses.replace(_envelope(), **{field: value})) == ()


def test_an_ownership_id_must_be_in_normalization_form_nfc():
    """NFC and NFD are two encodings of one subject, and one row must win.

    ``"jos\u00e9-1"`` (6 code points) and ``"jose\u0301-1"`` (7) render
    identically. Before this rule both were accepted, so they were two rows no
    reviewer could tell apart and a per-person delete keyed on the composed
    spelling missed the decomposed one. This product has Chinese input paths and
    macOS filesystem APIs return decomposed strings, so a decomposed id arrives
    through the same door U+3000 arrives through.

    What the rule refuses when its assumption is wrong: a caller legitimately
    holding a decomposed id from an external system. That failure is loud, at
    the first write, and one ``unicodedata.normalize`` call in the caller.
    """
    nfd = "jose\u0301-1"
    nfc = unicodedata.normalize("NFC", nfd)
    assert nfd != nfc and len(nfd) == len(nfc) + 1

    assert "NFC" in (noncanonical_id_reason(nfd) or "")
    assert noncanonical_id_reason(nfc) is None
    assert noncanonical_id_reason("\u7528\u6237-1") is None  # CJK is already NFC

    for field in ("tenant_id", "actor_id", "user_id"):
        with pytest.raises(ContractViolation):
            _invariant_ownership(_owned_envelope_payload(**{field: nfd}), contracts.ReceiptEnvelope)
        assert ownership_violations(dataclasses.replace(_envelope(), **{field: nfd}))
        _invariant_ownership(_owned_envelope_payload(**{field: nfc}), contracts.ReceiptEnvelope)
        assert ownership_violations(dataclasses.replace(_envelope(), **{field: nfc})) == ()

    # The SQL half is a clause on every generated check, not a comment.
    assert "is nfc normalized" in ownership_id_sql_check("user_id")
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert sql.count("is nfc normalized") >= 27


def test_the_migration_pins_standard_conforming_strings():
    """The 27 check texts are only regex escapes while this setting is on.

    They are plain single-quoted literals containing backslashes. With
    ``standard_conforming_strings = on`` (the Postgres default since 9.1) the
    backslashes reach the regex engine, which is what makes them escapes. With
    it off the same literal is parsed as an escape string and the leading NUL
    escape is a string-literal error, so the migration fails to apply. The file
    already sets ``search_path`` for the same reason: a migration should not
    depend on an unstated session setting.
    """
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "set standard_conforming_strings = on;" in sql
    guard = sql.index("set standard_conforming_strings = on;")
    first_check = sql.index("check (tenant_id = btrim(")
    assert guard < first_check, "the guard must precede the checks it protects"
    assert "E'" not in sql, "an escape-string literal would not need the guard"


def test_the_ledger_ownership_shim_re_exports_the_same_objects():
    """Nothing kept the compatibility shim a shim.

    ``curator/ledger/ownership.py`` re-exports ``curator/ownership.py`` so old
    import sites keep working. A later "small compatibility tweak" that gave the
    shim a local definition would recreate two copies of the one rule for two
    write paths, which is the defect class this change set exists to remove, and
    the suite would stay green. Identity, not equality: two functions with the
    same source are still two rules.
    """
    import curator.ledger.ownership as shim
    import curator.ownership as real

    assert shim.__all__
    for name in shim.__all__:
        assert getattr(shim, name) is getattr(real, name), name


@pytest.mark.parametrize("value", NONCANONICAL_IDS, ids=[repr(v) for v in NONCANONICAL_IDS])
def test_both_validators_reject_a_noncanonical_ownership_id(value):
    """Non-blank was never enough, and both layers must agree that it is not."""
    assert noncanonical_id_reason(value) is not None or _is_blank(value)
    for field in ("tenant_id", "actor_id", "user_id"):
        payload = _owned_envelope_payload(**{field: value})
        with pytest.raises(ContractViolation):
            _invariant_ownership(payload, contracts.ReceiptEnvelope)
        record = dataclasses.replace(_envelope(), **{field: value})
        assert ownership_violations(record), (field, value)
    # The control: the canonical spelling of the same id is accepted by both.
    payload = _owned_envelope_payload(user_id="user-1")
    _invariant_ownership(payload, contracts.ReceiptEnvelope)
    assert ownership_violations(dataclasses.replace(_envelope(), user_id="user-1")) == ()


def test_a_settled_limit_receipt_must_read_at_least_one_meter():
    """The empty-collection hole, closed for meters as it already was for projections.

    ``readings: []`` made the freshness loop vacuous, so a receipt that read NO
    meter at all settled green. That is the strongest instance of "a receipt
    whose meters cannot be read settles ``unknown``", not an exception to it.
    """
    good = _load(FIXTURE_ROOT / "receipt" / "valid-limit-receipt-unknown-meter.json")
    seeded = json.loads(json.dumps(good))
    seeded["payload"]["envelope"]["state"] = "settled"
    seeded["payload"]["envelope"]["settled_at"] = "2026-09-01T12:05:00+00:00"
    seeded["payload"]["envelope"]["reason_code"] = ""
    seeded["payload"]["final_state"] = "settled"
    seeded["payload"]["readings"] = []
    with pytest.raises(ContractViolation) as raised:
        validate_fixture(seeded)
    assert "at least one meter" in str(raised.value)


@dataclasses.dataclass(frozen=True)
class _RenamedEnvelopeReceipt:
    """Red-gate specimen: a wrapper whose envelope field is NOT called ``envelope``."""

    receipt_envelope: contracts.ReceiptEnvelope


def test_an_unlisted_envelope_shape_is_not_a_frozen_wrapper():
    """Wrapper identity comes only from the closed frozen class set."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    wrapper = _RenamedEnvelopeReceipt(
        receipt_envelope=_envelope(kind="ranking", actor_kind=ActorKind.SYSTEM, user_id=None)
    )
    assert not is_receipt_wrapper(wrapper)
    assert not _is_receipt_wrapper_class(_RenamedEnvelopeReceipt)
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().record_deletion_receipt(wrapper)
    assert not is_receipt_wrapper(_envelope())
    assert {
        name for name, cls in _contract_dataclasses().items() if _is_receipt_wrapper_class(cls)
    } == {cls.__name__ for cls, _, _ in RECEIPT_WRAPPER_KINDS}


@dataclasses.dataclass(frozen=True)
class _TwoEnvelopeReceipt(contracts.DeletionReceipt):
    """Red-gate specimen: a PINNED wrapper carrying a second envelope."""

    secondary_envelope: contracts.ReceiptEnvelope | None = None


def test_a_receipt_wrapper_carries_exactly_one_envelope():
    """A subclass cannot add a second envelope to a frozen wrapper."""
    from curator.ledger.memory import InMemoryLedgerStore, LedgerError

    rogue = _envelope(kind="ranking", actor_kind=ActorKind.SYSTEM, user_id=None)
    assert ownership_violations(rogue), "the specimen envelope must itself be invalid"

    wrapper = _TwoEnvelopeReceipt(
        envelope=_envelope(kind="deletion"),
        target_kind="user",
        target_ids=("user-owner",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-1",
        zero_contribution_verdict=True,
        projections=(),
        mirrored_targets=(),
        secondary_envelope=rogue,
    )
    assert not is_receipt_wrapper(wrapper)
    assert _pinned_envelope_kind(_TwoEnvelopeReceipt) is None
    assert any(
        "unknown record type" in problem for problem in receipt_wrapper_violations(wrapper)
    ), receipt_wrapper_violations(wrapper)
    assert any(
        "unknown record type" in problem for problem in ownership_violations(wrapper)
    ), ownership_violations(wrapper)
    with pytest.raises(LedgerError, match="unknown record type"):
        InMemoryLedgerStore().record_deletion_receipt(wrapper)


@dataclasses.dataclass(frozen=True)
class _DeepA:
    x: contracts.ReceiptEnvelope


@dataclasses.dataclass(frozen=True)
class _DeepB:
    x: list[contracts.ReceiptEnvelope | None]


@dataclasses.dataclass(frozen=True)
class _DeepC:
    x: dict[str, contracts.ReceiptEnvelope]


@dataclasses.dataclass(frozen=True)
class _DeepD:
    x: tuple[list[contracts.ReceiptEnvelope], ...]


@dataclasses.dataclass(frozen=True)
class _DeepE:
    x: typing.Annotated[contracts.ReceiptEnvelope, "note"]


@dataclasses.dataclass(frozen=True)
class _DeepF:
    x: int


@pytest.mark.parametrize(
    "cls,field_name,_kind",
    RECEIPT_WRAPPER_KINDS,
    ids=[cls.__name__ for cls, _, _ in RECEIPT_WRAPPER_KINDS],
)
def test_each_frozen_wrapper_has_exactly_one_envelope_typed_field(
    cls, field_name, _kind
):
    """The annotation walk is static and limited to the reviewed closed set."""
    assert _envelope_field_names(cls) == (field_name,)


@dataclasses.dataclass(frozen=True)
class _ChainedDeletionReceipt(contracts.DeletionReceipt):
    """Red-gate specimen: the round-6 hole, one container level deep."""

    chained: list[contracts.ReceiptEnvelope | None] = dataclasses.field(default_factory=list)


def test_a_container_of_union_envelope_is_detected_and_the_store_rejects_it():
    """End to end: a rogue envelope hidden in ``list[ReceiptEnvelope | None]``
    used to be invisible to every guard and was ACCEPTED by
    ``InMemoryLedgerStore.record_deletion_receipt``. Reproduced per round 6's
    ``r6q7.py`` and closed by the total annotation walk above.
    """
    from curator.ledger.memory import InMemoryLedgerStore

    rogue = _envelope(kind="ranking", actor_kind=ActorKind.AGENT, user_id=None)
    assert ownership_violations(rogue), "the specimen envelope must itself be invalid"

    wrapper = _ChainedDeletionReceipt(
        envelope=_envelope(kind="deletion"),
        target_kind="user",
        target_ids=("user-owner",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-1",
        zero_contribution_verdict=True,
        projections=(),
        mirrored_targets=(),
        chained=[rogue],
    )
    assert not is_receipt_wrapper(wrapper)
    assert any(
        "unknown record type" in problem for problem in receipt_wrapper_violations(wrapper)
    ), receipt_wrapper_violations(wrapper)
    assert any(
        "unknown record type" in problem for problem in ownership_violations(wrapper)
    ), ownership_violations(wrapper)

    store = InMemoryLedgerStore()
    with pytest.raises(Exception, match="unknown record type"):
        store.record_deletion_receipt(wrapper)


@dataclasses.dataclass(frozen=True)
class _UnresolvableAnnotationRogueDeletionReceipt(contracts.DeletionReceipt):
    """Round 7 red-gate specimen: the exact reproduction from the round-7
    Codex review.

    Three ingredients, all required to reproduce the hole: (1) the valid
    inherited ``envelope`` field every ``DeletionReceipt`` carries, (2) a
    SECOND, rogue ``ReceiptEnvelope`` field naming no wrapper kind, and (3)
    one unrelated field whose annotation cannot be resolved at all (a forward
    reference to a name that is never defined anywhere). Before the round-7
    fix, ``typing.get_type_hints(cls)`` raised on ingredient 3 for the WHOLE
    class, and ``except Exception: hints = {}`` turned that failure into "no
    envelope fields found": ``_envelope_field_names`` returned ``()``,
    ``receipt_wrapper_violations`` returned clean, ``ownership_violations``
    returned clean, and ``InMemoryLedgerStore.record_deletion_receipt``
    accepted the rogue envelope. The unresolvable field itself is otherwise
    inert; it exists only to break ``get_type_hints`` for this class.
    """

    rogue_envelope: contracts.ReceiptEnvelope = dataclasses.field(default=None)  # type: ignore[assignment]
    unresolvable: "_ThisNameIsIntentionallyNeverDefinedAnywhere" = None  # type: ignore[name-defined]  # noqa: F821


def test_unresolvable_wrapper_subclass_is_refused_without_runtime_annotation_walk():
    """Unknown subclasses fail by identity before annotations matter."""
    from curator.ledger.memory import InMemoryLedgerStore

    cls = _UnresolvableAnnotationRogueDeletionReceipt

    # get_type_hints genuinely cannot resolve this class: confirm the
    # ingredient is real, not merely assumed.
    with pytest.raises(NameError):
        typing.get_type_hints(cls)

    rogue = _envelope(kind="ranking", actor_kind=ActorKind.AGENT, user_id=None)
    assert ownership_violations(rogue), "the specimen envelope must itself be invalid"

    wrapper = cls(
        envelope=_envelope(kind="deletion"),
        target_kind="user",
        target_ids=("user-owner",),
        correction_watermark=datetime.fromisoformat("2026-09-01T12:00:00+00:00"),
        invalidated_snapshot_ids=(),
        rebuild_id="rebuild-1",
        zero_contribution_verdict=True,
        projections=(),
        mirrored_targets=(),
        rogue_envelope=rogue,
    )

    assert not is_receipt_wrapper(wrapper)
    assert any(
        "unknown record type" in problem
        for problem in receipt_wrapper_violations(wrapper)
    ), receipt_wrapper_violations(wrapper)
    assert any(
        "unknown record type" in problem for problem in ownership_violations(wrapper)
    ), ownership_violations(wrapper)

    store = InMemoryLedgerStore()
    with pytest.raises(Exception, match="unknown record type"):
        store.record_deletion_receipt(wrapper)


# ---------------------------------------------------------------------------
# The three CANONICAL tables every per-record row links to
# ---------------------------------------------------------------------------


def _generated_content(path: Path, name: str) -> str:
    """Return the literal bytes between one generated marker pair."""
    text = path.read_text(encoding="utf-8")
    start, end = generated_markers(name)
    assert text.count(start) == 1, f"{path}: expected one {start!r} marker"
    before, remainder = text.split(start, 1)
    assert end in remainder, f"{path}: {start!r} has no closing marker"
    rendered, after = remainder.split(end, 1)
    assert before or after  # the markers belong inside a real document
    return rendered


def test_generated_contract_tables_match_renderer_byte_for_byte():
    """Canonical tables are rendered from frozen tuples, never hand-maintained."""
    for name, (relative_path, renderer) in GENERATED_TABLES.items():
        actual = _generated_content(REPO_ROOT / relative_path, name)
        assert actual == renderer(), (
            f"{relative_path}: generated table {name!r} drifted; edit the frozen "
            "tuples and run scripts/render_contract_tables.py"
        )


def _normalized_contract_lines(path: Path) -> list[str]:
    """NFKC-normalize prose and remove every frozen invisible code point."""
    text = unicodedata.normalize("NFKC", path.read_text(encoding="utf-8"))
    return [
        "".join(char for char in line if ord(char) not in INVISIBLE_ID_CODE_POINTS)
        for line in text.splitlines()
    ]


def _normalized_identifiers(line: str) -> set[str]:
    """Tokenize after NFKC and invisible removal without joining words."""
    tokens: set[str] = set()
    current: list[str] = []
    for char in unicodedata.normalize("NFKC", line):
        code_point = ord(char)
        if code_point in INVISIBLE_ID_CODE_POINTS:
            if unicodedata.category(char) != "Cf" and current:
                tokens.add("".join(current))
                current = []
            continue
        if char.isascii() and (char.isalnum() or char == "_"):
            current.append(char)
        elif current:
            tokens.add("".join(current))
            current = []
    if current:
        tokens.add("".join(current))
    return tokens


def _line_tables(lines: list[str]) -> list[tuple[int, int]]:
    """Return half-open spans for every table, including fenced tables."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        is_row = line.lstrip().startswith("|")
        if is_row and start is None:
            start = index
        elif not is_row and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def _frozen_table_vocabulary() -> set[str]:
    """Names allowed in markdown table cells only inside generated blocks."""
    return (
        {cls.__name__ for cls in OWNED_RECORDS}
        | {kind for kind, _ in RECEIPT_KIND_TIERS}
        | {wrapper.__name__ for wrapper, _, _ in RECEIPT_WRAPPER_KINDS}
    )


def _generated_line_indexes(path: Path, lines: list[str]) -> set[int]:
    """Every line strictly between a generated marker pair in one document."""
    generated: set[int] = set()
    relative_path = path.relative_to(REPO_ROOT)
    for name, (table_path, _) in GENERATED_TABLES.items():
        if table_path != relative_path:
            continue
        start_marker, end_marker = generated_markers(name)
        starts = [index for index, line in enumerate(lines) if line.strip() == start_marker]
        assert len(starts) == 1, f"{path}: missing or duplicate {start_marker}"
        start = starts[0]
        ends = [
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if line.strip() == end_marker
        ]
        assert ends, f"{path}: {start_marker} has no closing marker"
        end = ends[0]
        assert start < end, f"{path}: reversed markers for {name}"
        generated.update(range(start + 1, end))
    return generated


def test_no_frozen_name_appears_in_a_markdown_table_outside_generated_markers():
    """No hand-written table may restate a frozen class, kind, or wrapper.

    This scans every cell under every header in every contract document. Plain
    and backticked names tokenize identically. A field table may name its class
    in the heading above it, but never in a table cell.
    """
    vocabulary = _frozen_table_vocabulary()
    for path in sorted((REPO_ROOT / "docs" / "contracts").glob("*.md")):
        source_lines = path.read_text(encoding="utf-8").split("\n")
        lines = _normalized_contract_lines(path)
        generated = _generated_line_indexes(path, source_lines)
        for start, end in _line_tables(lines):
            for index in range(start, end):
                if index in generated:
                    continue
                identifiers = _normalized_identifiers(source_lines[index])
                hit = sorted(identifiers & vocabulary)
                assert not hit, (
                    f"{path.relative_to(REPO_ROOT)}:{index + 1}: markdown table "
                    f"outside generated markers names frozen vocabulary {hit}"
                )


_TABLE_GUARD_MUTATIONS = (
    (
        "plain wrong table inside generated markers",
        "inside",
        (
            "\n| Any header | Any claim |\n"
            "|---|---|\n"
            "| wrong tier | Slate is subjectless |\n"
        ),
        test_generated_contract_tables_match_renderer_byte_for_byte,
    ),
    (
        "plain wrong table outside generated markers",
        "outside",
        (
            "| Any header | Any claim |\n"
            "|---|---|\n"
            "| wrong tier | Slate is subjectless |\n\n"
        ),
        test_no_frozen_name_appears_in_a_markdown_table_outside_generated_markers,
    ),
    (
        "fenced wrong table outside generated markers",
        "outside",
        (
            "```text\n"
            "| Any header | Any claim |\n"
            "|---|---|\n"
            "| wrong tier | Slate is subjectless |\n"
            "```\n\n"
        ),
        test_no_frozen_name_appears_in_a_markdown_table_outside_generated_markers,
    ),
    (
        "zero-width split frozen name outside generated markers",
        "outside",
        (
            "| Any header | Any claim |\n"
            "|---|---|\n"
            "| wrong tier | Sla\u200bte is subjectless |\n\n"
        ),
        test_no_frozen_name_appears_in_a_markdown_table_outside_generated_markers,
    ),
)


@pytest.mark.parametrize(
    "label,location,wrong_table,guard",
    _TABLE_GUARD_MUTATIONS,
    ids=[mutation[0] for mutation in _TABLE_GUARD_MUTATIONS],
)
def test_generated_table_guards_reject_plain_name_mutations(
    tmp_path, monkeypatch, label, location, wrong_table, guard
):
    """Both new gates prove they go red on wrong tables under arbitrary headers."""
    import shutil

    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "docs" / "contracts", root / "docs" / "contracts")
    monkeypatch.setitem(globals(), "REPO_ROOT", root)

    test_generated_contract_tables_match_renderer_byte_for_byte()
    test_no_frozen_name_appears_in_a_markdown_table_outside_generated_markers()

    path = root / "docs" / "contracts" / "tenant.md"
    text = path.read_text(encoding="utf-8")
    start, end = generated_markers("ownership-classification")
    if location == "inside":
        before, remainder = text.split(start, 1)
        _, after = remainder.split(end, 1)
        mutated = before + start + wrong_table + end + after
    else:
        mutated = text.replace(start, wrong_table + start, 1)
    path.write_text(mutated, encoding="utf-8")

    with pytest.raises(AssertionError):
        guard()


def _prose_scan_files() -> tuple[str, ...]:
    """Every file whose prose could state a stale tier, DERIVED not listed.

    The list used to be seven literal paths, so a stale explanation in
    ``candidate.md``, in a README, or in a module added next week was invisible
    to the scan: the same shape as the hand-maintained canonical tables one
    level up. It is derived now: every frozen contract doc, plus every Python
    file that imports the frozen tuples (that import is what makes a file an
    explainer of the classification), plus the tests that assert it.
    """
    paths = {str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / "docs" / "contracts").glob("*.md")}
    for directory in ("curator", "tests"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            markers = ("SUBJECT_BOUND_RECORDS", "RECEIPT_KIND_TIERS", "ownership_violations")
            if any(marker in text for marker in markers):
                paths.add(str(path.relative_to(REPO_ROOT)))
    return tuple(sorted(paths))


_PROSE_SCAN_FILES = _prose_scan_files()


def _prose_lines(path: str) -> list[str]:
    """Human-facing text only: comments, docstrings, and non-table markdown.

    Data literals are excluded on purpose: the frozen kind map holds both tier
    words as neighbouring VALUES, and a table row lists both tiers, and neither
    is a claim about any one kind. The failure this scan exists to catch is an
    EXPLANATION that still states the pre-2026-09-02 tier in prose.
    """
    import ast as _ast

    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    if path.endswith(".md"):
        return [line for line in text.split("\n") if not line.lstrip().startswith("|")]
    lines = [line for line in text.split("\n") if line.lstrip().startswith("#")]
    for node in _ast.walk(_ast.parse(text)):
        if isinstance(
            node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)
        ):
            doc = _ast.get_docstring(node)
            if doc:
                lines.extend(doc.split("\n"))
    return lines


def test_no_prose_still_calls_ranking_permissive_or_subjectless():
    """Five explanations still stated the pre-reclassification tier in prose.

    The exact frozen map moved the kind to the subject-bound tier while the
    prose beside it kept explaining the wrapper binding as "a known kind, in
    the permissive tier". A reader trusting the explanation would conclude such
    an envelope may name nobody, which every layer rejects. The rationale is
    the type mismatch now, and this scan keeps the old one from coming back.
    """
    for known in (
        "docs/contracts/receipt.md",
        "docs/contracts/tenant.md",
        "curator/contracts/__init__.py",
        "curator/ownership.py",
        "curator/ledger/ownership.py",
        "tests/test_contract_freeze.py",
    ):
        assert known in _PROSE_SCAN_FILES, known
    assert len(_PROSE_SCAN_FILES) > 7, "the derived list must be wider than the old literal one"

    offenders = []
    for path in _PROSE_SCAN_FILES:
        blob = " ".join(_prose_lines(path))
        for sentence in re.split(r"(?<=[.!?])\s+", blob):
            low = sentence.lower()
            if "ranking" in low and ("permissive" in low or "subjectless" in low):
                offenders.append(f"{path}: {sentence.strip()[:120]}")
    assert offenders == [], offenders


def test_the_subjectless_prose_scan_is_not_vacuous():
    """The stale-string scan must also prove the CURRENT sentences are there.

    ``test_no_doc_field_table_states_the_subjectless_rule_for_a_subject_bound_record``
    looks for a phrase that appears zero times anywhere, including on records
    that are legitimately subjectless, so on its own it is a guard against one
    historical string rather than against the class of error.
    """
    docs = sorted((REPO_ROOT / "docs" / "contracts").glob("*.md"))
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in docs)
    expected = _expected_ownership_doc_sentence(contracts.SourceCheckpoint)
    assert corpus.count(expected) == len(SUBJECTLESS_RECORDS), (
        f"expected {len(SUBJECTLESS_RECORDS)} derived subjectless rows, "
        f"found {corpus.count(expected)}"
    )
    subject = _expected_ownership_doc_sentence(contracts.LearningEvent)
    assert corpus.count(subject) == len(SUBJECT_BOUND_RECORDS)


def test_no_narrative_sentence_contradicts_a_records_frozen_tier():
    """The derived row test reads TABLE ROWS only, so prose could still drift.

    ``_OWNERSHIP_DOC_ROW`` anchors on ``| `tenant_id` ``, so a second sentence
    in the same section's narrative could state the opposite tier and stay
    invisible. This scans the narrative half of every record's section for a
    sentence that names the record and asserts the wrong tier.
    """
    subject_bound = set(SUBJECT_BOUND_RECORDS)
    offenders = []
    for cls in OWNED_RECORDS:
        doc = REPO_ROOT / _DOC_BY_MODULE[cls.__module__]
        lines = doc.read_text(encoding="utf-8").split("\n")
        sections = [s for s in _doc_sections(lines) if cls.__name__ in s[0]]
        if len(sections) != 1:
            continue
        _, start, end = sections[0]
        narrative = " ".join(
            lines[i] for i in range(start, end) if not lines[i].lstrip().startswith("|")
        )
        wrong = "SUBJECTLESS" if cls in subject_bound else "SUBJECT-BOUND"
        for sentence in re.split(r"(?<=[.!?])\s+", narrative):
            if cls.__name__ in sentence and wrong in sentence.upper():
                offenders.append(f"{doc.name}: {sentence.strip()[:120]}")
    assert offenders == [], offenders


def test_curator_sources_does_not_import_curator_ledger():
    """Layering: the shared rule is not ledger-specific and must not import it.

    ``curator/sources/checkpoint.py`` used to reach sideways into
    ``curator.ledger.ownership``, which executed ``curator/ledger/__init__.py``
    and pulled the base and in-memory stores into a source adapter's process.
    There was no cycle, and nothing kept it that way.
    """
    import subprocess
    import sys

    probe = (
        "import curator.sources, curator.sources.checkpoint, sys; "
        "print(sorted(m for m in sys.modules if m.startswith('curator.ledger')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout
