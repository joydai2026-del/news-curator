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
from .tenant import Actor, Tenant, TenantMembership, User

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
    "MIRROR_TERMINAL_STATES",
    "MIRROR_TRANSITIONS",
    "PUBLICATION_TERMINAL_STATES",
    "PUBLICATION_TRANSITIONS",
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
