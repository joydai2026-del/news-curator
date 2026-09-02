# News Curator v3 translation repair receipt

Date: 2026-08-29

Evidence grade: B, proven in the reviewed local source tree and deterministic tests. No cloud, provider, workflow dispatch, browser, or production claim is made.

## Implemented

- Direct workflow script entry points import the repository package under the exact `python scripts/...` invocation shape.
- Provider response language, id, model, cardinality, and configurable output bounds are validated before settlement.
- Provider, acquire, store, and lifecycle failures emit fixed bounded reason counters. A failed attempt to persist an ambiguous paid state exits the translation job nonzero.
- Stale never-sent leases release counters. Stale sent work becomes charge-unknown and remains blocked from automatic paid retry.
- SQL reconciliation locks the reservation before rechecking prior reconciliation, making identical concurrent calls idempotent.
- Default budgets are 2,000 characters/run, 15,000/day, and 450,000/month.
- Native target-language content wins while retaining translation provider/model/source-language provenance.
- `TranslationRecord` is documented as a settled-only static projection. Mutable lifecycle and accounting data remain private and authoritative in durable cache/reservation rows.

## Local proof

- Translation-focused suite: 84 passed, 2 skipped. The two skips are the local PostgreSQL harness because local Supabase PostgreSQL was unavailable on `127.0.0.1:54322`.
- Full deterministic no-socket suite: 949 passed, 2 skipped, 3 deselected.
- Static compile: passed using a temporary bytecode cache.
- Both direct script `--help` entry points: passed from the repository root.
- `git diff --check`: passed.
- Design guard for `docs/design/` and `curator/render.py`: passed.

## Explicit blockers

- Local SQL behavior is not executed until `supabase start` and `supabase db reset` are available. The harness exists at `tests/test_translation_store_local_db.py` and refuses non-loopback databases.
- Protected workflow execution, single-snapshot orchestration, callback materialization, workflow expression quoting, dependency split, and canonical workflow artifact validation are owned by the workflow lane and remain unverified here.
- Google translation, Supabase cloud state, credentials, billing, browser behavior, workflow dispatch, deployment, and live production are not exercised or activated.
