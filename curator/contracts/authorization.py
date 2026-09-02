"""Authorization contract: what a proven principal may do.

Declarative only. No behavior, no I/O.
Freezes: plan "Authorization contract" and SC-34.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .enums import ActorKind, Decision, Scope
from .tenant import Ownership


@dataclass(frozen=True)
class PrincipalClaims:
    """Provider-neutral claims every request carries.

    Authentication proves principal_id. Nothing here grants anything by itself:
    the server still verifies membership, scope, audience, expiry, and the
    current revocation version before any read or write.
    """

    principal_id: str
    tenant_id: str
    membership_id: str
    actor_id: str
    actor_kind: ActorKind
    audience: str
    issued_at: datetime
    expires_at: datetime
    credential_id: str
    revocation_version: int
    roles: tuple[str, ...] = field(default_factory=tuple)
    scopes: tuple[Scope, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ActionRequirement:
    """One row of the frozen authorization matrix.

    An action absent from the matrix fails closed. It never falls back to
    "any authenticated tenant member".
    """

    action: str
    required_scope: Scope
    human_requirement: str
    agent_requirement: str
    requires_separate_credential: bool = False
    requires_idempotency_key: bool = False
    requires_revision_check: bool = False
    requires_dry_run_first: bool = False
    # A credential holding this scope must not be reachable from any of the
    # named pipeline stages. Empty means no isolation requirement.
    forbidden_on_paths: tuple[str, ...] = field(default_factory=tuple)


#: The frozen 14-row action matrix (docs/contracts/authorization.md's table),
#: as typed data. An action absent from this tuple fails closed (SC-34): it
#: never falls back to "any authenticated tenant member" because there is no
#: implicit fallback path, only ``ACTION_MATRIX`` lookup or denial.
ACTION_MATRIX: tuple[ActionRequirement, ...] = (
    ActionRequirement(
        action="read_story",
        required_scope=Scope.STORIES_READ,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
    ),
    ActionRequirement(
        action="search",
        required_scope=Scope.SEARCH_READ,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
    ),
    ActionRequirement(
        action="give_feedback",
        required_scope=Scope.FEEDBACK_WRITE,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
    ),
    ActionRequirement(
        action="save_artifact",
        required_scope=Scope.ARTIFACTS_WRITE,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
        requires_revision_check=True,
    ),
    ActionRequirement(
        action="mirror_artifact",
        required_scope=Scope.MIRRORS_WRITE,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
        requires_revision_check=True,
    ),
    ActionRequirement(
        action="change_ranking_policy",
        required_scope=Scope.POLICY_ADMIN,
        human_requirement="tenant owner",
        agent_requirement="tenant owner's delegated credential",
        requires_revision_check=True,
    ),
    ActionRequirement(
        action="import_history",
        required_scope=Scope.IMPORTS_WRITE,
        human_requirement="tenant owner, dry run reviewed first",
        agent_requirement="separate import credential, dry run reviewed first",
        requires_separate_credential=True,
        requires_idempotency_key=True,
        requires_dry_run_first=True,
        forbidden_on_paths=("delete", "publish", "signer"),
    ),
    ActionRequirement(
        action="export_data",
        required_scope=Scope.DATA_EXPORT,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
    ),
    ActionRequirement(
        action="delete_data",
        required_scope=Scope.DATA_DELETE,
        human_requirement="authorized tenant role plus a separate delete credential",
        agent_requirement="separate delete credential, never the ingestion, export, or read credential",
        requires_separate_credential=True,
        requires_idempotency_key=True,
        requires_dry_run_first=True,
        forbidden_on_paths=("ingestion", "import", "export", "read"),
    ),
    ActionRequirement(
        action="ask_ai_conversation",
        required_scope=Scope.CONVERSATIONS_WRITE,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
    ),
    ActionRequirement(
        action="mutate_publishing_basket",
        required_scope=Scope.BASKETS_WRITE,
        human_requirement="authenticated tenant member",
        agent_requirement="authenticated tenant member",
        requires_idempotency_key=True,
        requires_revision_check=True,
    ),
    ActionRequirement(
        action="approve_publication",
        required_scope=Scope.PUBLISH_APPROVE,
        human_requirement="authorized approver, separate from the executing credential",
        agent_requirement="separate approval credential, never the executing credential",
        requires_separate_credential=True,
        requires_revision_check=True,
    ),
    ActionRequirement(
        action="execute_publication",
        required_scope=Scope.PUBLISH_EXECUTE,
        human_requirement="authorized publisher role acting on an existing approval",
        agent_requirement="separate execution credential",
        requires_separate_credential=True,
        requires_idempotency_key=True,
        requires_revision_check=True,
        forbidden_on_paths=("publish_approve_credential", "ingestion"),
    ),
    ActionRequirement(
        action="use_publishing_signer",
        required_scope=Scope.SIGNER_USE,
        human_requirement="authorized publisher role",
        agent_requirement="separate signer credential",
        requires_separate_credential=True,
        forbidden_on_paths=("ingestion", "ranking", "import", "export"),
    ),
)


@dataclass(frozen=True)
class AuthorizationAudit(Ownership):
    """One per allow and one per deny. Carries no private payload content."""

    audit_id: str
    principal_id: str
    action: str
    required_scope: Scope
    decision: Decision
    reason_code: str
    occurred_at: datetime
    credential_id: str
    revocation_version: int
