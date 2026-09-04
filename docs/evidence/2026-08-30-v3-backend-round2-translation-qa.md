# News Curator v3 Round 2 translation and database QA receipt

Date: 2026-08-30

Evidence grade: B. This proves the current local source tree and deterministic tests only. It does not prove cloud, provider, browser, workflow dispatch, database-engine, or production behavior.

## Evidence progression

- Pre-Round-2 deterministic baseline: 949 passed, 2 skipped, 3 deselected.
- Post-fix deterministic suite: 979 passed, 8 skipped, 3 deselected.
- Translation-focused suite: 83 passed, 8 skipped.
- Final merged deterministic suite after snapshot-freshness fixes: 1097 passed, 8 skipped, 6 deselected.
- Post-correlation translation matrix: 117 passed. Focused store and job correlation suite: 55 passed.
- Static compile with a temporary bytecode cache: passed.
- QA checklist YAML parse, `git diff --check`, and the no-design diff against `3adefcae37794e40a4acc053d1e424791711678a`: passed.

## Round 2 fixes proven locally

- Google translateText now receives a validated full model resource. That exact requested string is also the provider result, cache identity, and localized provenance.
- Translation consumes the same checksummed source snapshot as publication when `--source-snapshot` is supplied. Explicit consumers reject stale, future, tampered, or config-mismatched snapshots without calling `collect()` again. Ranking and age filtering use the current build clock, not the snapshot timestamp.
- Originals remain unchanged under timeout, partial response, missing credential, budget exhaustion, database outage, malformed artifact, missing artifact, and disabled translation.
- The local PostgreSQL harness now covers competing acquire, run/day/month counters and UTC keys, duplicate idempotency, deadlock pressure, settle retry, quarantine, pre-send and post-send failures, stale recovery, concurrent reconciliation, and Python Supabase RPC parity.
- Supabase cache and reservation replies are now bound to the exact originating request before output, lifecycle state, or provider action. Malicious fake replies cover alternate stories, input digests, locales, provider and model identity, policy versions, idempotency, run, and budget fields.
- A valid blocker may come from an older run, but only when its complete cache identity matches and its state is one that must prevent automatic paid retry.

## Explicit blockers

- All eight local database cases skipped because local Supabase PostgreSQL is unavailable on `127.0.0.1:54322`. Python RPC parity additionally needs a local-only service-role test identity. No SQL-engine claim is made.
- Dependency hash locking, expression-safe environment quoting, callback materialization, single-snapshot workflow wiring, and one canonical workflow artifact validator remain outside this protected workflow edit boundary and are not claimed here.
- Google Translation, Supabase cloud state, credentials, billing, browser behavior, protected workflow execution, live sources, deployment, and production remain unverified or blocked.
