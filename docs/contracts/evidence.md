# Evidence contract

Typed definition: `curator/contracts/evidence.py`
Fixtures: `tests/fixtures/contracts/evidence/`
Freezes: plan sections "Core records" (`raw_imports`, `evidence_items`,
`profile_snapshots`), "Data-source policy", and "Profile update path".
Criteria: SC-02, SC-03, SC-04, SC-06, SC-21.

## Purpose

Raw imported bytes and normalized evidence are two different things, stored
separately. An import writes bytes and changes no profile state. Only normalized,
provenance-linked, policy-weighted evidence rows feed a snapshot, and only a
settled snapshot feeds ranking.

## Records

### `RawImport`

| Field | Type | Constraint |
|---|---|---|
| `raw_import_id` | str | Required. |
| `tenant_id`, `owner_actor_id` | str | Required. |
| `source_kind` | str | Required. A KIND, never a provider brand: `newsletter_archive`, `assistant_chat_export`, `browser_history`, `mailbox_state`, `url_list`. |
| `checksum` | str | **Required.** Idempotency has nothing to key on without it. |
| `schema_version` | str | Required. |
| `storage_reference` | str | Required. Restricted storage, never a public path. |
| `imported_at` | datetime | Required. |
| `consent_version` | str | Required. Per-source consent, recorded at import time. |
| `retention_state` | `RetentionState` | Required. `active`, `retracted`, `purged`. |
| `exported_at` | datetime or null | Recorded when the export tool supplies it. |
| `byte_size` | int | Default 0. |

### `EvidenceItem`

| Field | Type | Constraint |
|---|---|---|
| `evidence_id` | str | Required. |
| `tenant_id` | str | Required. |
| `raw_import_id` | str or null | Null only for live evidence with no import behind it. |
| `source_item_id` | str | Required. With `checksum` this is what makes re-import produce zero duplicates. |
| `occurred_at`, `recorded_at` | datetime | Required. When it happened, and when we learned of it. |
| `evidence_class` | `EvidenceClass` | Required. Exactly `observed`, `inferred`, `explicit`, `passive`. |
| `origin` | `EvidenceOrigin` | Required. `live` or `imported`. A SEPARATE axis. |
| `confidence` | `ConfidenceBand` | Required. `strong`, `medium`, `weak`. |
| `weight` | float | Required. Read from the active policy revision and persisted, so past snapshots replay after the policy changes. |
| `policy_revision` | int | Required. |
| `story_id` | str or null | |
| `canonical_url` | str | Default empty. |
| `entity_ids`, `topic_tags` | tuple of str | Default empty. |
| `corroborated` | bool | Default `false`. Never set at import time. |
| `corroborating_evidence_ids` | tuple of str | Default empty. Names the rows that promoted this one. **`corroborated: true` requires at least one entry here**; an unattributed promotion is rejected, and a fixture asserts it. |
| `retracted_by_event_id` | str or null | Set by a correction. The row itself is never edited. |

### `ProfileSnapshot`

| Field | Type | Constraint |
|---|---|---|
| `snapshot_id`, `tenant_id` | str | Required. |
| `version` | int | Required, monotonic. |
| `evidence_watermark` | datetime | Required. |
| `build_version` | str | Required. |
| `policy_revision` | int | Required. |
| `settled_at` | datetime | **Required, non-null.** An unsettled build is not a snapshot row at all. |
| `topic_affinities`, `entity_affinities`, `source_affinities` | tuple of (feature_id, weight, provenance) | Default empty. **Weighted entries, not bare strings**: a bare string can record THAT a topic is an affinity and nothing about how strongly, so `less_like_this` and `more_like_this` would land in the same place and decay would have no numeric input to read. `provenance` is a stable reference (an evidence id, or a comma-joined list) tracing the weight to the events that produced it. |
| `knowledge_gaps` | tuple of str | Default empty. Unweighted: the plan treats gap topics as a set, not a scored ranking. |
| `novelty_tolerance` | float | Default 0.0. |

## Invariants

1. **`EvidenceClass` has exactly four members.** Importedness is not a fifth
   class; it is the `origin` axis. Folding the two together would quietly widen
   SC-04, so a fixture asserts the rejection.
2. Import writes raw bytes and changes no profile state. Normalization is a
   separate step.
3. Re-importing the same `(tenant_id, source_kind, checksum)` and
   `source_item_id` creates zero duplicate evidence or events.
4. A weak imported row rises to medium ONLY through the deterministic
   corroboration policy, and the promoting rows are named in
   `corroborating_evidence_ids`. Promotion is never silent.
5. A partial or failed rebuild never replaces the last settled snapshot. This is
   structural: `settled_at` is non-nullable, so a half-built snapshot cannot be
   written at all.
6. Ranking reads exactly one settled snapshot per run.
7. Imported content is data, never instructions. Parsers reject executable
   payloads and never follow commands found in imported text.
8. A correction sets `retracted_by_event_id`; the original values stay
   queryable.

## Weak-evidence rules that this contract encodes

| Source | Ceiling | Why |
|---|---|---|
| Mailbox current label state | weak, exposure only | No documented per-open timestamp exists. It cannot create a read event or an `opened_at`, and a schema that accepts one is rejected. |
| Single browser visit | weak | Background tabs, redirects, work tasks, and shared devices. It never equals a save. |
| Newsletter archive selections | strong for positive selection | These are stories the owner actually chose, with framing and commentary. Absence is not dislike. |
| Assistant Q&A export | strong for curiosity and gaps | Filter to news-related threads before profile use. |

## Freeze notes

- The plan describes an evidence "class" in two different senses: SC-04's four
  lineage values, and the event table's "strong explicit / weak imported"
  strength language. Splitting them into `evidence_class`, `origin`, and
  `confidence` is the smallest change that satisfies both without contradicting
  SC-04's enumeration.
- `weight` is persisted on the row rather than looked up from policy at read
  time. Otherwise a policy change would silently rewrite the meaning of every
  historical snapshot, and "past editions remain reproducible" would be false.
- Retention is a three-value state, not a date. Dates live in the retention
  policy; the row records where it currently stands.
- Grade: C throughout. No evidence ledger exists in the repository today.
