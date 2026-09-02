# Mirror contract

Typed definition: `curator/contracts/mirror.py`
Fixtures: `tests/fixtures/contracts/mirror/`
Freezes: plan section "Canonical artifacts are versioned" plus the provider
write-mode table. Criteria: SC-26.

## Purpose

Placing one canonical artifact version onto one external target, provably, and
refusing to guess when the target's answer is ambiguous.

## State machine (frozen)

```
planned ──► writing ──► settled
                   ├──► conflict
                   └──► unknown
```

Legal transitions, and no others:

| From | To |
|---|---|
| `planned` | `writing` |
| `writing` | `settled`, `conflict`, `unknown` |

`settled`, `conflict`, and `unknown` are terminal **for that attempt**.

| State | Meaning | What it permits |
|---|---|---|
| `planned` | The write is intended and the precondition is known. | Proceed to `writing`. |
| `writing` | In flight. | Exactly one of the three terminal outcomes. |
| `settled` | The target holds this exact version, proven by readback. | Nothing further. |
| `conflict` | The target's checksum did not match the expected prior value. Someone else changed it. | **No overwrite. No automatic retry.** A new attempt with a fresh precondition, or a human resolution. |
| `unknown` | The provider's response was ambiguous. | **No overwrite. No automatic retry.** Resolve by readback first. |

## Records

### `MirrorAdapterDescriptor`

| Field | Type | Constraint |
|---|---|---|
| `adapter_id`, `adapter_version` | str | Required. |
| `write_mode` | `WriteMode` | Required. |
| `proves_atomic_conditional_write` | bool | Required. |
| `precondition_kind` | str | Required. What the precondition actually is: `commit_parent`, `entity_version`, `none`. |
| `supports_readback` | bool | Default `true`. |

### `MirrorReceipt`

| Field | Type | Constraint |
|---|---|---|
| `receipt_id`, `artifact_id` | str | Required. |
| `tenant_id`, `actor_id`, `actor_kind`, `user_id` | inherited from `Ownership` | Required, all four. SUBJECT-BOUND: `user_id` required, non-blank, regardless of writer. See [tenant.md](tenant.md#ownership). |
| `artifact_version` | int | Required. |
| `adapter_id`, `target_id` | str | Required. |
| `state` | `MirrorState` | Required. |
| `idempotency_key` | str | Required. Unique with `tenant_id` and `user_id`. |
| `attempted_at` | datetime | Required. |
| `expected_prior_checksum` | str | Required. The compare-and-set precondition. |
| `attempted_checksum` | str | Required. |
| `readback_checksum` | str | Default empty. **Required non-empty and equal to `attempted_checksum` when `settled`.** |
| `settled_at` | datetime or null | Non-null only when `settled`. |
| `reason_code` | str | Default empty. |
| `created_revision_id` | str | Default empty. Used by append-only targets. |
| `prior_receipt_ids` | tuple of str | Default empty. |
| `prior_attempt_state` | str | Default empty. The state the receipt named first in `prior_receipt_ids` settled into. Present only when there is a prior attempt. |
| `resolution_ref` | str | Default empty. **Required** whenever this receipt claims `settled` or `writing` and `prior_attempt_state` is `conflict` or `unknown`: a recorded human (or otherwise out-of-band) resolution reference. Its absence on such a receipt is rejected as an automatic retry. |

## Write mode is provider-gated

Compare-and-set safety is provider-specific and most providers do not offer it.
A client-side read-compare-write is **not** an atomic conditional write: a human
edit landing between the pre-read and the write is lost, and the adapter then
reads back its own content and calls it settled.

| Provider class | Default write mode | Requires |
|---|---|---|
| Proven atomic conditional write | `overwrite_compare_and_set` | `proves_atomic_conditional_write: true` |
| Git-backed target | `overwrite_compare_and_set` | The commit parent as the enforced precondition |
| Everything else | `append_only_revision` or `human_conflict_resolution` | Overwrite-in-place is not available |

Provider capability decides whether overwrite is even offered. It never decides
whether `conflict` and `unknown` block retries: those are unconditional.

## Invariants

1. `overwrite_compare_and_set` REQUIRES `proves_atomic_conditional_write`. A
   fixture asserts the rejection.
2. `settled` requires `readback_checksum == attempted_checksum`, both non-empty,
   plus a `settled_at`. **A write acknowledgement alone never settles a mirror.**
3. `conflict` and `unknown` never carry `settled_at`, and never trigger an
   automatic retry. Enforced across attempts, not just within one: a receipt
   whose `prior_receipt_ids` names a receipt that settled into `conflict` or
   `unknown` may not itself settle into `settled` or `writing` without a
   recorded `resolution_ref`. A fixture proves the rejection when the same
   idempotency key is retried straight through a conflict with no resolution
   recorded.
4. Every write uses compare-and-set against BOTH the target id and the last
   acknowledged checksum.
5. The stored idempotency identity is `(tenant_id, user_id, idempotency_key)`.
   A client derives the key stably per artifact, version, adapter, and target.
   Two users in one tenant may reuse the same key text, while the same triple
   is rejected by the named database constraint
   `mirror_receipts_tenant_user_idempotency_key_key`.
6. Every settled receipt is enumerable by artifact, because a deletion has to
   walk them: an unretractable external copy is what turns a deletion receipt
   from settled to partial (see [receipt.md](receipt.md)).

## Freeze notes

- **2026-09-02, ownership.** `MirrorReceipt` inherits the four `Ownership`
  fields. `MirrorAdapterDescriptor` does not: it is adapter configuration, not
  a private record, and carries no tenant. A permanent seeded fixture
  (`invalid-mirror-receipt-missing-user-id-key.json`) proves that omitting the
  `user_id` KEY is corrupt rather than read as "acts for no human".

- **2026-09-02, subject attribution.** `MirrorReceipt` is SUBJECT-BOUND. It
  records an external copy of one human's artifact, so it must be enumerable per
  person for a deletion receipt to be provably complete; `user_id` is required
  non-blank even under a system writer. Its `artifact_id` link is now a
  composite `(artifact_id, tenant_id)` foreign key, so a receipt cannot name an
  artifact in another tenant.

- **2026-09-02, idempotency identity.** The migration enforces one mirror
  receipt per `(tenant_id, user_id, idempotency_key)`. There is no mirror write
  path in `LedgerStore` today, so this round adds no in-memory behavior that
  could pretend to prove a write surface which does not exist.

- The plan's mirror state list is exactly the five states above. No `retrying`
  or `queued` state was added: a retry is a new attempt with its own receipt,
  which keeps the audit trail honest about how many times we wrote.
- `created_revision_id` was added for append-only targets. Without it, such an
  adapter would have to claim it replaced content it actually appended beside.
- `precondition_kind` is a named string rather than an enum. The set of
  precondition mechanisms will grow with adapters, and an unknown value grants
  nothing because the boolean gate is what the invariant reads.
- Grade: C. No mirror adapter exists in the repository today.
