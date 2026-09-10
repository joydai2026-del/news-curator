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

PR [22](https://github.com/joydai2026-del/news-curator/pull/22) merged these corrections as `3c86e4be1dee46fa0da801342c8cfa226b3a3cd4`. Its exact-head CI run [34471062086](https://github.com/joydai2026-del/news-curator/actions/runs/34471062086) tested `34fd962bf5472615f2876b452df10275a3941cda`; all four jobs passed, including PostgreSQL 17.11. Post-merge CI [34471193286](https://github.com/joydai2026-del/news-curator/actions/runs/34471193286) also passed at `3c86e4be1dee46fa0da801342c8cfa226b3a3cd4`.

Migration `202609100001_discovery_retry_identity.sql` was then applied to production with SHA-256 `0347e5d23db3a2445f2d95c40da1bab6266215722d25851ba6c53e9dbe3ea6da`. Production readback confirmed the migration, one functional lookup index, service-role-only execution, rejected null input, and zero private editions.

## Second activation and recovery

Activation [34471393133](https://github.com/joydai2026-del/news-curator/actions/runs/34471393133) ran at `3c86e4be1dee46fa0da801342c8cfa226b3a3cd4`. The repaired public baseline helper passed against the actual GitHub API and selected the oldest eligible validated capture. The owner-bound candidate selected 21 stories with shortfalls Updates 0, Hot 1, Interested 0, Surprise 2. Relevance and Deliberate Surprise returned FAIL, and the materializer exited 3 without storing an edition. The diagnostics did not expose achieved values or direction, so the shortfalls do not prove how many stories qualified for either failed band.

Discovery was restored to false. The run ended terminal-cancelled before deploy, persistence, or archive finalization. Production readback confirmed zero private editions. The latest public revision is `3c86e4be1dee46fa0da801342c8cfa226b3a3cd4`, serving the M1 behavior from the earlier flag-off release.

Independent selection analysis has not proven a safe fix or a viable passing owner edition. The unresolved next investigation is to distinguish failure direction with safe aggregate achieved and eligible-count evidence from the protected candidate pool, then assess whether eligible same-primary-lane replacements exist. Quality thresholds remain unchanged.

## Verification boundary

The local full suite completed with 2,279 passed, 29 optional-runtime skips, and zero failures or errors. The final focused suite completed with 78 passed. CI supplied the PostgreSQL runtime proof for the retry migration. Independent security and correctness reviews passed before merge and production application.

Production completion still requires a genuine configured-owner PASS settlement, activated-page verification, and authenticated owner read/save testing. Account-specific authorization and normal browser sign-in subsequently passed, as recorded below. No test reading state was changed. M2 is not done, and M1 learning completion is not inferred from source tests or public-page checks.

## Authorized account follow-up

JJ authorized a specific Google account for M2 testing. Normal Google account selection succeeded in a background browser, and the production signed-in dashboard loaded. No password, verification code, additional consent, token extraction, or native-login workaround was required. No reading, saved, or interest state was changed. Discovery remained disabled, so this proves login and dashboard access, not M2 lane behavior.

A read-only diagnostic used that account's actual saved preferences and signals, the failed activation's original current and prior source captures, and a fixed diagnostic evaluation clock. This is not a stored production receipt or proof of the exact internal production evaluation clock. The existing engine selected 14 of 24 candidates. Relevance was 0.000, and Surprise was 0.429 against its 0.250 cap; the other five bands passed. None of the 24 candidates met the relevance threshold, so no unchanged-policy subset could pass relevance. All 73 possible same-primary single replacements were also checked without finding a PASS.

The automated production run's 21 selected stories and six primary Interested entries differ from this account's diagnostic. The hidden automated-owner binding remains unverified; the difference does not identify another account. No production owner setting was replaced.

The pending user choices are which real interests to save for the test and whether the authorized account should receive ongoing M2 editions or be used only temporarily. Do not invent interests, lower quality thresholds, infer another account's identity, or claim M2 end-to-end completion from the successful login. Once those choices are supplied, verify the protected account routing, build a genuine PASS edition, and complete the private lane/read/save/reload/logout tests while restoring temporary reading state. M1 learning remains separate.
