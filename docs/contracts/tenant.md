# Tenant contract

Typed definition: `curator/contracts/tenant.py`
Fixtures: `tests/fixtures/contracts/tenant/`
Freezes: plan sections "Privacy and security boundary" and the Locked WHAT
privacy row. Criteria: SC-13, SC-27, SC-32.

## Purpose

Every private record in the system carries provider-neutral `tenant_id`,
`user_id`, and `actor_id`. That is the whole point of this contract: identity is
decided here, once, so that a later sign-in provider, a second user, or a public
product is an adapter and a new tenant row rather than a schema change.

Today there is exactly one tenant, private, with one human user. The contract is
written for many because retrofitting tenancy after the ledger exists is the
expensive version.

## Records

### `Tenant`

| Field | Type | Constraint |
|---|---|---|
| `tenant_id` | str | Required. Stable, opaque, never a provider subject. |
| `display_name` | str | Required. Human label only; carries no authority. |
| `default_publication_class` | `PublicationClass` | Required. `private` or `public`. No third value exists. |
| `created_at` | datetime | Required, timezone-aware UTC. |
| `is_public_projection_tenant` | bool | Default `false`. |

### `User`

| Field | Type | Constraint |
|---|---|---|
| `user_id` | str | Required. Stable across any future identity provider. |
| `tenant_id` | str | Required. A user belongs to exactly one tenant. |
| `created_at` | datetime | Required. |
| `display_name` | str | Optional, default empty. |

### `Actor`

| Field | Type | Constraint |
|---|---|---|
| `actor_id` | str | Required. Referenced by every event, artifact, receipt, and audit row. |
| `tenant_id` | str | Required. |
| `actor_kind` | `ActorKind` | Required. `human`, `agent`, or `system`. |
| `created_at` | datetime | Required. |
| `user_id` | str or null | Set when the actor acts for a human. Null for a system actor. |
| `label` | str | Optional. |

### `TenantMembership`

| Field | Type | Constraint |
|---|---|---|
| `membership_id` | str | Required. |
| `tenant_id`, `principal_id`, `actor_id` | str | Required. |
| `roles` | tuple of str | Default empty. Roles gate actions; scopes gate credentials. |
| `active` | bool | Default `true`. |
| `revoked_at` | datetime or null | Set on revocation. The row is never deleted. |

## Invariants

1. Every private record references a `tenant_id`. A record without one cannot be
   isolated and therefore cannot exist.
2. `is_public_projection_tenant` is not a switch on the private tenant. A public
   product is a SEPARATE tenant row with its own membership and its own
   projections; it cannot query raw imports, private events, private profiles,
   questions, or unpublished artifacts.
3. An agent actor is always distinguishable from the human it acts for. They are
   separate `actor_id` values sharing a `user_id`.
4. Revocation is a state change plus a timestamp, never a delete, so an audit
   trail written under a since-revoked membership stays interpretable.
5. A provider subject (an OAuth `sub`, for instance) is held by the identity
   adapter and mapped onto `user_id`. It never becomes a core field, which is
   what makes SC-32 a mapping exercise rather than a migration.

## Freeze notes

- The plan names `tenant_id`, `user_id`, and `actor_id` but does not enumerate
  role names. Roles are left as free strings here, deliberately: the
  authorization contract gates on SCOPES, which are enumerated and closed, so an
  unrecognised role grants nothing. Enumerating roles would be a second closed
  vocabulary doing the same job.
- `PublicationClass` has exactly two members. A "shared" or "unlisted" class was
  not created, because the plan's boundary is binary and a third value would
  need its own projection rules.
- Grade: B that the repository has no tenant column today (the shipped
  `user_preferences` table keys on a user id only; see
  `personalization-reconciliation.md`). C that this shape is sufficient at
  multi-user scale, which is a Later-horizon claim.
