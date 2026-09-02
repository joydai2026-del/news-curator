# Personalization reconciliation: adopt, adopt-with-migration, or supersede

Date: 2026-09-01
Inputs inspected: `supabase/migrations/202608290001_user_preferences.sql` and
`docs/contracts/personalization.md`, both read in full at commit `ef9a855`.
Compared against: [tenant.md](tenant.md), [authorization.md](authorization.md),
[evidence.md](evidence.md).
Required by: SC-41 ("the shipped Supabase personalization schema and contract are
inspected and explicitly adopted or superseded with a recorded decision"), and
carried forward from the Gate 0c receipt, which did not perform this inspection.

**This document recommends. It does not decide.** The decision is the owner's.

## Recommendation in one line

**ADOPT WITH ADDITIVE MIGRATION.** Keep the shipped table, its row-level
security, and its compare-and-swap update path exactly as they are; add tenancy
and actor columns, reframe the record as *declared preference evidence* rather
than *the profile*, and resolve two genuine conflicts (a cascade hard delete and
a direct delete grant) that contradict the append-only rule.

## Why not the other two verdicts

**Not "adopt as-is."** Three things are missing that the new contracts require of
every private record: `tenant_id`, `actor_id`, and an append-only deletion path.
Adopting unchanged would mean the first record type in the system is the one that
does not follow the tenancy rule, and every later record would inherit the
exception.

**Not "supersede."** The shipped schema's security posture is stronger than
anything the new contracts specify, and it is already written, reviewed, and
locally tested: forced row-level security, four owner-only policies, **no direct
update grant at all**, insert narrowed to four columns, a `security definer`
compare-and-swap function that derives the caller from the session rather than
trusting a supplied id, and `execute` revoked from `public`, `anon`, and
`authenticated` on every helper function. Throwing that away to rewrite it in the
same shape would be pure risk with no gain. What the new contracts add is
tenancy, provenance, and lifecycle, all of which are additive.

## Field-by-field comparison

### `public.user_preferences` against the new contracts

| Shipped field | Type and constraint (Grade B, read from the migration) | New-contract counterpart | Verdict |
|---|---|---|---|
| `user_id` | `uuid primary key references auth.users(id) on delete cascade` | `tenant.User.user_id` | **Adopt with change.** The id concept maps cleanly. The `on delete cascade` does not: see conflict C1. |
| none | absent | `tenant_id` (required on every private record) | **Delta D1. Add.** |
| none | absent | `actor_id` (required to tell a human write from an agent write) | **Delta D2. Add.** |
| `revision` | `bigint not null default 0 check (revision >= 0)`, server-owned, increments by one | `ActionRequirement.requires_revision_check`; basket revision field | **Adopt as-is.** This is exactly the compare-and-set discipline the new contracts require for artifact, mirror, basket, and policy writes. The new contracts should follow this pattern, not the reverse. |
| `locale` | `not null default 'en' check (locale in ('en','zh'))` | **No counterpart.** The normalized-document language field is a document property, not a reader preference. | **Delta D3.** The new contracts have no home for a reader locale. Keep it here. |
| `interests` | `text[] not null default '{}'`, ≤20 entries, each 1-80 chars and ≤160 bytes, trimmed | derived profile topic-affinity field | **Conceptual difference, not a conflict. See below.** |
| `saved_searches` | `jsonb not null default '[]'`, ≤20 objects, ≤8192 serialized bytes; each exactly `{id, query, enabled}` with unique trimmed ids | search request contract (a request, not a stored object) | **Delta D4.** A saved search stores only free text: no lane, topic, source, or class filters. |
| `created_at`, `updated_at` | `timestamptz not null default statement_timestamp()`, `updated_at` maintained by trigger | `created_at` on most records; `recorded_at` on evidence | **Adopt as-is.** |

### The one conceptual difference worth stating plainly

`interests` is a **declared** preference: the reader typed it. `ProfileSnapshot`
is a **derived** state: rebuilt from evidence, invalidated by corrections, and
versioned against an evidence watermark.

These are not competing versions of the same thing, and collapsing them would be
a mistake in either direction:

- Making `interests` the profile would mean feedback events never change what is
  recommended, which contradicts the whole event contract.
- Deleting `interests` in favour of the derived profile would throw away the only
  personalization signal that exists before any import lands, and would remove
  the reader's ability to state a preference directly.

The clean resolution: **a declared interest is a strong explicit evidence source
that feeds the snapshot**, alongside events. It gets an `EvidenceClass.EXPLICIT`,
`EvidenceOrigin.LIVE`, `ConfidenceBand.STRONG` row per interest, sourced from
this table, and the snapshot builder aggregates it like any other evidence. The
table stays the system of record for what the reader *said*; the snapshot stays
the system of record for what the system *believes*.

### Security and access posture

| Concern | Shipped (Grade B) | New contracts | Verdict |
|---|---|---|---|
| Row isolation | Forced RLS, four owner-only policies keyed on `auth.uid() = user_id` | Tenant membership verified server-side before any read or write | **Adopt the shipped mechanism.** It is stronger: enforcement is in the database, not in application code that could be bypassed. Add a tenant predicate alongside the user predicate. |
| Update path | **No direct update grant.** All updates go through the CAS function. | `requires_revision_check` on artifact, mirror, basket, and policy writes | **Adopt as-is, and generalize it.** |
| Caller identity | `security definer` function derives `caller_id := auth.uid()`; the client never sends a user id or a chosen revision on update | `PrincipalClaims.principal_id` verified, never trusted from the payload | **Aligned.** |
| Credential in the browser | Publishable key only; a service-role key is prohibited in both flows | "The browser receives no service-role key" | **Aligned.** |
| Scopes | None. Any authenticated session may read, insert, and delete its own row. | Closed `Scope` vocabulary, per-action | **Delta D5.** Acceptable for declared preferences; not acceptable once this row can influence sequence calculation. |
| Audit | None. No allow/deny record. | Every allow and every deny writes an authorization audit row | **Delta D6.** |
| Delete | `grant delete ... to authenticated` on the owner's own row, plus `on delete cascade` from `auth.users` | Append-only. A delete request is a correction event plus a receipt naming every derived projection. | **Conflicts C1 and C2.** |

## The exact deltas

Additive, no behavior change to what ships today:

- **D1 `tenant_id`.** Add a not-null column, backfilled to the single private
  tenant, and add it to every RLS predicate. Without it the table is the one
  private record type with no isolation boundary.
- **D2 `actor_id`.** Add, so a preference change made by the CLI is
  distinguishable from one made in the browser. Both use the same session today.
- **D3 `locale`.** No change. Record in the tenant contract that the reader
  locale lives here, so it is not re-invented elsewhere.
- **D4 saved-search shape.** Optional. If saved searches should carry lane,
  topic, source, or class filters, that is an additive JSON schema change inside
  `saved_searches`; the current three-key shape stays valid.
- **D5 scopes.** When this row starts influencing ranking, reads and writes move
  behind `stories:read` and `feedback:write` equivalents. Not needed while it is
  a preference store only.
- **D6 audit.** Add allow/deny audit rows when the authorization layer lands.
  Not a change to this table.
- **D7 declared-interest evidence bridge.** New: a projection that turns each
  `interests` entry into an `EvidenceItem` with explicit class, live origin,
  strong confidence, and this table's `revision` as its `source_item_id`, so a
  changed interest list produces new evidence idempotently rather than a
  duplicate on every rebuild.

Genuine conflicts, each needing a decision:

- **C1 `on delete cascade` from `auth.users`.** Deleting an identity-provider
  user **hard-deletes** the preference row. The new contracts require deletion to
  be a correction event plus a receipt, with the audit chain still queryable
  afterwards. Options: (a) change to `on delete restrict` and route deletion
  through the correction path; (b) keep the cascade and accept that
  provider-level account deletion is a hard delete for this table only,
  documented as an explicit exception; (c) detach the foreign key entirely and
  map provider subjects through an identity adapter, which is what
  [tenant.md](tenant.md) already assumes for SC-32. **Leaning (c)**, because it
  also removes the last provider-shaped dependency from a core table, but this is
  the owner's call.
- **C2 direct `delete` grant.** Any authenticated session may delete its own row.
  The authorization contract requires `data:delete` on a credential separate from
  the read credential. For a declared-preference row this is arguably fine and
  even good for a data-subject deletion path. For anything holding evidence it is
  not. Recommendation: keep the grant on THIS table, and never extend the pattern
  to evidence, event, artifact, or receipt tables.

## What is NOT in scope of this recommendation

- The browser and CLI authentication flows, PKCE handling, token persistence, and
  refresh rotation described in `personalization.md`. This freeze does not touch
  them and does not evaluate them.
- Cloud activation. The shipped contract states plainly that cloud activation is
  incomplete and lists a nine-step checklist. Everything asserted about the LIVE
  cloud project is **Grade C**, and nothing here should be read as evidence that
  the schema is deployed.
- The translation contract, untouched by this freeze.

## Grades

| Claim | Grade | Basis |
|---|---|---|
| The migration's columns, constraints, policies, grants, and function bodies are as described | **B** | Read in full from the migration file at `ef9a855`. |
| The shipped contract document describes a local implementation with cloud activation incomplete | **B** | Stated in its own first line. |
| The schema is live in a cloud project | **C** | Not verified, and the contract itself says it is not. |
| The additive migration will not break the shipped browser and CLI paths | **C** | Argued from the fact that every delta adds columns or projections rather than changing the four insertable columns or the CAS signature. Not proven: proving it requires running the existing personalization test suite against a migrated schema. |
| A declared interest is best modelled as strong explicit evidence rather than as the profile | **C** | A design judgement, argued above from the event contract, not a measurement. |

## Recorded decision (adjudicated 2026-09-01, open to owner veto at review)

The recommendation ADOPT WITH ADDITIVE MIGRATION is accepted. The two named conflicts settle as follows:

1. **`on delete cascade` vs append-only deletion.** Both stand, at different layers. Account deletion (the `auth.users` cascade) is a lawful-erasure path and keeps the cascade; the account-deletion flow MUST write a deletion receipt recording the cascade so the audit chain shows why the row vanished. Learning evidence, events, and artifacts remain append-only with retraction events and rebuild receipts; the cascade never substitutes for the evidence-layer deletion contract.
2. **Direct `delete` grant vs `data:delete` separation.** The additive migration revokes the direct row `DELETE` grant from ordinary authenticated users; deletion routes through the authorized `data:delete` scope per the frozen authorization contract, with the same separate-credential rule that separates publish approval from ingestion.

Rationale: recoverability and audit beat convenience on both, and neither choice changes a user-visible behavior in the single-user pilot.
