"""Artifact contract: durable questions, answers, reports, insights, and saves.

Declarative only. No behavior, no I/O.
Freezes: plan "Core records" (`knowledge_artifacts`, `artifact_versions`,
`artifact_relations`) and SC-26.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ArtifactStatus, ArtifactType, PublicationClass
from .tenant import Ownership


@dataclass(frozen=True)
class KnowledgeArtifact(Ownership):
    """The canonical private record. Mirrors are copies, never the original."""

    artifact_id: str
    artifact_type: ArtifactType
    status: ArtifactStatus
    publication_class: PublicationClass
    created_at: datetime
    current_version: int
    title: str = ""
    conversation_id: str | None = None
    story_id: str | None = None


@dataclass(frozen=True)
class ArtifactVersion(Ownership):
    """One immutable version. A revision appends; it never rewrites history.

    ``checksum`` is the value a mirror compares against, so an external target
    can be proven to hold this exact version and no other.

    Ownership is inherited (2026-09-02). This row previously carried
    ``actor_id`` and no ``tenant_id`` at all, so its isolation depended
    entirely on a join to its parent artifact.
    """

    artifact_id: str
    version: int
    parent_version: int | None
    checksum: str
    content_reference: str
    settled_at: datetime
    citations: tuple[str, ...] = field(default_factory=tuple)
    # Set when a correction redacted this version's content. The version row
    # itself remains queryable.
    redacted_by_event_id: str | None = None


@dataclass(frozen=True)
class ArtifactRelation(Ownership):
    """The conversation-to-artifact graph, both directions retained."""

    relation_id: str
    conversation_id: str
    artifact_id: str
    relation_type: str
    requested_type: str = ""
    depth: int = 0
