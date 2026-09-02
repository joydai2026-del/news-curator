# Candidate contract

Typed definition: `curator/contracts/candidate.py`
Fixtures: `tests/fixtures/contracts/candidate/`
Freezes: plan section "Candidate generation and slate assembly" and "Transparent
ranking policy". Criteria: SC-08, SC-08A, SC-09, SC-10, SC-24, SC-25.
The ranking receipt itself is in [receipt.md](receipt.md).

## Purpose

Four lanes generate candidates independently, one merge step deduplicates them
into canonical stories, the transparent scorer produces an explainable score
set, and a constrained assembler builds the edition. A shadow learned reranker
may run beside all of it and can never touch the live order.

Lanes are independent of topics. A topic filter narrows the active lane; it does
not select the lane.

## Pipeline order (frozen)

```
lane generators -> candidate merge -> transparent scorer
                                   -> [shadow reranker, non-authoritative]
                -> constrained slate assembly -> final invariant verifier -> settle
```

Only the verifier permits settlement, and it runs after EITHER scorer.

## Records

### `LaneCandidate`

| Field | Type | Constraint |
|---|---|---|
| `run_id`, `tenant_id` | str | Required. |
| `lane` | `Lane` | Required. |
| `story_id`, `story_cluster_id` | str | Required. |
| `lane_score` | float | Required. Persisted, because SC-24 replays from it. |
| `reason` | str | Required. |
| `generator_version` | str | Required. |
| `evidence_ids` | tuple of str | Default empty. |

A generator proposes. It never assigns a primary lane, so it cannot award itself
quota.

### `MergedCandidate`

| Field | Type | Constraint |
|---|---|---|
| `run_id`, `tenant_id`, `story_id`, `story_cluster_id` | str | Required. |
| `primary_lane` | `Lane` | Required. **Must appear in `lane_scores`.** |
| `lane_scores` | tuple of (Lane, float) | Required. Every lane the story qualified for. |
| `lane_reasons` | tuple of (Lane, str) | Required. |
| `tie_break_applied` | str | Required. Which rule decided, persisted for replay. |
| `secondary_lanes` | tuple of `Lane` | Default empty. Each must appear in `lane_scores`. |

### `StoryRecord`

The consolidated story cluster (`story_records`, plan "Core records"). One row
per canonical story, independent of any single lane assignment or edition.

| Field | Type | Constraint |
|---|---|---|
| `story_cluster_id`, `tenant_id` | str | Required. |
| `publication_class` | `PublicationClass` | Required. |
| `canonical_source_document_id` | str | Required. |
| `source_document_ids` | tuple of str | Required. |
| `created_at`, `updated_at` | datetime | Required. |
| `topic_tags` | tuple of str | Default empty. |
| `synthesis_evidence_ids` | tuple of str | Default empty. |

### `ComponentScores` and `ScoredCandidate`

`ComponentScores` carries all eight components plus `final_score`:
`relevance`, `freshness`, `trend`, `editor_consensus`, `deliberate_surprise`,
`diversity`, `repetition_penalty`, `source_fatigue_penalty`. A disabled
component records 0.0 here, and the policy records the disablement explicitly.

Each value is the **weighted contribution**: the scorer's own 0.0-1.0
normalized value multiplied by that component's weight in
`config/ranking-policy-r1.yaml`. Two consequences, both checked (invariant 11):
a contribution can never exceed its ceiling of `weight * cap`, and
`final_score` is the signed composition of the eight, positives minus
penalties. A penalty is stored as a positive magnitude and subtracted, never
as a negative number.

`ScoredCandidate` adds `run_id`, `story_id`, `scorer_kind`, `scorer_version`,
`authoritative`, and `plain_reason`.

### `BandResult`, `SlateEntry`, `Slate`

`BandResult`: `band`, `active`, `floor` (nullable), `cap` (nullable),
`achieved` (nullable), `verdict` (`pass` / `fail` / `disabled`),
`exception_reason`.

`SlateEntry`: `position`, `story_id`, `primary_lane`, `final_score`,
`plain_reason`, `backfilled`.

`Slate`: `run_id`, `tenant_id`, `edition_date`, `built_at`, `policy_revision`,
`profile_snapshot_id` (nullable before imports), `entries`, `bands`,
`verifier_verdict`, `lane_quotas`, `short_reason_code` (default empty;
required whenever the entry count is below the summed quotas).

## Lane overlap, the rule this contract exists for

A story can qualify for several lanes but appears **at most once** in a slate.

1. Primary lane = highest **lane priority** first, then highest `lane_score`.
   Priority is configured in `config/ranking-policy-r1.yaml` as
   `[updates, hot, interested, surprise]`. **Executable, not merely
   documented:** `tests/test_contract_freeze.py`'s `_primary_lane_from_tiebreak`
   recomputes the winner from `lane_scores` and the configured priority, and
   rejects a `MergedCandidate` or `RankingReceipt` naming any other qualified
   lane as primary. Revision 1's priority is a strict permutation over the
   four lanes, so level 1 always resolves alone in production; level 2
   (`lane_score`) is still frozen and independently tested for a future
   non-strict priority. The tie_break list's third level, stable `story_id`,
   cannot break a tie between LANES for one story (`story_id` is a single
   constant value for the whole candidate); it is enforced instead where it
   is reachable, ordering same-lane, same-score entries inside one settled
   slate (invariant 10, below).
2. **Only the primary lane consumes quota.**
3. Secondary lane reasons stay visible in the receipt and in the explanation.
4. When dedupe or the verifier rejects an item, deterministic backfill takes the
   next eligible story from **that same primary lane's** remaining ranked pool.
   Cross-lane borrowing is disabled, so a short lane ships short rather than
   misreporting the lane mix.
5. The assignment must replay **from the persisted receipt alone**: stored
   `lane_score` values, lane priority, and story id, with no generator rerun.

The frozen fixture
`tests/fixtures/contracts/candidate/valid-merged-candidate-overlap-updates-hot.json`
is the case that makes this checkable: one story qualifies for Updates AND Hot,
Hot scores HIGHER (0.81 against 0.74), and Updates is still primary because lane
priority is the first tie-break. If a later change made score the first
tie-break, that fixture goes red.

## Invariants

1. A story appears at most once per slate, and slate positions are `1..n` with
   no gaps.
2. `primary_lane` must appear in `lane_scores`, otherwise the receipt cannot
   replay it.
3. **Only `ScorerKind.TRANSPARENT` may be authoritative.** A shadow score set
   marked authoritative is a contract violation, and a fixture asserts it.
4. **Lane quotas are CAPS, not exact counts.** The settled entry count must
   never exceed the summed lane quotas, and no lane exceeds its own quota, but
   an edition below the summed quotas is legal exactly when a lane's ranked
   pool is exhausted (`allow_cross_lane_borrow: false`) -- and it must record
   why via `short_reason_code`, so a short edition is recorded rather than
   inferred from a mismatch. An earlier draft of this invariant required exact
   equality, which made the policy's own documented short-edition behavior a
   contract violation.
5. A slate with any FAILING active band cannot carry a passing verifier verdict.
   An active edition fails closed when a band is missed; it cannot be published
   or described as balanced.
6. A `disabled` band verdict is legal only when the policy carries a recorded,
   versioned exception for that band. Omission is never a disable.
7. Exploration candidates must clear the configured freshness, source-quality,
   and consensus gates. Random low-quality content is not exploration.
8. Repetition control clusters by `story_cluster_id`, never by raw URL.
9. Every displayed story stores component scores and emits one plain-language
   reason. When no profile snapshot exists, the reason must say so rather than
   implying personalization.
10. **Bands are recomputed, never self-certified.** All seven SC-20 bands must
    be present on every settled `Slate` and `RankingReceipt` (an empty or
    partial list is rejected), and every active band's `verdict` must equal
    what its own `achieved` value says against `floor`/`cap` -- the receipt
    cannot simply assert `pass`. Within one lane, entries must be ordered by
    `final_score` descending then `story_id` ascending (the tie_break list's
    third level, made reachable here).
11. **`final_score` is composed, never asserted.** Every persisted
    `ComponentScores` set, shadow ones included, must satisfy
    `final_score == (relevance + freshness + trend + editor_consensus +
    deliberate_surprise + diversity) - (repetition_penalty +
    source_fatigue_penalty)` within a rounding tolerance, and every component
    must sit within `[0, weight * cap]` read from policy revision 1. Without
    this, a transparent and authoritative score set could carry eight zeroed
    components under a `final_score` of 0.99, and that number is what the
    ranking receipt replays the whole edition from.

## Freeze notes

- The plan gives the scoring formula but no numbers. Concrete initial values for
  every component weight and every band are in policy revision 1, derived from
  the plan's rules and the measured route volumes; the rationale for each number
  is an inline comment there.
- The plan does not say whether `ComponentScores` stores the raw normalized
  value or the weighted contribution. The freeze reads it as the WEIGHTED
  contribution, which is what revision 1's own fixtures already compose and
  what keeps every stored component under its `weight * cap` ceiling. Recorded
  because it is a silence-filling choice, and because it is what makes
  invariant 11 checkable from policy instead of by convention.
- `tie_break_applied` is an added field. The plan defines the tie-break order in
  prose; persisting which rule fired is what turns SC-24's replay requirement
  into a one-line assertion instead of a reimplementation.
- `profile_snapshot_id` is nullable. Before any import there is no profile, and
  the plan explicitly requires the UI to disclose that state rather than fake it.
- `editor_consensus` appears in the formula but not in SC-20's seven bands. It
  is therefore a weighted COMPONENT with no band, which is what the plan's own
  lists say. Not an oversight.
- Grade: C. No lane generator, merge step, or slate assembler exists in the
  repository today.
