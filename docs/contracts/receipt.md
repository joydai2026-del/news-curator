# Receipt contract

Typed definition: `curator/contracts/receipt.py`
Fixtures: `tests/fixtures/contracts/receipt/`
Freezes: plan records `ranking_receipts` and `deletion_receipts`, the host budget
tripwire receipt, and the import inventory receipt. Criteria: SC-05, SC-08,
SC-11A, SC-28, SC-40.
Mirror and output receipts live in [mirror.md](mirror.md) and
[output-adapter.md](output-adapter.md) and carry the same envelope fields.

## Purpose

A receipt is the durable proof that something happened, complete enough to
replay. The rule that shapes all of them: **missing or unresolved is `unknown`
or `partial`, never zero and never green.**

## `ReceiptEnvelope`

Every receipt in the system carries these.

| Field | Type | Constraint |
|---|---|---|
| `receipt_id`, `tenant_id`, `kind` | str | Required. |
| `state` | `ReceiptState` | Required. `settled`, `partial`, `failed`, `unknown`. |
| `created_at` | datetime | Required. |
| `policy_revision` | int | Required. |
| `actor_id` | str | Default empty. |
| `reason_code` | str | Default empty. |
| `settled_at` | datetime or null | Non-null only when `settled`. |

`partial` exists so a deletion that cannot resolve one derived projection can
never present as green.

## `RankingReceipt`

| Field | Type | Constraint |
|---|---|---|
| `envelope` | `ReceiptEnvelope` | Required. |
| `run_id`, `edition_date` | str | Required. |
| `profile_snapshot_id`, `profile_version` | str / int or null | Null before any import. |
| `pre_rank_candidate_ids` | tuple of str | Required. |
| `lane_scores` | tuple of (story_id, Lane, float) | Required. Every lane every story qualified for. |
| `lane_priority` | tuple of `Lane` | Required. The configured priority AS USED by this run. |
| `primary_lane_by_story` | tuple of (story_id, Lane) | Required. |
| `secondary_lane_reasons` | tuple of (story_id, Lane, str) | Required. |
| `transparent_scores` | tuple of `ScoredCandidate` | Required. |
| `final_order` | tuple of `SlateEntry` | Required. |
| `bands` | tuple of `BandResult` | Required. |
| `lane_quotas` | tuple of (Lane, int) | Required. |
| `verifier_verdict` | `BandVerdict` | Required. |
| `shadow_scores` | tuple of `ScoredCandidate` | Default empty. |

### Invariants

1. **A settled receipt REQUIRES at least one authoritative transparent score,
   AND every `final_order` entry's story requires one.** No settled run may
   have derived its live order from the shadow reranker, and no displayed
   story may be missing its own authoritative score. A fixture with an empty
   transparent set and a populated shadow set is rejected.
2. Every entry in `shadow_scores` has `authoritative: false`.
3. Every `primary_lane_by_story` pair must appear in `lane_scores`, so the
   assignment replays from the receipt alone: stored scores, `lane_priority`, and
   story id, with no generator rerun (SC-24). **Recomputed, not just checked
   for membership:** the validator derives the tie-break winner from
   `lane_scores` and `lane_priority` and rejects a receipt naming any other
   qualified lane as primary (see `candidate.md`'s tie-break section).
4. `lane_priority` is stored per run, not read from current config at replay
   time. A later policy change must not silently rewrite a past edition's
   explanation.
5. A settled receipt's `bands` must carry all seven SC-20 bands, each with its
   `verdict` recomputed from `achieved` against `floor`/`cap` -- an empty
   `bands` list, or a self-asserted `pass` that contradicts `achieved`, is
   rejected (shared with `Slate`; see `candidate.md` invariant 10).

## `DeletionReceipt`

| Field | Type | Constraint |
|---|---|---|
| `envelope` | `ReceiptEnvelope` | Required. |
| `target_kind`, `rebuild_id` | str | Required. |
| `target_ids`, `invalidated_snapshot_ids` | tuple of str | Required. |
| `correction_watermark` | datetime | Required. |
| `zero_contribution_verdict` | bool | Required. |
| `projections` | tuple of `ProjectionResolution` | Required. |
| `mirrored_targets` | tuple of str | Default empty. |
| `audit_chain_queryable` | bool | Default `true`. |

`ProjectionResolution`: `projection`, `resolved`, `resolution_kind`,
`target_ref`, `user_visible_disclosure`.

### The projections that must all resolve

| Projection | Required resolution |
|---|---|
| Profile snapshots and ranking | Rebuild proves zero contribution. |
| Search indexes | Index invalidation or removal receipt. |
| Knowledge artifacts quoting the evidence | Redaction or retraction receipt per artifact version. |
| Caches | Invalidation receipt. |
| Exports | Retraction record, or a user-visible disclosure that an export already left the system. |
| Public projections | Removal receipt from the public projection build. |
| Mirrors | Every settled mirror receipt derived from the deleted artifact is listed in `mirrored_targets` and reaches either a settled retraction write or an explicit user-visible "external copy retained at `<target>`" disclosure. |

### Invariants

1. **A receipt with ANY unresolved projection cannot settle.** It settles
   `partial` with the unresolved targets named to the user.
2. Every unresolved projection carries a user-visible disclosure. Silence is not
   an option.
3. The immutable audit chain stays queryable throughout: a settled receipt
   REQUIRES `audit_chain_queryable: true`. Deleting evidence never deletes the
   record that it was deleted.
4. A clean-looking receipt that leaves content sitting on an external target is
   a false deletion, and a fixture asserts the rejection. **Enforced beyond
   "no unresolved rows":** a settled receipt must enumerate all SEVEN
   projections by name (an empty or partial `projections` list settles green
   under the naive "no unresolved row" reading, because a row that is never
   listed is not "unresolved" -- this was the false-deletion hole several
   seeded payloads exploited), must carry `zero_contribution_verdict: true`,
   and every `mirrored_targets` entry must appear as a `mirrors` projection's
   `target_ref` (mirror reconciliation).

## `LimitReceipt` and `MeterReading`

One pilot day's host-budget record.

`LimitReceipt`: `envelope`, `meter_source`, `attributed_operation_class`,
`readings`, `shed_actions`, `final_state`.

`MeterReading`: `meter`, `meter_kind`, `value` (nullable), `unit`,
`freshness_verdict`, `sampled_at` (nullable), `warning_threshold`,
`hard_stop_threshold`, `breached`.

### The two meter kinds are not interchangeable

| `meter_kind` | Enforcement | Policy model |
|---|---|---|
| `cumulative_budget` | Consumed over a window, observable before exhaustion. | Programmable warning and hard-stop thresholds. Capacity pressure sheds noncritical enrichment and conversational work FIRST. Checkpoint settlement and required durable writes stay protected; if their safe completion cannot be guaranteed, intake pauses rather than losing state. |
| `per_invocation_ceiling` | Enforced by the runtime, which terminates the invocation. | **Shedding cannot rescue it.** The work must fit or be split. Warning thresholds are diagnostic only. |

### Invariants

1. A missing or stale meter has `value: null` and a `freshness_verdict` saying
   so. It is **never** recorded as zero.
2. A receipt whose meters cannot be read settles `unknown`, not `settled`.
3. Every reading names its `meter_kind`, so a shed policy can never be applied to
   a limit that shedding cannot rescue.

## `ImportInventoryReceipt`

| Field | Type | Constraint |
|---|---|---|
| `envelope` | `ReceiptEnvelope` | Required. |
| `source_kind` | str | Required. |
| `credential_verified` | bool | Required. |
| `coverage_window_start`, `coverage_window_end` | datetime or null | Null when unknown. |
| `sampled_record_count` | int | Required. |
| `available_fields`, `missing_fields` | tuple of str | Required. |
| `evidence_grade` | str | Required. A, B, or C. |
| `import_enabled` | bool | Default `false`. |

### Invariants

1. A failed or incomplete receipt keeps that source's evidence DISABLED. Absence
   of a receipt is a disable, not a default-on.
2. `import_enabled` defaults to `false`. Enabling is an act, never an omission.
3. A source whose inventory is missing, ungraded, or incomplete stays
   import-disabled. **Enforced:** `import_enabled: true` requires
   `envelope.state: settled`, `credential_verified: true`, a complete
   `coverage_window_start`/`coverage_window_end`, an empty `missing_fields`,
   and `evidence_grade` of `A` or `B`. A fixture asserts the rejection of an
   incomplete, credential-unverified inventory marked settled and enabled.

## Freeze notes

- `ReceiptEnvelope` is a shared struct rather than a base class, so a receipt
  type can be read without knowing an inheritance tree, and so the four
  settlement states are identical everywhere.
- `ranking_receipts` and `deletion_receipts` live here rather than in the
  candidate and evidence contracts because they share the envelope and the same
  "partial is not green" rule. Mirror and output receipts stay with their own
  contracts because their states are the mirror and publication state machines,
  not `ReceiptState`.
- `evidence_grade` is a plain string constrained by prose rather than an enum.
  It is a documentation quality marker, not a control-flow value.
- Grade: C for every receipt shape. B for the two meter kinds being genuinely
  different, which follows from the host's published limits.
