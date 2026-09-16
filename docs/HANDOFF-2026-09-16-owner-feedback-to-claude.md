---
type: note
created: 2026-09-16
modified: 2026-09-16
tags: [news-curator, handoff, m2]
status: handed-to-claude
---

# News Curator M2: Codex to Claude Code

JJ asked Codex to wrap up and let Claude Code do the rest on September 16. Codex execution is stopped. This is a handoff, not approval to deploy or a claim that M2 is accepted.

## Start here

- Private repository: https://github.com/joydai2026-del/news-curator
- Feedback draft PR: https://github.com/joydai2026-del/news-curator/pull/41
- Branch: `codex/m2-owner-feedback`.
- Verified pushed implementation checkpoint: `d8d8e9f7c70731f4b75ccd048692f7128024198d`.
- Local worktree: `/private/tmp/nc-m2-owner-feedback`. It was clean at handoff inspection. Do not use the M1 learning checkout for these edits.
- Read the PR's `docs/evidence/2026-09-15-owner-feedback-verification.md`, `docs/plans/2026-09-15-m2-owner-feedback.md` and `docs/plans/2026-09-15-m2-operational-health.md`.
- Also read the separate Claude audit: `projects/news-curator/docs/evidence/2026-09-15-m2-third-party-verification/2026-09-15-m2-third-party-verification.html` in the vault. Its findings have NOT been reconciled against PR #41 by this wrap-up. Preserve them as open until checked.

## What JJ asked for and what this branch contains

| Feedback | Built locally | Still needs live proof |
|---|---|---|
| Mixed Chinese and English | Persistent English/中文 display choice, cached public-news translations, language-bound ranking and pagination, localized headlines and interface | Translation quality in both directions; initial load, search, Saved and failure paths on production |
| Save is ambiguous | Saving state, Saved confirmation, rollback, Saved tab; fixed a cache bug that made Saved look empty after recommendation fallback | Owner save/reload/remove across real sessions |
| China News | Configured China News category, verified SCMP China RSS plus matching shared feeds | Fresh relevant coverage from multiple source families |
| Load more feels slow and rigid | Immediate busy/disabled feedback, accessible loading state, retained cards, modest glass styling and reduced-motion support | Measured production latency; visual polish is not a speed result |
| Cannot observe recommendation learning | Distinct pending/recorded/used states, session and revision guards, aggregate backend request health | Owner action -> recorded history -> next eligible model request -> receipt readback |

China scope was stated as China-focused reporting from Chinese and international sources, independent of the selected display language. JJ may provide more sources later. Private newsletters retain their existing original-language privacy boundary; cross-language newsletter translation is not established. Do not silently turn missing translations or thin pages into a coverage PASS.

## What passed, and what did not

- All four GitHub CI jobs passed on `d8d8e9f`: Python 3.10, Python 3.12, Edge and discovery database. Run: https://github.com/joydai2026-del/news-curator/actions/runs/35043792356
- 130 local rendering/reader-contract checks passed on the final executable source.
- Browser coverage: 13 auth cases, 16 controller cases, one long captured-story M2 scenario. Integrated run passed 28/30; the two test-fixture failures were corrected and passed focused reruns on the same executable source. Do not describe this as one uninterrupted 30/30 run.
- Local PostgreSQL tests proved locale filtering/search, source-digest invalidation and permissions, plus translation reservation/release/unknown-charge holds/shared limits. This was not a real provider translation run.
- Independent reader review passed through executable source `5c5c1993ce7d2e19f9ff03179586fba966cedc3d`. Later commits contain test/evidence changes only.
- Real defects found and fixed: wrong headline selector, owner controls shown without configuration, pending activity falsely appearing current, stale-session activity callbacks, Saved hydration cache surviving removal of private card state, and empty rather than valid ARIA busy values.
- Phone renders were inspected. Captured protocol fixtures prove layout and dispatch, not semantic translation quality or China coverage.
- PR #41 is DRAFT, unmerged. These feedback changes were NOT deployed. Seven-day recommendation-quality qualification remains incomplete. No Claude PASS for this feedback branch is claimed: the earlier attempted external Claude plan review was blocked before transmission.

## Production gate: do not bypass

Automatic approval review rejected applying three migrations because they mutate production schema, translation billing functions and access rules. No migration was applied. The user was asked to approve those exact effects and informed a defect could temporarily disrupt translation or recommendations. No such approval has arrived in this conversation. The wrap-up/handoff request does not supply it. A different agent or tool is not a workaround.

Pending migrations:

| File | SHA-256 |
|---|---|
| `202609160001_m2_request_health.sql` | `5ff2358b0cbff949d6380b88c54a98b81c12d4019b67d57008b77dd892e4c1e3` |
| `202609160002_m2_translation_money.sql` | `d5df246e2b09bf2d493d81aac1b2f94311fa74de0c3a08db470d0ed779fde66b` |
| `202609160003_m2_localized_reader.sql` | `c45abda1ccafc7cf002e994705f3bf4c49be164ab26c1f6b21f13b142a271c38` |

A reviewed, machine-local migration helper is prepared at `/private/tmp/nc-m2-build/apply_owner_feedback_migrations.py` (SHA-256 `e5b69d07c85ba7d7a6a3116c83c0a27a1be22e558a8c7c2f9ec113ba5783c976`). Frozen migration worktree `/private/tmp/nc-m2-feedback-migration-release`, source `f39d579b4ec5bb14bc57bcea6340b9c24306cc49`. SQL hashes above match the feedback branch. It pins Supabase project `odurwknvigshekaprjvj`, applies the three changes atomically, records migrations and verifies existing M1 access rules. Read-only preflight passed previously; refresh it after any intervening changes. Review helper and `owner-feedback-migration-helper.md` before use. Do not blindly retarget it to a new commit.

Prior permissions include normal M2 development/deployment, existing OpenAI key and Modal use, and owner testing with `joyd.ai.2026@gmail.com`. They did not satisfy this specific production auto-review gate. Credential values never belong in this handoff, logs or git.

## Other open M2 findings from Claude's audit

The September 15 audit reports five acceptance blockers: a paid rank discarded on a late 409; browser tests missing from CI; no test exercising the real prompt builder; empty owner allowlist failing open; and pagination after interaction causing a new paid ranking. It also reports duplicate query stories, uncategorized All-feed cards and consent changes invalidating other surfaces' frozen rankings. These are audit findings to reconcile against actual current main and PR #41, not newly reproduced facts from this wrap-up. The audit includes owner-account test side effects; do not reset them blindly. Avoid overwriting the audit's existing next-session instructions.

## Continuation order and parallel work

1. Verify current main, PR head, ownership, account state and deployment before changing anything. Main and production may have advanced since the receipts here.
2. In parallel: reconcile Claude's audit against the feedback branch; inspect source/translation capacity and deployment preflight. No production mutation without the missing explicit approval. Work on fixes required by the audit without treating the narrower PR review as a whole-product PASS.
3. After review and exact database approval: apply the pinned migrations with ledger and ACL readback. This is a dependency for the new locale export and telemetry release; do not merge an exporter that calls absent RPCs.
4. After migrations: bounded real translation backfill, both-direction quality checks, current immutable backend image build/deploy, then site publication. The prepared image context is stale preparation evidence, not the final release artifact.
5. After deployment: real owner browser and CLI parity, Save/reload, language switch, China relevance, search, pagination latency, consent/history flow, cost ledger and monitoring readback. Rehearse rollback and keep seven-day relevance/freshness/coverage qualification separate from successful requests.

Operational counters use five-minute anonymous aggregates, a 60-minute alert window, minimum five requests, failure threshold above 10%, and 14-day retention. They diagnose delivery; they do not prove good recommendations. Translation work is bounded and outside interactive pagination. Existing RankLLM/Supabase/Modal and translation interfaces are reused; no new service or subscription is needed for this feedback patch.

## Local access and evidence pointers

- Production site: https://news.joydong.org/
- Last recorded backend before this handoff: `99e9661c363fa115f5bb8257fe96b014092d8011`, Modal app `news-curator-m2-ranker`, workspace `joydai2026-del/main`. Recheck before relying on it.
- Python environment: `/private/tmp/nc-m2-feedback-test-env/bin/python`.
- Local evidence: `/private/tmp/nc-m2-build/`, including review reports, migration preflight and language UI coverage receipt. These local temporary artifacts may not survive machine cleanup; committed evidence and GitHub are durable.
- Owner authentication: existing normal Google browser flow or existing Keychain-backed CLI. Do not mint owner JWTs or extract browser secrets.
- OpenAI key was already staged to the protected GitHub `personalization` environment. Read presence only, never values.
- Local private binding and runtime-secret artifacts exist under the evidence directory. They are not committed; inspect only required field presence and never publish their contents.

## Wrap-up accounting

No build, provider call, production migration, merge or deployment was performed by this wrap-up. M1 remains separate. No milestone plan was archived because M2 release/acceptance is still open. Existing skills and unrelated staged vault work are preserved, not swept into this handoff. No new cross-project pattern is asserted from a single run. Duration across the continued/compacted task is not reliably recoverable, so no duration is invented.

### Paste into Claude Code

Continue News Curator M2 from `/Users/joyd/Documents/jj-knowledge-vault/projects/news-curator/docs/HANDOFF-2026-09-16-owner-feedback-to-claude.md`. Reconcile draft PR #41 (`codex/m2-owner-feedback`, checkpoint `d8d8e9f`) with the separate September 15 third-party audit. The feedback implementation is locally reviewed/tested but not deployed; specific approval for three production database migrations remains missing. Verify current state, preserve M1, and carry forward both the user feedback and unresolved audit findings.
