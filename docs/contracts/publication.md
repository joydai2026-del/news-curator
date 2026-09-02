# Publication contract

Typed definition: `curator/contracts/publication.py`
Fixtures: `tests/fixtures/contracts/publication/`
Freezes: plan section "All publishing adapters share ..." and the publication
identity ruling. Criteria: SC-30, SC-37.

## Purpose

At-most-once publishing. Every publishing adapter uses the same seven states,
authorization is immutable and bound to exactly what was approved, and an
ambiguous provider response can never license a fresh send.

## State machine (frozen)

```
draft ──► ready ──► authorized ──► publishing ──► settled
   ▲        ▲            │              ├──► failed-safe ──► ready
   └────────┘            │              └──► unknown
            └────────────┘
         (digest changed, or authorization expired)
```

Legal transitions, and no others:

| From | To | When |
|---|---|---|
| `draft` | `ready` | Eligibility met. |
| `ready` | `draft` | No longer eligible. |
| `ready` | `authorized` | An explicit, unexpired authorization was granted. |
| `authorized` | `ready` | Digest changed, or authorization expired. |
| `authorized` | `publishing` | Execution started, under a separate credential. |
| `publishing` | `settled` | Acknowledged and read back. **Terminal.** |
| `publishing` | `failed-safe` | Failed with certainty that nothing was published. |
| `publishing` | `unknown` | **Ambiguous.** Blocks a fresh publish until readback resolves it. |
| `failed-safe` | `ready` | Retry the whole approval path. |
| `unknown` | `settled` | A positive readback proved the content is at the destination. Requires `readback_verdict: positive`. |
| `unknown` | `failed-safe` | A conclusive negative readback proved nothing landed. Requires `readback_verdict: negative_conclusive`. |

`settled` is the only terminal state. `unknown` is not terminal but is not
retryable either: it is resolved by reading the destination, never by sending
again. There is still no `unknown -> publishing` edge, so an ambiguous send
can never be silently retried; the ONLY two ways out are the readback-gated
edges above, and both require the readback verdict recorded on the record
itself (`PublicationRecord.readback_verdict`), never a timer.

## The identity ruling, and why it is not the digest

**Publication identity is keyed by tenant, publisher, destination, and issue
date ONLY. The content digest is deliberately excluded.**

The digest binds AUTHORIZATION. A changed digest invalidates the existing
authorization and returns the record to `ready`, so a new approval is required.
It must never mint a second terminal publication identity for the same issue
date.

Without that split: digest A publishes, the basket changes to digest B, a new
authorization produces a NEW idempotency key, and the same issue publishes
twice. The at-most-once guarantee dies quietly at the exact moment someone fixes
a typo.

## Records

### `PublicationIdentity`

| Field | Type | Constraint |
|---|---|---|
| `tenant_id`, `publisher_identity_ref`, `destination`, `issue_date` | str | Required. These four ARE the at-most-once key. |

### `PublicationAuthorization`

| Field | Type | Constraint |
|---|---|---|
| `authorization_id` | str | Required. |
| `identity` | `PublicationIdentity` | Required. |
| `content_digest` | str | Required. Binds the approval to exactly what was approved. |
| `policy_revision` | int | Required. |
| `approved_by_actor_id` | str | Required. |
| `approved_at`, `expires_at` | datetime | Required. |
| `approval_credential_id` | str | Default empty. Must never equal the executing credential. |

Immutable once written. A change produces a new authorization, never an edit.

### `PublicationRecord`

| Field | Type | Constraint |
|---|---|---|
| `identity` | `PublicationIdentity` | Required. |
| `state` | `PublicationState` | Required. |
| `updated_at` | datetime | Required. |
| `authorization_id` | str or null | **Non-null when `settled`.** |
| `content_digest` | str | Default empty. |
| `idempotency_key` | str | Default empty. Required when `publishing`. **Enforced derivation:** `pub-{tenant_id}\|{publisher_identity_ref}\|{destination}\|{issue_date}`, from the identity alone. A key containing the digest is rejected; a fixture asserts it. |
| `attempt` | int | Default 0. |
| `settled_receipt_id` | str | Default empty. |
| `reason_code` | str | Default empty. |
| `prior_state` | str | Default empty. The state this record transitioned FROM. Empty skips transition validation; present, it must name a legal `PUBLICATION_TRANSITIONS` edge. |
| `readback_verdict` | str | Default empty. One of `ReadbackVerdict`'s wire values. Required to leave `unknown`. |

### `PublishingBasket`

| Field | Type | Constraint |
|---|---|---|
| `basket_id`, `tenant_id`, `destination`, `issue_date` | str | Required. |
| `item_story_ids` | tuple of str | Required. |
| `required_item_count` | int | Required. |
| `content_digest` | str | Required. |
| `state` | `PublicationState` | Required. **`draft` or `ready` only.** Every state past `ready` (`authorized`, `publishing`, `settled`, `failed-safe`, `unknown`) belongs to `PublicationRecord`; a basket in one of those states would let a container of items impersonate a publication record. |
| `updated_at` | datetime | Required. |
| `readiness_signal` | bool | Default `false`. |
| `revision` | int | Default 0. Compare-and-set on basket mutation. |
| `item_artifact_ids` | tuple of str | Default empty. |

## Invariants

1. **Readiness is a signal. It authorizes nothing.** Reaching the configured
   item count sets `readiness_signal` and moves the basket to `ready`. Scheduling
   still requires an explicit unexpired authorization bound to the current digest
   and policy revision.
2. A basket is `draft` or `ready` at most, never `authorized`, `publishing`,
   `settled`, `failed-safe`, or `unknown`. Authorization and every state past
   it lives on the publication record, not on the container of items.
3. `state: settled` requires a non-null `authorization_id`. A settled
   publication with nothing behind it is the unapproved send this machine exists
   to make impossible.
4. `state: publishing` requires an idempotency key.
5. A changed digest or an expired authorization returns the record to `ready`
   and **leaves the idempotency key unchanged**.
6. `unknown` blocks a fresh publish. Resolution is a readback: `unknown ->
   settled` requires `readback_verdict: positive`, `unknown -> failed-safe`
   requires `readback_verdict: negative_conclusive`. An `unknown` record with
   an inconclusive or absent verdict stays `unknown`; there is still no
   `unknown -> publishing` edge.
7. `publish:approve` and `publish:execute` are different credentials. See
   [authorization.md](authorization.md).
8. A fixture that changes the digest, re-authorizes, and retries publishes at
   most once for that issue date: `valid-record-digest-changed-back-to-ready.json`
   (digest B invalidates the old authorization, key unchanged) paired with
   `valid-record-republished-same-key-after-reauthorization.json` (the retry
   under the new authorization, same `idempotency_key`) prove the identity
   never mints a second key across the whole cycle.

## Freeze notes

- `failed-safe -> ready` is included as a legal transition. The plan lists
  `failed-safe` as a state but does not say what follows it; returning to `ready`
  (and therefore requiring a fresh authorization) is the smallest choice
  consistent with "authorization is immutable and bound to a digest".
- `unknown` has exactly two outgoing transitions, both gated by a readback
  verdict recorded on the record: `settled` (positive) and `failed-safe`
  (negative_conclusive). An earlier draft of this freeze gave `unknown` zero
  outgoing edges "so it can never auto-retry", which instead parked an
  ambiguous send forever (no edge to `settled`, none to `failed-safe`): the
  fix is gating the edge on evidence, not deleting it. There is still no
  `unknown -> publishing` edge, so an ambiguous send can never be silently
  retried.
- `issue_date` is a string date, not a timestamp. Publication identity is per
  issue, and a timestamp would let two runs on the same day disagree about
  whether they are the same publication.
- Grade: C. No publishing state machine exists in the repository today.
