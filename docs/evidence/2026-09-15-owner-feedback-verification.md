# Reader feedback verification

Status: local implementation reviewed and regression checks passed; production verification pending. Preserved in draft PR #41 (https://github.com/joydai2026-del/news-curator/pull/41). Production still serves the previous release. This record does not mark M2 relevance or coverage qualification complete.

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

- Independent reader review passed through `5c5c199`, including pending activity, empty history, failed writes, model revision checks, sign-out/session fencing, headline translation, unconfigured controls and restoring Saved after a recommendation fallback and the accessible loading indicator.
- Python compilation, offline shell rendering and 130 reader/render contract checks passed. Offline rendering is structural evidence only, with no coverage or translation-quality claim.
- Independent backend review passed at `e94e195`. It covered translation dispatch bounds, shared spending limits, category scheduling and preservation of native newsletter projections.
- A disposable local PostgreSQL test at `4b3e37c` proved pre-send release, retained unknown-charge holds, exact usage settlement, idempotent replay, and rejection of a third call that would exceed the shared cap. This is a local protocol test, not a real provider call.
- The localized SQL verification passed nine local checks, including original/display search, missing translations, stale content digests, quarantine and access rules.
- The initial broad local regression had six browser failures and was not a complete PASS. Subsequent checks found and repaired both outdated fixtures and real defects: headline translations targeted a nonexistent element, unconfigured pages exposed owner controls, and returning from M2 could leave Saved falsely marked as already loaded. The integrated 30-case run passed 28 cases. Its two remaining fixture failures were repaired and each passed a focused rerun against the final production source: duplicate projection-directory setup (3.96 seconds) and accelerated timing leaking into a normal Saved-to-All transition (8.53 seconds). The checks cover 13 auth cases, 16 reader-controller cases and the full M2 browser scenario. This is local protocol evidence, not a production E2E pass.
- Draft PR CI passed all four jobs at the final executable source `5c5c199`: Python 3.10, Python 3.12, both Edge execution modes and the discovery database contract. Later commits repair browser fixtures and record evidence only.
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
