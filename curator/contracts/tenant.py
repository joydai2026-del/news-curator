"""Tenant contract: provider-neutral identity for every private record.

Declarative only. No behavior, no I/O.
Freezes: plan "Privacy and security boundary" and the Locked WHAT privacy row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ActorKind, PublicationClass


@dataclass(frozen=True)
class Tenant:
    """One isolation boundary. Today there is exactly one private tenant."""

    tenant_id: str
    display_name: str
    default_publication_class: PublicationClass
    created_at: datetime
    # A public tenant is a separate authenticated tenant, never a flag on the
    # private one. Recorded here so the later public product cannot be built by
    # relaxing a boolean on the private tenant's row.
    is_public_projection_tenant: bool = False


@dataclass(frozen=True)
class User:
    """A human identity inside one tenant, independent of any auth provider."""

    user_id: str
    tenant_id: str
    created_at: datetime
    # An identity provider maps its own subject onto user_id. The subject value
    # itself is held by the identity adapter, never by a core record.
    display_name: str = ""


@dataclass(frozen=True)
class Actor:
    """Who performs an action: a human, an agent, or the system itself.

    Every event, artifact, receipt, and audit row references an actor_id, so an
    agent-written record is always distinguishable from a human-written one.
    """

    actor_id: str
    tenant_id: str
    actor_kind: ActorKind
    created_at: datetime
    user_id: str | None = None
    label: str = ""


@dataclass(frozen=True)
class TenantMembership:
    """Binds a principal to a tenant with roles. Revocable, never deleted."""

    membership_id: str
    tenant_id: str
    principal_id: str
    actor_id: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    active: bool = True
    revoked_at: datetime | None = None
