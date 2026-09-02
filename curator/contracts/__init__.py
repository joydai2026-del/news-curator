"""Frozen phase-1 contract definitions for News Curator.

Twelve contracts: tenant, authorization, source plugin, evidence, event, search,
candidate, artifact, mirror, output adapter, publication, receipt.

This package is DECLARATIVE ONLY. It holds dataclasses, Protocols, and Enums.
It performs no validation, no persistence, and no I/O, and it imports nothing
from the running pipeline, so a contract can be read and frozen without pulling
in behavior that might drift under it.

The prose freeze, with field constraints, invariants, state machines, and the
plan sections each one freezes, is in ``docs/contracts/``.
"""

from __future__ import annotations

from .artifact import ArtifactRelation, ArtifactVersion, KnowledgeArtifact
from .authorization import (
    ACTION_MATRIX,
    ActionRequirement,
    AuthorizationAudit,
    PrincipalClaims,
)
from .candidate import (
    BandResult,
    ComponentScores,
    LaneCandidate,
    MergedCandidate,
    ScoredCandidate,
    Slate,
    SlateEntry,
    StoryRecord,
)
from .enums import (
    ActorKind,
    ArtifactStatus,
    ArtifactType,
    BandVerdict,
    CheckpointState,
    ConfidenceBand,
    CorrectionAction,
    Decision,
    EvidenceClass,
    EvidenceOrigin,
    EventType,
    HealthStatus,
    Lane,
    MirrorState,
    PluginState,
    PublicationClass,
    PublicationState,
    ReadbackVerdict,
    ReceiptState,
    RetentionState,
    ScorerKind,
    Scope,
    SearchOutcome,
    SearchResultClass,
    WriteMode,
)
from .event import CorrectionEvent, EventSemantics, LearningEvent
from .evidence import EvidenceItem, ProfileSnapshot, RawImport
from .mirror import (
    MIRROR_TERMINAL_STATES,
    MIRROR_TRANSITIONS,
    MirrorAdapterDescriptor,
    MirrorReceipt,
)
from .output_adapter import DryRunResult, OutputAdapterDescriptor, OutputReceipt
from .publication import (
    PUBLICATION_TERMINAL_STATES,
    PUBLICATION_TRANSITIONS,
    PublicationAuthorization,
    PublicationIdentity,
    PublicationRecord,
    PublishingBasket,
)
from .receipt import (
    DeletionReceipt,
    ImportInventoryReceipt,
    LimitReceipt,
    MeterReading,
    ProjectionResolution,
    RankingReceipt,
    ReceiptEnvelope,
)
from .search import SearchQuery, SearchResponse, SearchResult
from .source_plugin import (
    NormalizedSourceDocument,
    SourceCapabilities,
    SourceCheckpoint,
    SourceHealthRecord,
    SourcePlugin,
    SourcePluginRegistration,
    SourceProvenance,
    SourceRights,
)
from .tenant import Actor, Ownership, Tenant, TenantMembership, User

#: Every private record: the dataclasses that INHERIT ``Ownership`` and so
#: carry all four of tenant_id, actor_id, actor_kind, user_id as required
#: fields. Listed here so the gate test has one place to read the intended set
#: from, and so a reader can see at a glance what "private record" means.
#:
#: The set is DERIVED, not hand-maintained: a class earns membership by
#: subclassing ``Ownership``, and ``tests/test_contract_freeze.py`` fails if a
#: contract dataclass carries a tenant_id or an actor_id without either
#: subclassing ``Ownership`` or appearing in that test's exempt map with a
#: reason. Adding a private record and forgetting the ownership shape is
#: therefore a test failure, not a silent omission.
OWNED_RECORDS: tuple[type, ...] = (
    ArtifactRelation,
    ArtifactVersion,
    AuthorizationAudit,
    CorrectionEvent,
    EvidenceItem,
    KnowledgeArtifact,
    LaneCandidate,
    LearningEvent,
    MergedCandidate,
    MirrorReceipt,
    NormalizedSourceDocument,
    OutputReceipt,
    ProfileSnapshot,
    PublicationAuthorization,
    PublicationRecord,
    PublishingBasket,
    RawImport,
    ReceiptEnvelope,
    Slate,
    SourceCheckpoint,
    SourcePluginRegistration,
    StoryRecord,
)

#: Two-tier subject attribution. Every owned record answers ONE question:
#: **must a "delete everything about me" request find and delete this row?**
#:
#: - Yes  -> the row is SUBJECT-BOUND and ``user_id`` is REQUIRED non-blank,
#:   whatever wrote it. A system writer does not erase the human subject: a
#:   profile snapshot computed by the normalizer is still about one person.
#: - No   -> the row is SUBJECTLESS and ``user_id`` may be null, and then ONLY
#:   when ``actor_kind`` is ``system``. A human or agent actor always names the
#:   human it acts for, on every record.
#:
#: Decided by recoverability: a missing attribution is unrecoverable after the
#: fact (the row can no longer be found for a per-person delete), while a
#: redundant one costs a column. That reasoning is written here on purpose so a
#: later review round cannot silently flip a class back without answering it.
#:
#: Declarative data, not behavior. ``curator.ledger.ownership`` reads these
#: tuples at runtime and ``tests/test_contract_freeze.py`` reads them at freeze
#: time, so the two validators cannot drift.
SUBJECT_BOUND_RECORDS: tuple[type, ...] = (
    ArtifactRelation,
    ArtifactVersion,
    AuthorizationAudit,
    CorrectionEvent,
    EvidenceItem,
    KnowledgeArtifact,
    LearningEvent,
    MirrorReceipt,
    OutputReceipt,
    ProfileSnapshot,
    PublicationAuthorization,
    PublicationRecord,
    PublishingBasket,
    RawImport,
    Slate,
)

#: Owned records whose row is NOT about a person: infrastructure state and
#: pipeline intermediates that are reachable by join from a subject-bound
#: parent, so a per-person delete finds them through that parent rather than
#: through their own ``user_id``.
SUBJECTLESS_RECORDS: tuple[type, ...] = (
    LaneCandidate,
    MergedCandidate,
    NormalizedSourceDocument,
    SourceCheckpoint,
    SourcePluginRegistration,
    StoryRecord,
)

#: Owned records classified by a FIELD rather than by their class, because one
#: class carries rows of both kinds. ``ReceiptEnvelope`` is the only one.
KIND_BOUND_RECORDS: tuple[type, ...] = (ReceiptEnvelope,)

#: The FROZEN receipt-kind vocabulary: every wire value a
#: ``ReceiptEnvelope.kind`` may carry, each mapped to its subject tier. There
#: is no other legal kind, and an envelope carrying one that is not listed here
#: is a violation whatever else it looks like.
#:
#: Written as one closed list rather than two open ones because the failure it
#: closes is a TYPO, not a disagreement: ``kind="rankng"`` used to be accepted
#: by both validators whenever ``user_id`` happened to be non-blank, because
#: the tier was only consulted on the null-user_id branch. The vocabulary is
#: consulted for every owned record now, so an unknown kind fails CLOSED.
#:
#: - ``subject_bound``: a per-person delete must find this receipt. A deletion
#:   receipt proves one human's deletion; an import inventory enumerates one
#:   human's imported archive; a ranking receipt explains the order of ONE
#:   person's slate and cites the profile snapshot it ranked against.
#: - ``subjectless``: the receipt is about the system, so ``user_id`` may be
#:   null, and then only for a ``system`` actor. ``host_limits`` is the limit
#:   receipt's wire value and the only subjectless kind: a host budget is a
#:   property of the machine, not of a reader.
#:
#: ``ranking`` was reclassified on 2026-09-02, with ``Slate``, and is now
#: subject_bound, for the reason recorded on ``Slate`` below.
RECEIPT_KIND_TIERS: tuple[tuple[str, str], ...] = (
    ("deletion", "subject_bound"),
    ("host_limits", "subjectless"),
    ("import_inventory", "subject_bound"),
    ("ranking", "subject_bound"),
)

#: Envelope ``kind`` values whose receipt is about a person. Derived from
#: ``RECEIPT_KIND_TIERS`` so the vocabulary has exactly one definition.
SUBJECT_BOUND_RECEIPT_KINDS: tuple[str, ...] = tuple(
    kind for kind, tier in RECEIPT_KIND_TIERS if tier == "subject_bound"
)

#: Envelope ``kind`` values whose receipt is about the system.
SUBJECTLESS_RECEIPT_KINDS: tuple[str, ...] = tuple(
    kind for kind, tier in RECEIPT_KIND_TIERS if tier == "subjectless"
)

#: Each receipt WRAPPER pins the frozen field that carries its envelope and the
#: one envelope kind it may carry. The class objects are retained here so every
#: runtime classification uses exact identity rather than a forgeable name.
#: Frozen contracts are never subclassed; a new wrapper is added to this tuple.
#: Without this
#: binding a ``DeletionReceipt`` could ship an envelope stamped ``ranking``:
#: a TYPE MISMATCH, not a tier question. Both kinds are subject-bound and both
#: demand a non-blank ``user_id``, so every ownership check passes while the
#: receipt's type says it proves a deletion and its envelope says it explains a
#: slate order. Ownership can never catch this; only the binding can.
#: Enforced by the fixture invariant, by ``ownership_violations``, and by the
#: ledger write path, so no single one of the three is the only guard.
RECEIPT_WRAPPER_KINDS: tuple[tuple[type, str, str], ...] = (
    (DeletionReceipt, "envelope", "deletion"),
    (ImportInventoryReceipt, "envelope", "import_inventory"),
    (LimitReceipt, "envelope", "host_limits"),
    (RankingReceipt, "envelope", "ranking"),
)

#: The INVISIBLE code points an ownership id may never contain, frozen as
#: RANGES so the Python predicate and the SQL check are two renderings of one
#: list rather than two hand-kept lists.
#:
#: The set is every code point Python's ``unicodedata`` classifies ``Zs``
#: (space separators, including U+00A0 and U+3000), ``Zl`` (U+2028 LINE
#: SEPARATOR), ``Zp`` (U+2029 PARAGRAPH SEPARATOR), ``Cc`` (control characters,
#: including tab and newline) or ``Cf`` (format characters, including U+200B,
#: U+200C, U+200D, U+2060 and U+FEFF). ``Zl`` and ``Zp`` joined on 2026-09-02:
#: the set was Zs/Cc/Cf only, and Python's ``str.strip()`` removes U+2028 and
#: U+2029 while Postgres ``btrim`` does not, so ``"user-1\u2028"`` was REJECTED
#: by the Python predicate (it is not equal to its own strip) and ACCEPTED by
#: the generated SQL check. Two layers disagreeing about one id is the exact
#: split-subject hazard this set exists to close. Written out here rather than computed
#: from ``unicodedata`` at import time because a migration's CHECK constraint
#: is frozen text: if the running Python's Unicode tables grew and the constant
#: grew with them, the database would silently accept what Python rejects.
#: ``test_the_frozen_invisible_set_still_covers_every_unicode_invisible``
#: fails when the two diverge, so a Unicode upgrade is a deliberate edit here.
#:
#: Why these block an id at all: two ids that render identically are two
#: encodings of one subject. A per-person delete keyed on the visible spelling
#: misses the row that carries a zero-width joiner, exactly as a sweep on
#: ``user_id is null`` used to miss the blank-string rows.
INVISIBLE_ID_CODE_POINT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0020),
    (0x007F, 0x00A0),
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x1680, 0x1680),
    (0x180E, 0x180E),
    (0x2000, 0x200F),
    (0x2028, 0x202F),
    (0x205F, 0x2064),
    (0x2066, 0x206F),
    (0x3000, 0x3000),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x13438),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

#: The same set flattened, for the Python predicate. Derived, never typed.
INVISIBLE_ID_CODE_POINTS: frozenset[int] = frozenset(
    code_point
    for first, last in INVISIBLE_ID_CODE_POINT_RANGES
    for code_point in range(first, last + 1)
)

#: Why each owned record sits in the tier it sits in. Required for every owned
#: class, so a new record cannot land unclassified: the freeze test asserts the
#: three tuples partition ``OWNED_RECORDS`` exactly and that every entry has a
#: written reason.
OWNERSHIP_CLASSIFICATION_REASONS: tuple[tuple[str, str], ...] = (
    ("ArtifactRelation", "an edge between two of one human's artifacts; a per-person delete must remove it"),
    ("ArtifactVersion", "a revision of one human's artifact"),
    ("AuthorizationAudit", "the record that one human's action was allowed or denied"),
    ("CorrectionEvent", "one human's correction of their own signal"),
    ("EvidenceItem", "evidence derived from one human's behavior, whoever normalized it"),
    ("KnowledgeArtifact", "one human's artifact"),
    ("LaneCandidate", "a pipeline intermediate; reachable from the slate it feeds"),
    ("LearningEvent", "one human's behavioral signal"),
    ("MergedCandidate", "a pipeline intermediate; reachable from the slate it feeds"),
    ("MirrorReceipt", "proof of an external copy of one human's artifact; a delete must be provably complete over it"),
    ("NormalizedSourceDocument", "a public document fetched from a source; about nobody"),
    ("OutputReceipt", "proof that one human's edition reached a destination"),
    ("ProfileSnapshot", "a model of one human's interests, whoever computed it"),
    ("PublicationAuthorization", "one human's approval of a publication"),
    ("PublicationRecord", "the state of one human's publication"),
    ("PublishingBasket", "one human's edition, assembled for publication"),
    ("RawImport", "one human's imported archive"),
    ("ReceiptEnvelope", "classified by kind: deletion, import_inventory and ranking are about a person, host_limits is about the machine"),
    ("Slate", "one person's ranked edition, not a run intermediate: it cites the profile snapshot it was personalized against, and its only link to a human (profile_snapshot_id) is NULLABLE, so a cold-start slate would name nobody and nothing would name it. LaneCandidate, MergedCandidate and StoryRecord stay subjectless because they are per-RUN inputs that exist before any personalization; a per-user candidate store would move them too"),
    ("SourceCheckpoint", "a source's fetch cursor; infrastructure state about a feed, not a person"),
    ("SourcePluginRegistration", "a plugin registry row; infrastructure state"),
    ("StoryRecord", "a pipeline intermediate; reachable from the slate it feeds"),
)

#: The twelve frozen contracts, in the order SC-41 names them, mapped to the
#: module that defines them and the prose file that freezes them.
FROZEN_CONTRACTS: tuple[tuple[str, str, str], ...] = (
    ("tenant", "curator.contracts.tenant", "docs/contracts/tenant.md"),
    ("authorization", "curator.contracts.authorization", "docs/contracts/authorization.md"),
    ("source-plugin", "curator.contracts.source_plugin", "docs/contracts/source-plugin.md"),
    ("evidence", "curator.contracts.evidence", "docs/contracts/evidence.md"),
    ("event", "curator.contracts.event", "docs/contracts/event.md"),
    ("search", "curator.contracts.search", "docs/contracts/search.md"),
    ("candidate", "curator.contracts.candidate", "docs/contracts/candidate.md"),
    ("artifact", "curator.contracts.artifact", "docs/contracts/artifact.md"),
    ("mirror", "curator.contracts.mirror", "docs/contracts/mirror.md"),
    ("output-adapter", "curator.contracts.output_adapter", "docs/contracts/output-adapter.md"),
    ("publication", "curator.contracts.publication", "docs/contracts/publication.md"),
    ("receipt", "curator.contracts.receipt", "docs/contracts/receipt.md"),
)

__all__ = [
    "ACTION_MATRIX",
    "FROZEN_CONTRACTS",
    "INVISIBLE_ID_CODE_POINTS",
    "INVISIBLE_ID_CODE_POINT_RANGES",
    "KIND_BOUND_RECORDS",
    "MIRROR_TERMINAL_STATES",
    "MIRROR_TRANSITIONS",
    "OWNED_RECORDS",
    "OWNERSHIP_CLASSIFICATION_REASONS",
    "PUBLICATION_TERMINAL_STATES",
    "PUBLICATION_TRANSITIONS",
    "RECEIPT_KIND_TIERS",
    "RECEIPT_WRAPPER_KINDS",
    "SUBJECTLESS_RECEIPT_KINDS",
    "SUBJECTLESS_RECORDS",
    "SUBJECT_BOUND_RECEIPT_KINDS",
    "SUBJECT_BOUND_RECORDS",
    "ActionRequirement",
    "Actor",
    "ActorKind",
    "ArtifactRelation",
    "ArtifactStatus",
    "ArtifactType",
    "ArtifactVersion",
    "AuthorizationAudit",
    "BandResult",
    "BandVerdict",
    "CheckpointState",
    "ComponentScores",
    "ConfidenceBand",
    "CorrectionAction",
    "CorrectionEvent",
    "Decision",
    "DeletionReceipt",
    "DryRunResult",
    "EventSemantics",
    "EventType",
    "EvidenceClass",
    "EvidenceItem",
    "EvidenceOrigin",
    "HealthStatus",
    "ImportInventoryReceipt",
    "KnowledgeArtifact",
    "Lane",
    "LaneCandidate",
    "LearningEvent",
    "LimitReceipt",
    "MergedCandidate",
    "MeterReading",
    "MirrorAdapterDescriptor",
    "MirrorReceipt",
    "MirrorState",
    "NormalizedSourceDocument",
    "OutputAdapterDescriptor",
    "OutputReceipt",
    "Ownership",
    "PluginState",
    "PrincipalClaims",
    "ProfileSnapshot",
    "ProjectionResolution",
    "PublicationAuthorization",
    "PublicationClass",
    "PublicationIdentity",
    "PublicationRecord",
    "PublicationState",
    "PublishingBasket",
    "RankingReceipt",
    "RawImport",
    "ReadbackVerdict",
    "ReceiptEnvelope",
    "ReceiptState",
    "RetentionState",
    "ScoredCandidate",
    "ScorerKind",
    "Scope",
    "SearchOutcome",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "SearchResultClass",
    "Slate",
    "SlateEntry",
    "SourceCapabilities",
    "SourceCheckpoint",
    "SourceHealthRecord",
    "SourcePlugin",
    "SourcePluginRegistration",
    "SourceProvenance",
    "SourceRights",
    "StoryRecord",
    "Tenant",
    "TenantMembership",
    "User",
    "WriteMode",
]
