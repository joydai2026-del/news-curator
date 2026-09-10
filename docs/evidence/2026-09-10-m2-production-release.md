# M2 production release evidence

Status: release verification in progress. M1 learning remains separate and unchanged.

## Deployed foundation

PR [21](https://github.com/joydai2026-del/news-curator/pull/21) merged as `ecbabda899f898fa047ec1f5259bf6e3b612b93c`. All four post-merge CI jobs passed in [34466299837](https://github.com/joydai2026-del/news-curator/actions/runs/34466299837), including PostgreSQL 17.11. Curate [34466299921](https://github.com/joydai2026-del/news-curator/actions/runs/34466299921) deployed this code with discovery disabled and finalized its public archive.

Migration `202609090001_discovery_lanes.sql` was applied to production. Readback confirmed all three M2 tables, forced row-level security, service-only settlement, and authenticated owner-derived reads. Anonymous callers cannot execute discovery reads or settlement. The migration SHA-256 is `96dfe95e0dea9b60ac7306c6161d8336af4ccc79bc5af9eaf9386195a1d9c2b9`.

Anonymous desktop and mobile checks passed on the deployed public release: reader, topic filtering, pagination, expanded headlines, and signed-out dashboard. The 390-pixel mobile viewport had no horizontal overflow. No browser console errors or private edition content were observed. These checks do not establish authenticated owner behavior.

## First activation and recovery

Activation [34467168523](https://github.com/joydai2026-del/news-curator/actions/runs/34467168523) ran on `c8410d4ca5bd6201741ba4a959759615d2a9094a`, a cache-only successor of the merged code. Its materializer exited 3 with `edition_bands_failed`: nine selected stories, with shortfalls Updates 8, Hot 2, Interested 2, Surprise 3. Production readback confirmed zero stored private editions. The workflow's continue-on-error step displayed a successful conclusion, so the badge was insufficient evidence of settlement.

Discovery was restored to false and the activation was canceled before a Pages deployment. The verified public release remained live. No private data or schema was deleted.

## Cause and correction

The source baseline picker stopped at the nearest compatible capture. During a deployment burst this compared captures only minutes apart, overlooking genuine publisher text changes elsewhere in the configured Updates window. A real captured-source comparison at the same evaluation clock found zero eligible Updates with the nearest capture, nine with a capture about 1.15 hours earlier, and fourteen with one about 2.06 hours earlier. The latter two source-only, first-local editions passed the unchanged bands. They are not owner-bound settlement evidence. Full replay of that large comparison was interrupted and is not claimed as passing.

The correction selects chronologically, never according to whether a candidate edition passes. The first edition uses the oldest validated capture from the bounded, complete eligible run window. Later editions use the newest validated capture at or before the latest stored edition's generation time. The configured policy and evaluation clock are shared by selection and scoring. Out-of-window prior observations cannot establish a recent publisher change.

An additive service-only retry lookup recognizes the existing immutable input tuple before looking for baseline artifacts. The stored edition identity still binds its actual prior capture. This prevents immediate and older retries, including retries after artifact expiry, from creating another edition. Ambiguous matches fail closed. No authenticated or public RPC is widened.

Failure diagnostics expose only failed band names and verdicts. The workflow explicitly reports that no new edition was stored and existing state was preserved. All seven editorial bands and repetition guards remain unchanged.

GitHub's documented [workflow run filters](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow) provide the bounded creation-time query. Actual source capture clocks and configuration digests remain the final evidence checks.

## Verification boundary

The local full suite completed with 2,279 passed, 29 optional-runtime skips, and zero failures or errors. Focused checks cover the later query and clock corrections. PostgreSQL execution for the new retry migration must pass in CI before application; local Docker was unavailable. Independent security and correctness review findings are being resolved before the next release.

Production completion still requires the retry migration, a genuine configured-owner PASS settlement, activated-page verification, and authenticated owner read/save testing. The browser approval gate requires account-specific authorization before selecting a Google account. No account was selected and no test reading state was changed. M2 acceptance and M1 learning completion are not inferred from source tests or public-page checks.
