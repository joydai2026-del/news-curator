# Output adapter contract

Typed definition: `curator/contracts/output_adapter.py`
Fixtures: `tests/fixtures/contracts/output-adapter/`
Freezes: plan section "Knowledge and output adapters". Criteria: SC-36, and the
state machine shared with [publication.md](publication.md) for SC-37.

## Purpose

One provider-neutral contract for every destination, private or public. Adding
or removing an adapter changes registry configuration and that adapter only. It
cannot require a core ranking change or alter an unrelated adapter.

## Records

### `OutputAdapterDescriptor`

| Field | Type | Constraint |
|---|---|---|
| `adapter_id`, `adapter_version` | str | Required. |
| `eligible_artifact_types` | tuple of `ArtifactType` | Required. Closed vocabulary. A raw import can never be listed. |
| `requires_approval` | bool | Required. |
| `supports_dry_run` | bool | Required. |
| `supports_readback` | bool | Required. |
| `publisher_identity_ref` | str | Default empty. A reference, never a key or a credential. |
| `emits_public_content` | bool | Default `false`. Declared up front, not decided at call time. |

### `DryRunResult`

| Field | Type | Constraint |
|---|---|---|
| `adapter_id`, `artifact_id`, `destination` | str | Required. |
| `artifact_version` | int | Required. |
| `content_digest` | str | Required. |
| `eligible` | bool | Required. |
| `reason_code` | str | Default empty. |
| `blocking_checks` | tuple of str | Default empty. Names every check that WOULD block a real publish. |

`blocking_checks` exists because a dry run that always passes is worthless. It
must name what it evaluated, not just return a verdict.

### `OutputReceipt`

| Field | Type | Constraint |
|---|---|---|
| `receipt_id`, `adapter_id`, `destination` | str | Required. |
| `tenant_id`, `actor_id`, `actor_kind`, `user_id` | inherited from `Ownership` | Required, all four. SUBJECT-BOUND: `user_id` required, non-blank, regardless of writer. See [tenant.md](tenant.md#ownership). |
| `publisher_identity_ref` | str | Required. |
| `content_digest` | str | Required. |
| `idempotency_key` | str | Required. Derived from the publication identity, never from the digest. |
| `authorization_id` | str | Required. |
| `policy_revision` | int | Required. |
| `authorization_expiry` | datetime | Required. |
| `attempt` | int | Required. |
| `state` | `PublicationState` | Required. The shared seven-state machine. |
| `receipt_state` | `ReceiptState` | Required. This attempt's settlement verdict. |
| `attempted_at` | datetime | Required. |
| `acknowledgement_ref` | str | Default empty. |
| `settled_at` | datetime or null | |
| `reason_code` | str | Default empty. |
| `acknowledged_targets` | tuple of str | Default empty. For fan-out adapters. |

## The adapter roster and its boundaries

The labels in this table record contract coverage only. They do not establish
implementation order or product priority.

| Adapter | Contract state | Boundary |
|---|---|---|
| Private web | Boundary defined | Reads private tenant projections after edge authentication. |
| Vault Wiki | Boundary defined | Mirrors saved artifacts with git-based compare-and-set receipts, commit parent as the enforced precondition. |
| Document workspace | Optional boundary defined | Output-only unless separately configured as an import plugin. **Append-only revisions**, because its update endpoint exposes no atomic conditional write. |
| Long-form relay | Unscheduled boundary defined | Isolated publisher identity, stable article identifier across revisions, distinct event id per revision, acknowledgement threshold, idempotency receipt. |
| Newsletter basket | Unscheduled boundary defined | Saved basket, item-count readiness signal, separate programmable approval and publish policy, at-most-once receipt. |
| Public web / API / feeds | Unscheduled boundary defined | Explicit public projections only. Never private tenant records. |

## Invariants

1. An adapter with `emits_public_content: true` must set `requires_approval:
   true`. A public destination that publishes without approval is the failure
   this field pair exists to prevent.
2. `eligible_artifact_types` is a closed vocabulary. A new output class needs its
   own eligibility and mirror rules and cannot be introduced by writing a string.
3. `state: unknown` REQUIRES `receipt_state: unknown`, and `state: settled`
   requires `receipt_state: settled`. An ambiguous response that reports a
   settled receipt is how a send becomes a silent double publish. A fixture
   asserts the rejection. **`state: settled` also requires a non-empty
   `acknowledgement_ref` and a non-null `settled_at`**: matching `state` and
   `receipt_state` is not proof anything was actually acknowledged by the
   destination (mirroring `mirror.md`'s "a write acknowledgement alone never
   settles"). A fixture asserts the rejection of a settled receipt with
   neither.
4. A retry addresses only destinations absent from `acknowledged_targets` for
   that exact content. It never re-sends to a target that already acknowledged.
5. The publishing signer is isolated to the publishing adapter. No ingestion,
   ranking, import, or export path may hold it (`signer:use`, see
   [authorization.md](authorization.md)).
6. Adding or removing a fixture adapter produces no diff in core code or in any
   unrelated adapter (SC-36).
7. Verbatim quoted commentary extracted from a newsletter is not public-eligible
   without separate licensing. A public adapter's dry run must evaluate that
   check and name it in `blocking_checks`.

## Freeze notes

- **2026-09-02, ownership.** `OutputReceipt` inherits the four `Ownership`
  fields. `OutputAdapterDescriptor` and `DryRunResult` do not: the descriptor
  is adapter configuration and the dry-run result is a computed answer, not a
  stored private row.

- **2026-09-02, subject attribution.** `OutputReceipt` is SUBJECT-BOUND: it
  proves one human's edition reached a destination, so `user_id` is required
  non-blank.

- The adapter roster names destination CLASSES, not vendors, which is why the
  table above reads "document workspace" and "long-form relay". The concrete
  provider for each lives in that adapter's own configuration.
- `state` and `receipt_state` are two fields rather than one because they answer
  different questions: where the publication stands, and how this attempt
  finished. Collapsing them would make "ambiguous response, no publication" and
  "failed attempt, publication still ready" indistinguishable.
- Grade: C. No output adapter exists in the repository today.
