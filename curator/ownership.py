"""The shared ownership rule: pure functions over the frozen contract tuples.

Why this module is dependency-free and sits at the top of ``curator``: three
different domain packages need the rule (the ledger write paths, the source
checkpoint store, and the contract-freeze corpus). It used to live in
``curator/ledger/ownership.py``, which made ``curator.sources`` import
``curator.ledger`` (and, through that package's ``__init__``, its base and
in-memory store) just to ask whether a checkpoint names a human. There was no
cycle, but the direction was wrong and nothing kept it from becoming one.
``curator/ledger/ownership.py`` re-exports everything here, so existing imports
keep working; ``test_curator_sources_does_not_import_curator_ledger`` fails if
the sideways import comes back.

Why the rule is not a ``__post_init__`` on ``Ownership``: every contract module
is DECLARATIVE ONLY (no behavior, no I/O), so validation inside the frozen
package would be behavior. The rule stays declarative data in
``curator.contracts`` (the classification tuples and the invisible-code-point
ranges) and this module is the pure function that reads it.
``tests/test_contract_freeze.py`` implements the same rule over JSON payloads
for the fields that are genuinely mirrored (blank rule, tier resolution, kind
vocabulary, nested-tenant rule), and a test runs both over the whole fixture
corpus and compares verdicts. The canonical-id half (padding, invisible
characters, surrogates, NFC) is SHARED by construction: the fixture side calls
``noncanonical_id_reason`` from this module directly rather than
re-implementing it, so "two validators that cannot drift" is true of the
mirrored half and true by sharing, not by duplication, of the canonical half.

Pure: no I/O, no state, no exceptions raised by ``ownership_violations``.
Callers decide what a violation means. Every write path in the ledger and the
checkpoint store calls it, which is what stops a record with blank or
non-canonical ownership from ever being stored.
"""

from __future__ import annotations

import dataclasses
import unicodedata

from curator.contracts import (
    INVISIBLE_ID_CODE_POINT_RANGES,
    INVISIBLE_ID_CODE_POINTS,
    KIND_BOUND_RECORDS,
    OWNED_RECORDS,
    RECEIPT_WRAPPER_KINDS,
    SUBJECT_BOUND_RECEIPT_KINDS,
    SUBJECT_BOUND_RECORDS,
    SUBJECTLESS_RECEIPT_KINDS,
    SUBJECTLESS_RECORDS,
)
from curator.contracts.enums import ActorKind
from curator.contracts.receipt import ReceiptEnvelope
from curator.contracts.tenant import Actor, Ownership, Tenant, TenantMembership, User

_SUBJECT_BOUND = tuple(SUBJECT_BOUND_RECORDS)
_SUBJECTLESS = tuple(SUBJECTLESS_RECORDS)
_KIND_BOUND = tuple(KIND_BOUND_RECORDS)
_OWNED = tuple(OWNED_RECORDS)


def _is_exact_class(cls: type, frozen: tuple[type, ...]) -> bool:
    return any(cls is frozen_class for frozen_class in frozen)


def _wrapper_spec(cls: type) -> tuple[str, str] | None:
    for wrapper_class, field_name, kind in RECEIPT_WRAPPER_KINDS:
        if cls is wrapper_class:
            return field_name, kind
    return None


def _inherits_frozen_wrapper(cls: type) -> bool:
    return any(
        wrapper_class in cls.__mro__[1:]
        for wrapper_class, _, _ in RECEIPT_WRAPPER_KINDS
    )


# ---------------------------------------------------------------------------
# Canonical ownership ids
# ---------------------------------------------------------------------------


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def noncanonical_id_reason(value: str) -> str | None:
    """Why ``value`` is not a canonical ownership id, or ``None`` if it is.

    Non-blank was never enough. ``" user-1 "`` and ``"user-1"`` are two
    encodings of one subject, so a per-person delete keyed on the canonical
    spelling misses the padded row exactly as a sweep on ``user_id is null``
    used to miss the blank-string rows. The same argument covers invisible
    characters: ``"user\\u200b1"`` renders identically to ``"user1"`` and no
    human reviewing a deletion could tell the two rows apart.

    A canonical id is non-empty, equal to its stripped value, carries at least
    one non-whitespace character (that is ``_blank``), contains no code point in
    ``INVISIBLE_ID_CODE_POINTS`` (every ``Zs``, ``Zl``, ``Zp``, ``Cc`` and
    ``Cf`` character, so U+0020, U+00A0, U+2028, U+2029, U+3000, U+200B-U+200D,
    U+2060 and U+FEFF are all refused), carries no unpaired surrogate, and is in
    Unicode normalization form NFC. ASCII space is in ``Zs``, so an id with an
    INTERNAL space is refused too. That is deliberate: an identifier is a key,
    not a label, and "two ids that differ only in spacing" is the same ambiguity
    one step in.

    NFC is the same argument in the encoding dimension. ``"jos\u00e9-1"`` (6
    code points) and ``"jose\u0301-1"`` (7) render identically, so they are two
    encodings of one subject and a per-person delete keyed on the composed
    spelling misses the decomposed row. This product has Chinese input paths and
    macOS filesystem APIs hand back decomposed strings, so an id sourced from a
    filename or a clipboard paste arrives decomposed through the same door the
    U+3000 case arrives through. Both layers enforce it: the SQL half is
    ``x is nfc normalized``.

    Unpaired surrogates (U+D800-U+DFFF, category ``Cs``) are refused here and
    NOT in the SQL class, because a UTF-8 Postgres database cannot store one at
    all: the write fails with an encoding error instead of a check violation.
    This is the one deliberate asymmetry between the layers and it points the
    safe way (Python is stricter than a value the database cannot hold).

    Consequences worth naming, all of them fallout from "an identifier is a key,
    not a label": an emoji ZWJ sequence is refused (U+200D is ``Cf``), an emoji
    flag TAG sequence is refused (U+E0020-U+E007F are ``Cf``), and a display
    name carrying a bidi mark (U+200E, U+200F) is refused. Plain CJK, kana,
    hangul, Arabic, combining marks, dashes, underscores, uuids and single
    emoji with or without U+FE0F are all ACCEPTED.

    What this refuses when its assumption is wrong: a caller that genuinely
    wants a human-readable name with spaces must carry it in a display field
    and key on a canonical id, and a caller holding a decomposed id from an
    external system must normalize it. The failure is loud, arrives at the first
    write, and is fixed in the caller rather than silently splitting a subject's
    rows.
    """
    if value != value.strip():
        return "must not carry leading or trailing whitespace"
    offenders = sorted({ord(char) for char in value} & INVISIBLE_ID_CODE_POINTS)
    if offenders:
        rendered = ", ".join(f"U+{code:04X}" for code in offenders)
        return f"must not contain invisible characters, found {rendered}"
    surrogates = sorted({ord(c) for c in value if 0xD800 <= ord(c) <= 0xDFFF})
    if surrogates:
        rendered = ", ".join(f"U+{code:04X}" for code in surrogates)
        return f"must not contain unpaired surrogates, found {rendered}"
    if value != unicodedata.normalize("NFC", value):
        return (
            "must be in Unicode normalization form NFC; this value is a second "
            "encoding of an id that already exists"
        )
    return None


def _id_violations(value: object, field: str, blank_message: str) -> tuple[str, ...]:
    """The blank check and the canonical check for one ownership id."""
    if _blank(value):
        return (blank_message,)
    assert isinstance(value, str)  # _blank already rejected every non-str
    reason = noncanonical_id_reason(value)
    if reason is None:
        return ()
    return (f"{field} {reason}",)


def _sql_code_point(code: int) -> str:
    """One code point as a Postgres ARE regex escape (``\\uXXXX``/``\\UXXXXXXXX``)."""
    return f"\\u{code:04X}" if code <= 0xFFFF else f"\\U{code:08X}"


#: The invisible set rendered as a Postgres regex bracket expression. Derived
#: from the same frozen ranges the Python predicate reads, so the two layers
#: cannot list different characters.
INVISIBLE_ID_SQL_CLASS: str = "[" + "".join(
    _sql_code_point(first) if first == last
    else f"{_sql_code_point(first)}-{_sql_code_point(last)}"
    for first, last in INVISIBLE_ID_CODE_POINT_RANGES
) + "]"


def ownership_id_sql_check(column: str) -> str:
    """The CHECK constraint text every ownership column in the schema carries.

    ``btrim(x) <> ''`` was the old form and it strips SPACES ONLY, so tab,
    newline, U+00A0 and U+3000 all passed the database while both Python
    validators rejected them: a real INSERT of an ideographic-space ``user_id``
    was accepted, and ``where user_id is null`` did not find it. The three
    clauses here are whitespace-complete: equal to its own ``btrim``, at least
    one non-space character, and no invisible code point. A fourth clause,
    ``is nfc normalized``, is the SQL half of the NFC rule in
    ``noncanonical_id_reason``: it needs Postgres 13 or later on a UTF8
    database and no extension.

    The migration's text is generated from this function, and
    ``test_the_migration_ownership_checks_are_derived_from_the_frozen_set``
    fails if the file drifts from it.
    """
    return (
        f"check ({column} = btrim({column}) and {column} ~ '[^[:space:]]' "
        f"and {column} !~ '{INVISIBLE_ID_SQL_CLASS}' "
        f"and {column} is nfc normalized)"
    )


# ---------------------------------------------------------------------------
# Tier resolution and receipt wrappers
# ---------------------------------------------------------------------------


def is_subject_bound(record: object) -> bool:
    """True when a per-person delete must be able to find this row by user_id.

    Raises ``KeyError`` for an owned class in none of the three tuples, so an
    unclassified record fails loudly at its first write rather than defaulting
    to the permissive tier.
    """
    record_type = type(record)
    if _is_exact_class(record_type, _SUBJECT_BOUND):
        return True
    if _is_exact_class(record_type, _SUBJECTLESS):
        return False
    if _is_exact_class(record_type, _KIND_BOUND):
        kind = getattr(record, "kind", None)
        if kind in SUBJECT_BOUND_RECEIPT_KINDS:
            return True
        if kind in SUBJECTLESS_RECEIPT_KINDS:
            return False
        raise KeyError(
            f"{record_type.__name__}: receipt kind {kind!r} is in neither "
            "SUBJECT_BOUND_RECEIPT_KINDS nor SUBJECTLESS_RECEIPT_KINDS"
        )
    raise KeyError(
        f"{record_type.__name__} inherits Ownership but is classified in neither "
        "SUBJECT_BOUND_RECORDS, SUBJECTLESS_RECORDS, nor KIND_BOUND_RECORDS"
    )


def is_receipt_wrapper(record: object) -> bool:
    """True only for an exact class in the frozen wrapper tuple."""
    return _wrapper_spec(type(record)) is not None


def receipt_wrapper_violations(record: object) -> tuple[str, ...]:
    """A receipt wrapper may carry only the one envelope kind it is named for.

    ``DeletionReceipt`` used to accept an envelope stamped ``ranking``. That is
    a TYPE MISMATCH, not a tier hole: both kinds are subject-bound and both
    demand a non-blank ``user_id``, so every ownership check passed while the
    store recorded a receipt whose type said it proved a deletion and whose
    envelope said it explained a slate order. No ownership rule can catch that;
    only this binding can.

    Runtime classification is closed over exact class objects. The frozen map
    also names the one envelope field, so runtime code never interprets type
    annotations. A subclass is an unknown record, not a wrapper. New wrappers
    are added to the frozen tuples and validated statically before use.
    """
    record_type = type(record)
    spec = _wrapper_spec(record_type)
    if spec is None:
        if _inherits_frozen_wrapper(record_type):
            return (
                f"unknown record type {record_type.__name__}; frozen contracts "
                "are never subclassed; add a new record to the frozen tuples",
            )
        return ()
    field_name, expected = spec
    envelope = getattr(record, field_name, None)
    if type(envelope) is not ReceiptEnvelope:
        return (
            f"{record_type.__name__}.{field_name} must be exactly ReceiptEnvelope; "
            f"got {type(envelope).__name__}",
        )
    kind = envelope.kind
    if kind == expected:
        return ()
    return (
        f"{type(record).__name__} requires an envelope of kind {expected!r}, "
        f"got {kind!r}",
    )


def ownership_violations(record: object) -> tuple[str, ...]:
    """Every way ``record``'s ownership shape is wrong, in a stable order.

    Empty tuple means the record may be stored. A record that does not inherit
    ``Ownership`` is not an owned record, so only the receipt-wrapper binding
    below applies to it.
    """
    problems: list[str] = list(receipt_wrapper_violations(record))

    # A wrapper owns THROUGH its envelope: the wrapper itself does not inherit
    # ``Ownership``, so without this recursion the generic guard returned clean
    # for a DeletionReceipt whose envelope named no human at all. The deletion
    # store happened to check the envelope separately; nothing made the next
    # wrapper's author do the same. The field comes from the frozen wrapper
    # map, never from runtime annotation interpretation.
    wrapper_spec = _wrapper_spec(type(record))
    if wrapper_spec is not None:
        envelope = getattr(record, wrapper_spec[0], None)
        if type(envelope) is ReceiptEnvelope:
            problems.extend(
                f"envelope: {problem}" for problem in ownership_violations(envelope)
            )

    if not isinstance(record, Ownership):
        return tuple(problems)

    if not _is_exact_class(type(record), _OWNED):
        problems.extend(
            (
                f"unknown record type {type(record).__name__}; add the exact class "
                "object to OWNED_RECORDS and one ownership tier",
            )
        )
        return tuple(problems)

    problems.extend(
        _id_violations(
            getattr(record, "tenant_id", None),
            "tenant_id",
            "every private record requires a non-blank tenant_id",
        )
    )
    problems.extend(
        _id_violations(
            getattr(record, "actor_id", None),
            "actor_id",
            "every private record requires a non-blank actor_id",
        )
    )

    kind = getattr(record, "actor_kind", None)
    if not isinstance(kind, ActorKind):
        problems.append(f"actor_kind {kind!r} is not a member of ActorKind")

    # Resolved for EVERY owned record, before anything branches on user_id.
    # Resolving it only on the null-user_id branch made an unclassified record
    # (a receipt whose kind is a typo) legal as long as some non-blank user_id
    # was present: the tier that would have rejected it was never consulted.
    # An unknown classification is a violation on its own terms now.
    try:
        subject_bound: bool | None = is_subject_bound(record)
    except KeyError as exc:
        problems.append(str(exc))
        subject_bound = None

    user_id = getattr(record, "user_id", None)
    if user_id is not None:
        # Three encodings of "no human" (null, "", "   ") where the contract
        # says there is one. A deletion sweep filtering `user_id is null`
        # silently misses the blank-string rows, and a sweep on the canonical
        # spelling misses the padded and zero-width variants.
        problems.extend(
            _id_violations(
                user_id,
                "user_id",
                "user_id must be a non-blank id or null, never blank",
            )
        )
    else:
        if subject_bound is True:
            problems.append(
                "this record is about a person, so user_id is required "
                "non-blank whatever wrote it"
            )
        elif subject_bound is False and kind is not ActorKind.SYSTEM:
            problems.append(
                "a human or agent actor requires a non-blank user_id; only "
                "a system actor may act for no human"
            )

    problems.extend(_nested_tenant_violations(record))
    return tuple(problems)


#: Field, per identity class, whose value is required non-blank and canonical
#: whenever it is not ``None``. Separate from ``ownership_violations`` because
#: none of these four classes inherits ``Ownership``: ``Tenant``, ``User`` and
#: ``TenantMembership`` are not owned records at all, and ``Actor`` is
#: deliberately exempt (it is the identity record an owned row's ``actor_id``
#: POINTS AT, not an owned row itself). But every id these classes carry is the
#: same id an owned record's ``tenant_id``/``actor_id``/``user_id`` names, so a
#: per-person delete keyed on the canonical spelling of a subject misses that
#: subject's own row if the identity record naming it is allowed to be padded,
#: carry an invisible character, or sit in NFD. A widening of
#: ``ownership_violations`` was rejected because there is no ``Ownership``
#: instance on any of these four classes to widen the check on; a parallel
#: function reading the same ``_id_violations``/``noncanonical_id_reason``
#: primitives keeps the one rule in one place instead.
_IDENTITY_ID_FIELDS: dict[type, tuple[str, ...]] = {
    Tenant: ("tenant_id",),
    User: ("user_id", "tenant_id"),
    Actor: ("actor_id", "tenant_id", "user_id"),
    TenantMembership: ("membership_id", "tenant_id", "principal_id", "actor_id"),
}


def identity_violations(record: object) -> tuple[str, ...]:
    """Every canonical-id problem on an IDENTITY record (not an owned record).

    ``Tenant.tenant_id``; ``User.user_id`` and ``tenant_id``; ``Actor.actor_id``,
    ``tenant_id``, and (when present) ``user_id``; ``TenantMembership
    .membership_id``, ``tenant_id``, ``principal_id`` and ``actor_id`` must all
    satisfy the same canonical predicate as an owned record's ids: non-empty,
    equal to its own stripped value, no frozen invisible code point, and in
    Unicode normalization form NFC. Before this function existed, a `Tenant`,
    `User`, `Actor` or `TenantMembership` row could carry a padded, zero-width,
    or NFD-encoded id and every write path agreed it was fine, because
    ``ownership_violations`` only runs on classes that inherit ``Ownership``,
    which none of these four do. The delete argument is the module's own
    (``noncanonical_id_reason``): two spellings that render identically are two
    encodings of one subject, and if the identity row THAT NAMES the subject
    can itself be non-canonical, the key an owned-record sweep uses to find it
    is already ambiguous before the sweep runs.

    ``Actor.user_id`` is checked only when present (it is legitimately ``None``
    for a ``system`` actor); the null-vs-required business rule for that case
    lives beside the other business rules, in the fixture invariant, not here.
    This function is the canonical-id predicate alone, mirroring the scope
    ``ownership_violations`` already keeps for owned records.
    """
    fields = _IDENTITY_ID_FIELDS.get(type(record))
    if fields is None:
        return ()
    problems: list[str] = []
    for field in fields:
        value = getattr(record, field, None)
        if field == "user_id" and value is None:
            continue
        problems.extend(
            _id_violations(
                value,
                field,
                f"{type(record).__name__}.{field} is required non-blank",
            )
        )
    return tuple(problems)


def _nested_tenant_violations(record: Ownership) -> tuple[str, ...]:
    """A nested key that carries its own tenant must name the SAME tenant.

    ``PublicationAuthorization`` and ``PublicationRecord`` hold a
    ``PublicationIdentity`` whose ``tenant_id`` is part of the at-most-once
    key. If the two could disagree, a member of tenant A could authorize a
    publication whose identity belongs to tenant B.
    """
    problems: list[str] = []
    for field in dataclasses.fields(record):
        value = getattr(record, field.name, None)
        if not dataclasses.is_dataclass(value) or isinstance(value, type):
            continue
        nested_tenant = getattr(value, "tenant_id", None)
        if nested_tenant is None:
            continue
        if nested_tenant != record.tenant_id:
            problems.append(
                f"{field.name}.tenant_id {nested_tenant!r} must equal the "
                f"record's tenant_id {record.tenant_id!r}"
            )
    return tuple(problems)
