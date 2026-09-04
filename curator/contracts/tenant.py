"""Tenant contract: provider-neutral identity for every private record.

Declarative only. No behavior, no I/O.
Freezes: plan "Privacy and security boundary" and the Locked WHAT privacy row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ActorKind, PublicationClass


@dataclass(frozen=True)
class Ownership:
    """The four ownership fields EVERY private record carries. All required.

    Inherited, never re-declared, so a private record cannot ship with three of
    the four (or with a one-off spelling of one of them). Dataclass inheritance
    keeps the fields FLAT: one column each in SQL, one key each in a fixture.

    Semantics:

    - ``tenant_id`` is the isolation boundary the row belongs to.
    - ``actor_id`` is WHO WROTE the row: a human, an agent, or the system.
    - ``actor_kind`` says which of those three it is.
    - ``user_id`` is the HUMAN THE ACTOR ACTS FOR. A human actor is that human;
      an agent actor names the human it acts for, which is what keeps an
      agent-written row attributable. A system actor may have none.

    ``user_id`` is required as a KEY and nullable as a VALUE, and it may be null
    ONLY when ``actor_kind`` is ``system``. It is not optional-with-a-default:
    an omitted guard field that silently skips its own check is the exact
    bypass class the contract-freeze review found, so absence fails structural
    validation rather than defaulting to "no human".
    """

    tenant_id: str
    actor_id: str
    actor_kind: ActorKind
    user_id: str | None


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

    ``user_id`` follows the same null rule as ``Ownership``: a ``human`` or
    ``agent`` actor names the human it acts for; only a ``system`` actor may
    carry null.
    """

    actor_id: str
    tenant_id: str
    actor_kind: ActorKind
    created_at: datetime
    # REQUIRED, never optional-with-a-check. An optional guard skips its own
    # check when it is omitted, and this is the row that makes an agent-written
    # record attributable to a human, so an omitted user_id here would silently
    # unbind every record that actor writes. Null is legal ONLY for a system
    # actor; a human or agent actor requires a non-blank user_id.
    user_id: str | None
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
