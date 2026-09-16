# Reader feedback verification

Status: local integration in progress. Production still serves the previous release. This record does not mark M2 relevance or coverage qualification complete.

## Changes under verification

The release checks below are requirements, not claims that live verification has passed.

| User feedback | Implementation | Release check |
|---|---|---|
| Mixed English and Chinese | Cached display translations, English default, persistent English/中文 choice, locale-bound ranking and cursors | Both directions, initial load, reload, search, categories, Saved, pending translations and request failures remain in the chosen language |
| Save has no confirmation | Immediate busy state, confirmed Saved indicator, existing durable state and rollback | Confirm, refresh, remove, conflict and failed request |
| China News is missing | Separate configured category, verified SCMP China feed plus matching shared feeds | Fresh China-related items from independent source families; language alone cannot qualify a story |
| Loading feels unresponsive | Immediate busy/disabled state, retained current cards on failure, bounded requests and restrained motion | First event-loop acknowledgment; measured deployed latency; no duplicate click requests or scroll loss |
| Backend behavior cannot be observed | Latest activity status plus aggregate request outcomes and latency | Actual owner action is recorded, next eligible model result uses it, and the monitor distinguishes model, fallback, failure and insufficient evidence |

## Existing evidence

- Independent backend review passed at `e94e195`. It covered translation dispatch bounds, shared spending limits, category scheduling and preservation of native newsletter projections.
- A disposable local PostgreSQL test at `4b3e37c` proved pre-send release, retained unknown-charge holds, exact usage settlement, idempotent replay, and rejection of a third call that would exceed the shared cap. This is a local protocol test, not a real provider call.
- The localized SQL verification passed nine local checks, including original/display search, missing translations, stale content digests, quarantine and access rules.
- The broad root regression process completed with six reader failures. A separate collection-only command counted 2,615 selected tests; it did not execute another suite. The failures involved five auth cases and one M2 card-loading case affected by the changed Save label and locale response contract. Updated browser checks must pass before release; this run is not recorded as a complete PASS.
- Edge checks passed 159 tests in each of the two existing execution modes. Fixture regeneration produced no diff.
- Phone renders at 390 × 844 showed captured public stories within the initial screen in both languages. These prove local layout only. They do not prove live translation quality or China relevance for those layout fixtures.

## Monitoring interpretation

The configured monitor examines a 60-minute window. Fewer than five requests is insufficient volume. Above 10% failed deliveries is a failure. A healthy delivery result does not establish useful recommendations. Model/fallback outcomes and latency bands remain visible in the receipt.

The existing seven-day M2 relevance, search, freshness and coverage criteria remain in force. Click capture and successful API responses cannot substitute for judged relevance. Missing translations cannot be used to hide a coverage failure.

The private newsletter lane remains original-only under its existing translation privacy contract. Its native-language projection is preserved. Cross-language newsletter translation is not established by these changes or tests.

## Production gate

Automatic approval review rejected applying the three new Supabase migrations because they change production schema, translation billing functions and access rules. No migration was applied. Explicit approval for those exact changes was requested and remains necessary. The reviewed migration helper pins the project, source commit and all three SQL hashes, applies them atomically, and verifies existing M1 access rules afterward.

After approval, release still requires a bounded real translation run, inspection of both translation directions, the immutable backend deployment, the site build, owner browser/CLI checks and operational receipt readback. The prepared local image context is not a deployment receipt.

An external Claude plan review was separately blocked before transmission. Independent Codex reviews were performed; no Claude PASS is claimed.
