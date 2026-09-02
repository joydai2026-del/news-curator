# News Curator v3 personalization QA receipt

Date: 2026-08-30  
Evidence grade: B for source and deterministic local execution. No database or cloud proof is claimed.

## Implemented matrix

The loopback-only Supabase API harness now covers anonymous and genuinely expired signed JWT denial; saved-search unknown fields, duplicate IDs, per-field size limits, and total serialized size; owner insert, read, and delete; cross-user read, write, and delete isolation; altered-ID and server-owned-column bypasses; direct update denial; private validation helpers; and authenticated-only compare-and-swap access. The concurrency test also rechecks the stale-revision conflict after simultaneous writers.

## Fresh checks

- Personalization focus: `51 passed, 6 skipped`. All six API cases skipped because the required local Supabase URL and keys are absent.
- Repository deterministic gate: `995 passed, 8 skipped, 6 deselected`. The eight skips are the local translation PostgreSQL matrix. The six deselected cases are the loopback-only personalization API matrix.
- Python compile, checklist YAML parse with 19 criteria, owned-file whitespace check, and no-design diff guard: PASS.
- Global `git diff --check`: PASS.

## Remaining proof

NCV3-07 and NCV3-08 remain PARTIAL. Run `supabase db reset`, export the four loopback harness variables from `supabase status -o env`, and execute the named local API and compare-and-swap suites. The current environment has none of those four variables. Callback workflow materialization and single-snapshot workflow wiring are absent in source, so NCV3-15 also remains PARTIAL rather than being described as merely unproven.
