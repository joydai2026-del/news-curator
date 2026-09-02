# Authorization contract

Typed definition: `curator/contracts/authorization.py`
Fixtures: `tests/fixtures/contracts/authorization/`
Freezes: plan section "Authorization contract". Criteria: SC-34, and the
credential-separation requirements referenced by SC-05, SC-30, SC-37.

## Purpose

Authentication proves a principal identity. Authorization decides what that
principal may do. This contract exists so that "who may delete" and "who may
publish" are data a test can read, not prose someone has to remember.

The rule that shapes everything below: **an action absent from the matrix fails
closed.** It never falls back to "any authenticated tenant member".

## Records

### `PrincipalClaims`

Carried by every request, human or agent.

| Field | Type | Constraint |
|---|---|---|
| `principal_id` | str | Required. Provider-neutral. |
| `tenant_id` | str | Required. Verified against live membership, not trusted from the token. |
| `membership_id` | str | Required. |
| `actor_id` | str | Required. |
| `actor_kind` | `ActorKind` | Required. |
| `audience` | str | Required. A token minted for another audience is rejected. |
| `issued_at`, `expires_at` | datetime | Required. `expires_at` must be strictly after `issued_at`. |
| `credential_id` | str | Required. Identifies WHICH credential, which is what makes separation checkable. |
| `revocation_version` | int | Required. Compared against current state on every request. |
| `roles` | tuple of str | Default empty. |
| `scopes` | tuple of `Scope` | Default empty. Closed vocabulary; an unknown scope string is rejected. |

### `ActionRequirement`

One frozen row of the matrix.

| Field | Type | Constraint |
|---|---|---|
| `action` | str | Required. Stable action name. |
| `required_scope` | `Scope` | Required. |
| `human_requirement`, `agent_requirement` | str | Required. |
| `requires_separate_credential` | bool | Default `false`. |
| `requires_idempotency_key` | bool | Default `false`. |
| `requires_revision_check` | bool | Default `false`. |
| `requires_dry_run_first` | bool | Default `false`. |
| `forbidden_on_paths` | tuple of str | Default empty. Named pipeline stages that must not be able to reach a credential holding this scope. |

### `AuthorizationAudit`

One row per allow AND one per deny. Carries no private payload content.

| Field | Type | Constraint |
|---|---|---|
| `audit_id` | str | Required. |
| `tenant_id`, `principal_id`, `actor_id` | str | Required. |
| `action` | str | Required. |
| `required_scope` | `Scope` | Required. |
| `decision` | `Decision` | Required. `allow` or `deny`. |
| `reason_code` | str | Required. Stable code, never free-form detail or payload text. |
| `occurred_at` | datetime | Required. |
| `credential_id`, `revocation_version` | str, int | Required. |

## The frozen action matrix

**Frozen as typed data, not only as this table.** `curator/contracts/authorization.py`
defines `ACTION_MATRIX: tuple[ActionRequirement, ...]`, the 14 rows below as
`ActionRequirement` instances. A test asserts every `Scope` member is covered
by at least one row, that `publish:approve` and `publish:execute` never share
a row, and that `delete_data` carries `requires_idempotency_key: true`
(corrected from an earlier draft that had it `false`, contradicting this
table). An implementer reads `ACTION_MATRIX`, never retypes this table.

| Action | Scope | Separate credential | Idempotency key | Revision check | Dry run first |
|---|---|---|---|---|---|
| Read private story | `stories:read` | no | no | no | no |
| Search | `search:read` | no | no | no | no |
| Give feedback | `feedback:write` | no | **yes** | no | no |
| Save artifact | `artifacts:write` | no | yes | **yes** | no |
| Mirror artifact | `mirrors:write` | no | yes | **yes** | no |
| Change ranking policy | `policy:admin` | no | no | **yes** | no |
| Import approved history | `imports:write` | **yes** | yes | no | **yes** |
| Export data | `data:export` | no | yes | no | no |
| Delete data | `data:delete` | **yes** | **yes** | no | **yes** |
| Ask AI conversation | `conversations:write` | no | **yes** | no | no |
| Mutate publishing basket | `baskets:write` | no | yes | **yes** | no |
| Approve publication | `publish:approve` | **yes** | no | **yes** | no |
| Execute publication | `publish:execute` | **yes** | **yes** | **yes** | no |
| Use publishing signer | `signer:use` | **yes** | no | no | no |

Credential isolation, stated as `forbidden_on_paths`:

| Scope | Unreachable from |
|---|---|
| `data:delete` | ingestion, import, export, read |
| `publish:execute` | the credential that granted `publish:approve`, and ingestion |
| `signer:use` | ingestion, ranking, import, export |
| `imports:write` | delete, publish, signer |

## Invariants

1. Verified before any read or write, in this order: tenant membership, action
   scope, audience, expiry, current revocation version.
2. Every evaluation writes an audit row. A deny is as auditable as an allow, and
   neither row contains private payload content.
3. `publish:approve` and `publish:execute` are never held by the same
   credential. Approval that can execute itself is not approval.
4. The import credential can never delete. This is the same separation the plan
   applies to publishing, applied to destruction.
5. An expired or revoked credential fails closed even when its scopes match.
6. Private API documentation is authenticated and nonindexable. Public
   documentation describes only the public projection API and contains no
   private route examples, tenant identifiers, or private schemas.

## Freeze notes

- The plan's matrix has 12 rows; the table above splits two of them into 14
  actions (read split from search, and Save/Save answer's action pair split
  from feedback) because they carry different scopes. No action was added or
  dropped; the split is right, an earlier draft of this note miscounted the
  plan's row total as 14.
- `requires_dry_run_first` was added for import and delete. The plan names a
  dry-run requirement for the agent import path in prose; making it a field lets
  a test assert it rather than a reviewer remember it. This is the "smallest
  thing consistent with the plan" choice.
- Roles stay free strings while scopes are closed. See `tenant.md` freeze notes.
- Grade: C for all of it. No authorization layer exists in the repository today;
  the shipped preference path enforces owner-only row access at the database
  level, which is a narrower thing (see `personalization-reconciliation.md`).
