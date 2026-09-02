# Source plugin contract

Typed definition: `curator/contracts/source_plugin.py`
Fixtures: `tests/fixtures/contracts/source-plugin/`
Freezes: plan section "Source plugin contract", including its recorded
supersession note and the Gate 0c gap map. Criteria: SC-23.

## Purpose

One contract for every input: feeds, sitemaps, web observers, newsletters, trend
sources, social sources, future agents, and historical imports. Candidate
generators and ranking read normalized records and never branch on a provider
name.

## This EXTENDS the live adapter. It does not replace it.

**Grade B, read from source at commit `ef9a855`.** `curator/sources/base.py`
defines a two-member `SourceAdapter` Protocol: `validate_options(spec)` and
`fetch(spec, context) -> SourceResult`, with `type_key` as the registry key.
Health rides inside `SourceResult` as a required field with an enforced id
invariant. That contract is **not superseded**. `SourcePlugin` restates those
three members unchanged and adds four more.

The plan records this as a deliberate extension of the `SourceAdapter` contract
frozen by the 2026-08-29 backend platform plan, which also locked "common policy
is deliberately small". If the extension turns out to break that plan's
guarantees, the conflict is escalated as a decision, never resolved by quietly
dropping either document.

| Capability | Status today | This freeze adds |
|---|---|---|
| `fetch` / `validate_options` | **Live.** Failure containment, parallel fan-out with stable ordering, per-spec transport narrowing, disabled-route short circuit. | Nothing. Restated verbatim. |
| `health` | **Live**, per-run. Seven-value status vocabulary, stable reason codes, no URL and no raw exception text in the record. | `SourceHealthRecord`: a durable cross-run record with `consecutive_failures` and `last_success_at`. |
| `normalize` | **Live but inlined** per adapter. Bounded text, item caps, canonical URL handling. | A declared contract member and one target schema (`NormalizedSourceDocument`), so two adapters cannot silently diverge. |
| `provenance` | **Partial.** Per-item source id, name, platform, weight, aggregator and echo flags, estimated-time flag; run-level configuration and content digests. | `fetched_at` (when we SAW it, which no item records today), `adapter_version`, `raw_response_digest`. |
| `capabilities` | **Implicit only.** Option-key allowlists and registry introspection. | `SourceCapabilities`, an explicit descriptor. |
| `discover` / `poll` | **Partial.** Every route is hand-authored config; no conditional GET anywhere; one global cycle. | A declared `discover` member taking a checkpoint. |
| `checkpoint` | **GREENFIELD. Nothing implements it.** The only hook is an inert `SourceContext.durable_store` field that is declared and never read or written. | `SourceCheckpoint`, entirely new. |

**Migration note on checkpoint.** There is no durable cursor anywhere in the
source layer. The nearest in-repo precedent is the newsletter lane's watermark,
which advances only after a successful publish and persists to a state file;
that advance-after-settlement shape is the pattern to borrow. Because checkpoint
is greenfield, every route's first written checkpoint is `uninitialized` with an
empty cursor rather than a fabricated one. This is the largest of the six gaps
and should drive sequencing.

## Records

### `SourceCapabilities`

| Field | Type | Constraint |
|---|---|---|
| `plugin_id`, `plugin_version` | str | Required. |
| `supports_poll`, `supports_push`, `supports_full_text`, `supports_trend_signal`, `supports_social_signal`, `supports_deletion`, `supports_incremental_checkpoint`, `consumes_search_queries` | bool | All required. No default, so no capability is ever assumed. |
| `languages` | tuple of str | Default empty. |

`consumes_search_queries` exists to close a measured, undeclared behavioural
split: the runtime hands search queries to every route, and exactly one adapter
consumes them while the rest silently ignore them.

### `SourceCheckpoint`

| Field | Type | Constraint |
|---|---|---|
| `plugin_id`, `source_id`, `tenant_id` | str | Required. |
| `state` | `CheckpointState` | Required. `uninitialized`, `advancing`, `settled`, `blocked`. |
| `cursor` | str | Required, may be empty only while `uninitialized`. |
| `watermark` | datetime or null | Null only while `uninitialized`. |
| `last_settled_run_id` | str | Required, may be empty while `uninitialized`. |
| `health_receipt_id` | str | Required, like `cursor`: present but may be empty until `settled`. The durable cross-run `SourceHealthRecord` this checkpoint's settlement is proven against (plan "Core records": `source_checkpoints` carries a health receipt reference). |
| `updated_at` | datetime | Required. |
| `etag`, `last_modified` | str | Default empty. Conditional-GET state, absent from the live collector. |
| `consecutive_failures` | int | Default 0. |
| `backoff_until` | datetime or null | Default null. |

### `SourcePluginRegistration`

The registry row (`source_plugins`, plan "Core records"). A plugin is never
implicitly enabled: `REGISTERED` means the row exists and nothing polls it
yet, only `ENABLED` may be polled.

| Field | Type | Constraint |
|---|---|---|
| `plugin_id`, `plugin_version`, `tenant_id`, `config_reference` | str | Required. |
| `capabilities` | `SourceCapabilities` | Required. Embedded, so a registry row and its declared capabilities cannot drift apart. |
| `state` | `PluginState` | Required. `registered`, `enabled`, `disabled`, `retired`. |
| `registered_at` | datetime | Required. |

### `NormalizedSourceDocument` and `SourceProvenance`

| Field | Type | Constraint |
|---|---|---|
| `document_id`, `tenant_id`, `title`, `url`, `canonical_url`, `language` | str | Required. |
| `provenance` | `SourceProvenance` | **Required.** A document with no provenance cannot be attributed, corrected, or deleted. |
| `summary` | str | Default empty. The SOURCE's own summary, bounded. Never generated. |
| `image_url` | str | Default empty. |
| `native_rank`, `native_score` | int or null | Source-local only. Never comparable across sources. |
| `topic_tags` | tuple of str | Default empty. |

`SourceProvenance` requires `source_id`, `plugin_id`, `adapter_version`,
`original_item_id`, `url`, `canonical_url`, `fetched_at`, `published_at` (nullable),
and `transform_version`, plus `published_at_is_estimated`, `echo_eligible`, and
an optional `raw_response_digest`.

### `SourceHealthRecord`

Requires `source_id`, `plugin_id`, `status` (`HealthStatus`), `usable_items`,
`newest_item_age_hours` (nullable), `max_age_hours`, `observed_at`; plus
`reason_code`, `consecutive_failures`, `last_success_at`.

### `SourceRights` (DECLARED, DEFERRED)

Gate 0c disposition D4. Syndication terms live today as prose comments in
configuration that no code can read. That is safe only while routes are added by
hand. The field is frozen now so it is not discovered late, and is populated and
enforced **when discover/poll is built, not before**, because programmatic route
discovery is what makes machine-readable terms load-bearing.

| Field | Type | Constraint |
|---|---|---|
| `terms_id` | str | Required. A stable identifier, never free text. |
| `verified` | bool | Required. True only when a human recorded terms for this exact route. |
| `deferred` | bool | Default `true`. While true, no gate reads this record. |
| `public_projection_eligible` | bool or null | Null while deferred. |
| `verbatim_quote_eligible` | bool or null | Null while deferred. Verbatim quoted commentary is not public-eligible without separate licensing. |

## Invariants

1. A checkpoint advances only after the durable normalized writes for that batch
   settle. A `blocked` checkpoint is never skipped forward; the next poll resumes
   from the last settled cursor, so a resume neither replays nor silently skips.
2. `uninitialized` carries no cursor and no watermark. `settled` requires a
   cursor AND a `health_receipt_id`.
3. Every normalized document carries provenance. No exceptions, including for
   imported history.
4. Adding or removing a plugin changes registry configuration and adapter code
   only. It cannot require a candidate-generator or ranking change (SC-23).
5. No core field names a provider. A vendor name in a contract field is a
   contract violation, and a fixture asserts it.
6. Health status stays within the frozen seven-value vocabulary the live
   collector already emits.

## Freeze notes

- The plan lists `discover` or `poll` as one capability. `SourcePlugin` declares
  `discover(spec, context, checkpoint)` only. A separate `poll` member would be
  the same call with a different name; the checkpoint argument is what makes it
  incremental.
- `advance_checkpoint(checkpoint, settled_documents)` is named explicitly rather
  than left as "the checkpoint capability", so the advance-after-settlement rule
  has a signature to hang on.
- `raw_response_digest` is optional, not required. Requiring it would force raw
  response retention for all 75 live routes, which is a storage decision this
  freeze has no measurement to justify.
- Dispositions D1 (a configured route family with no adapter), D2 (three
  registered adapters serving zero routes), and D3 (four dead or empty routes)
  are open decisions for the owner and are NOT resolved here. None of them
  changes a contract shape; they change which routes exist.
- Grade: B for every "what exists today" statement, each traceable to a cited
  file in the Gate 0c receipt. C for the additive layer, which is unimplemented
  by definition.
