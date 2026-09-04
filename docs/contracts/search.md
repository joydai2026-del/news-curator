# Search contract

Typed definition: `curator/contracts/search.py`
Fixtures: `tests/fixtures/contracts/search/`
Freezes: plan section "Search contract". Criteria: SC-35.

## Purpose

One tenant-scoped query contract spanning authorized normalized stories and
knowledge artifacts, returning identical IDs and identical ordering on the web
surface and the authenticated API or CLI for the same principal, query, filters,
index version, and policy revision.

The failure this contract is built against: a search that returns an empty list
when the index is down, so a broken surface looks like a quiet one.

## Records

### `SearchQuery`

| Field | Type | Constraint |
|---|---|---|
| `tenant_id`, `principal_id` | str | Required. |
| `text` | str | Required. Treated as data, never as an instruction. |
| `index_version` | str | **Required in the request**, not ambient. |
| `policy_revision` | int | **Required in the request.** |
| `limit` | int | Required. |
| `offset` | int | Default 0. |
| `classes` | tuple of `SearchResultClass` | Default empty, meaning all authorized classes. |
| `topic_tags`, `source_ids` | tuple of str | Default empty. |

Index version and policy revision are part of the request because parity is
otherwise untestable: two surfaces reading different index generations would
disagree for a legitimate reason, and the test could not tell that from a bug.

### `SearchResult`

| Field | Type | Constraint |
|---|---|---|
| `result_class` | `SearchResultClass` | Required. `story` or `artifact`. |
| `canonical_id` | str | Required. |
| `tenant_id` | str | Required. |
| `publication_class` | `PublicationClass` | Required. The authorization projection this hit came from. |
| `title` | str | Required. |
| `match_reason` | str | Required. Why this hit matched, in plain language. |
| `ordering_key` | str | Required. Makes ties stable across surfaces. |
| `provenance_ref` | str | Required. |
| `score` | float | Required. |

### `SearchResponse`

| Field | Type | Constraint |
|---|---|---|
| `outcome` | `SearchOutcome` | Required. `ok` or `error`. |
| `index_version` | str | Required. Echoed, so a caller can prove which generation answered. |
| `policy_revision` | int | Required. |
| `total_matched` | int | Required. |
| `results` | tuple of `SearchResult` | Default empty. |
| `error_code` | str | Default empty. |

## Invariants

1. **An empty result is a SUCCESS.** `outcome: ok` with zero results is a
   legitimate empty state.
2. **A failure is never rendered as empty.** `outcome: error` REQUIRES a
   non-empty `error_code`; `outcome: ok` requires an empty one. Index,
   authorization, and provider failures are explicit errors.
3. Web and authenticated API or CLI return identical IDs and identical ordering
   for the same principal, query, filters, index version, and policy revision.
4. Parity alone is not sufficient. Frozen POSITIVE fixtures assert expected
   story hits, expected artifact hits, expected exclusions, stable tie ordering,
   and index-update behavior after a new document lands, so two identical wrong
   or always-empty result sets cannot satisfy the criterion.
5. Results are tenant-scoped before ranking, not filtered afterwards. Public
   search can query only explicitly public projections.
6. `ordering_key` is the sort authority. A surface that re-sorts by score alone
   will disagree on ties, which is exactly the class of bug parity testing
   exists to catch.

## Freeze notes

- The plan says results include "artifact class, canonical ID, provenance,
  authorization projection, match reason, and stable ordering key". Those are
  the six required fields above; `score` and `title` were added because a result
  list is unusable without them and neither weakens a criterion.
- Positive-result fixtures are named as a requirement here but are a phase-8
  deliverable: they need a real index, which does not exist yet. This freeze
  supplies the query and response SHAPES those fixtures will be written against.
- Grade: C. No search index exists in the repository today.
