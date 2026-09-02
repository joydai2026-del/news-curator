"""Publication contract: the shared state machine every publisher obeys.

Declarative only. No behavior, no I/O.
Freezes: plan "All publishing adapters share ..." and the Daily TEA identity
ruling, plus SC-30 and SC-37.

State machine (frozen):

    draft -> ready -> authorized -> publishing -> settled
    publishing -> failed-safe
    publishing -> unknown
    authorized -> ready        (digest changed, or authorization expired)
    ready -> draft             (basket no longer eligible)

``settled`` is terminal for one publication identity. ``unknown`` blocks a fresh
publish until a readback resolves it; it never falls back to a retry. The ONLY
way out of ``unknown`` is a readback-resolution edge: a positive readback
resolves to ``settled``, a conclusive negative readback resolves to
``failed-safe``. There is still no ``unknown -> publishing`` edge, so an
ambiguous send can never be silently retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import PublicationState
from .tenant import Ownership

PUBLICATION_TRANSITIONS: tuple[tuple[PublicationState, PublicationState], ...] = (
    (PublicationState.DRAFT, PublicationState.READY),
    (PublicationState.READY, PublicationState.DRAFT),
    (PublicationState.READY, PublicationState.AUTHORIZED),
    (PublicationState.AUTHORIZED, PublicationState.READY),
    (PublicationState.AUTHORIZED, PublicationState.PUBLISHING),
    (PublicationState.PUBLISHING, PublicationState.SETTLED),
    (PublicationState.PUBLISHING, PublicationState.FAILED_SAFE),
    (PublicationState.PUBLISHING, PublicationState.UNKNOWN),
    (PublicationState.FAILED_SAFE, PublicationState.READY),
    # Readback-resolution edges (frozen 2026-09-01, contract-freeze review).
    # Both REQUIRE a readback_verdict carried on the record: `unknown` was
    # declared non-terminal but had zero outgoing edges, which parked an
    # ambiguous send forever. Gating by readback_verdict (never a timer or a
    # retry) is what keeps this from being the auto-retry the plan forbids.
    (PublicationState.UNKNOWN, PublicationState.SETTLED),
    (PublicationState.UNKNOWN, PublicationState.FAILED_SAFE),
)

PUBLICATION_TERMINAL_STATES: tuple[PublicationState, ...] = (
    PublicationState.SETTLED,
)


@dataclass(frozen=True)
class PublicationIdentity:
    """The at-most-once key. The content digest is deliberately NOT part of it.

    Keyed by tenant, publisher, destination, and issue date only. If the digest
    were part of the key, changing the basket after a publish would mint a
    second identity and the same issue would go out twice.
    """

    tenant_id: str
    publisher_identity_ref: str
    destination: str
    issue_date: str


@dataclass(frozen=True)
class PublicationAuthorization(Ownership):
    """Immutable approval, bound to exactly what was approved.

    The digest binds AUTHORIZATION, not identity. A changed digest invalidates
    this authorization and returns the basket to ``ready`` for a fresh approval.

    This is a stored, per-tenant row, so it carries the shared ``Ownership``
    shape like every other private record. The inherited ``actor_id`` IS the
    approver (the removed ``approved_by_actor_id`` was a second spelling of the
    same thing), and ``user_id`` is the human that approver acted for. An
    approval is always attributable to a person, so ``user_id`` is required
    non-blank here even when the approving actor is an agent.

    ``identity.tenant_id`` must equal the inherited ``tenant_id``: an approval
    that names one tenant in its key and another in its ownership row would let
    a member of tenant A authorize a publication belonging to tenant B.
    """

    authorization_id: str
    identity: PublicationIdentity
    content_digest: str
    policy_revision: int
    approved_at: datetime
    expires_at: datetime
    # The approving credential must never be the credential that executes the
    # publish, nor the ingestion credential.
    approval_credential_id: str = ""


@dataclass(frozen=True)
class PublicationRecord(Ownership):
    """The live state of one publication identity.

    A stored, per-tenant row, so it carries the shared ``Ownership`` shape. The
    inherited ``actor_id`` is whoever executed the LAST transition recorded
    here (the publisher credential for ``publishing`` and ``settled``, the
    approver for ``authorized``), and ``user_id`` is the human that actor acted
    for. A publication is always attributable to a person, so ``user_id`` is
    required non-blank even when the executing actor is the system.

    ``identity.tenant_id`` must equal the inherited ``tenant_id``.
    """

    identity: PublicationIdentity
    state: PublicationState
    updated_at: datetime
    authorization_id: str | None = None
    content_digest: str = ""
    # Derived from `identity` ALONE (tenant, publisher, destination, issue
    # date). Never derived from the digest, so a re-authorization after a
    # digest change keeps the same key instead of minting a second identity
    # for the same issue date.
    idempotency_key: str = ""
    attempt: int = 0
    settled_receipt_id: str = ""
    reason_code: str = ""
    # The state this record transitioned FROM. Empty means "no prior state
    # recorded for this fixture", which skips transition validation. Present
    # so a single record can prove it followed a legal edge, in particular
    # the readback-gated edges out of `unknown`.
    prior_state: str = ""
    # Set only when prior_state is "unknown". One of ReadbackVerdict's wire
    # values. Required to leave `unknown`: positive resolves to `settled`,
    # negative_conclusive resolves to `failed-safe`, inconclusive stays.
    readback_verdict: str = ""
    # Set only when prior_state is "unknown". Names the readback receipt that
    # resolved the ambiguous send. Required to leave `unknown`: without it, a
    # record could claim a readback_verdict with nothing backing it.
    readback_receipt_ref: str = ""


@dataclass(frozen=True)
class PublishingBasket(Ownership):
    """A basket of candidates. Readiness is a signal, never an authorization."""

    basket_id: str
    destination: str
    issue_date: str
    item_story_ids: tuple[str, ...]
    required_item_count: int
    content_digest: str
    state: PublicationState
    updated_at: datetime
    # True when the item count is met. It grants nothing on its own.
    readiness_signal: bool = False
    revision: int = 0
    item_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
