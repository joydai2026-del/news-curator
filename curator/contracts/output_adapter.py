"""Output adapter contract: one provider-neutral surface for every destination.

Declarative only. No behavior, no I/O.
Freezes: plan "Knowledge and output adapters" and SC-36.

Adding or removing an adapter changes registry configuration and that adapter
only. It cannot require a core ranking change or alter an unrelated adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ArtifactType, PublicationState, ReceiptState


@dataclass(frozen=True)
class OutputAdapterDescriptor:
    """Declared capabilities and boundaries for one destination."""

    adapter_id: str
    adapter_version: str
    eligible_artifact_types: tuple[ArtifactType, ...]
    requires_approval: bool
    supports_dry_run: bool
    supports_readback: bool
    # A publishing identity is isolated to this adapter. No ingestion, ranking,
    # import, or export path may hold it.
    publisher_identity_ref: str = ""
    # Only an adapter that emits public content declares this. It gates the
    # public-projection checks rather than being decided at call time.
    emits_public_content: bool = False


@dataclass(frozen=True)
class DryRunResult:
    """What would be written, proven before anything leaves the system."""

    adapter_id: str
    artifact_id: str
    artifact_version: int
    content_digest: str
    destination: str
    eligible: bool
    reason_code: str = ""
    # Names every check that would block a real publish, so a dry run is a
    # decision aid rather than a rehearsal that always passes.
    blocking_checks: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OutputReceipt:
    """Publish or delivery safety record.

    ``state`` is the publication state machine; ``receipt_state`` is the
    settlement verdict of this attempt. An ambiguous provider response is
    ``PublicationState.UNKNOWN`` with ``ReceiptState.UNKNOWN`` and blocks a
    fresh publish until a readback resolves it.
    """

    receipt_id: str
    tenant_id: str
    adapter_id: str
    publisher_identity_ref: str
    destination: str
    content_digest: str
    idempotency_key: str
    authorization_id: str
    policy_revision: int
    authorization_expiry: datetime
    attempt: int
    state: PublicationState
    receipt_state: ReceiptState
    attempted_at: datetime
    acknowledgement_ref: str = ""
    settled_at: datetime | None = None
    reason_code: str = ""
    # Targets that acknowledged, for adapters that fan out. A retry addresses
    # only the destinations absent from this tuple for this exact content.
    acknowledged_targets: tuple[str, ...] = field(default_factory=tuple)
