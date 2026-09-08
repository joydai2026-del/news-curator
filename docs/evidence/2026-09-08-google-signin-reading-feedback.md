# Google sign-in and reading feedback correction

Date: September 8, 2026. The Google sign-in blocker is resolved by production configuration and real-user verification. The corrected Google-only and reading flows are ready for JJ to test. Product-owner M1 acceptance remains open. Notion remains read-only planning authority.

Evidence grades: A means production proof, B means source or local executable proof, C means not verified.

## Current acceptance

Google sign-in now works in production. The original failure was reproduced at the real button as a provider-disabled HTTP 400. After the approved dedicated Web client and scoped Supabase activation, the real button reaches Google, actual consent completes, and new/returning sessions preserve preferences and reading state. No routed response or admin-created session substitutes for this proof.

| Gate | State and evidence |
| --- | --- |
| Google-only entry | A: deployed at the real callback; email/code controls and client endpoints removed; Google PKCE remains the only browser sign-in path |
| New and returning users | A: first real Google consent plus two returning Google sign-ins completed through the deployed callback; preferences and reading state survived reload and reauthentication |
| Callback errors | B: query and fragment errors are scrubbed; implicit token fragments and duplicate code/state parameters are rejected; the retry button remains usable |
| Real provider availability | A: live public settings report Google enabled, signup open, email disabled; the actual button returns HTTP 302 to Google with the exact Supabase callback |
| Dedicated Google web client | A: dedicated Web client, exact origin/redirect, External/In production audience, and only OpenID/email/profile scopes; no newsletter or unrelated client reused |
| Actual Google consent and callback | A: Google account choice and consent returned to the query-scrubbed callback and signed-in interests panel |
| Reader gray/read feedback | A: desktop, phone, delayed startup, and real signed-in read/save/unread persistence pass. Conflict rollback and stale revision rejection retain separate B local-contract evidence |
| Deployment of this correction | A: merged and deployed by successful Curate run 34253278365, including archive finalization |
| Product-owner acceptance | OPEN. Functional Google and reading gates pass; this is not a claim that JJ has accepted all M1 behavior |

## Google activation and current live build

| Item | Production evidence |
| --- | --- |
| Current served source | `7b840ef346f8429d6abe3c55debd44b99c34e10f`, from successful unattended [scheduled run 34257040399](https://github.com/joydai2026-del/news-curator/actions/runs/34257040399) |
| Current chain | Source snapshot, newsletter, build, state persistence, deploy, and archive finalization succeeded. The Google configuration change required no new publication |
| Exact served bytes | Live HTML matches that run's archive candidate; reader/auth scripts, privacy page, configured callback, inline reader and CSS match reviewed product source |
| Cache preservation | Docs branch starts after cache-only commit `5377fdb`; its differences from the served source are confined to the three existing cache/cursor files |
| Completed test-only follow-up | [PR 14](https://github.com/joydai2026-del/news-curator/pull/14) merged as the current served source. [PR CI 34254650751](https://github.com/joydai2026-del/news-curator/actions/runs/34254650751) and [main CI 34254863210](https://github.com/joydai2026-del/news-curator/actions/runs/34254863210) passed all three checks |
| Final deterministic floor | 2,128 passed, seven optional-environment skips, eleven socket-marked cases deselected; targeted auth 56 passed. The unchanged reader browser module remains 13 passed |
| Dedicated identity setup | Basic identity scopes only; External audience in production, not a test-user allowlist. Homepage/privacy/domain branding saved. Billing independently verified disabled; no paid workload created |
| Scoped auth activation | One PATCH contained only Google enable/client/secret, signup opening, and email disabling. Site URL and redirect allowlist still match exactly. No unrelated credential reused |
| Readback limitation | The management API returns a stable opaque secret representation, not the submitted plaintext. Strict helper comparison failed and is not counted as a full verifier pass. No retry or automatic rollback occurred. Actual Google code exchange proves the credential works |
| Rollback preparation | After independent review, a GET-only operation securely bound the observed five-field state while preserving the original previous and intended records. Later drift must refuse rollback. Seventeen optimized-Python boundary cases passed; no production rollback rehearsal occurred |

The real-user browser run exercised first consent and two returning Google sign-ins. A saved preference survived reload. Opening a real story immediately grayed it; Save persisted after feed reload and appeared in Saved. Mark unread kept the story open and persisted as unread while saved. Signing out in one tab changed the other tab to signed-out public mode. The temporary preference and story flags were restored to their initial empty/unread/unsaved state, then a final real Google sign-in and reload verified that restoration. The real user account was retained. Private account, preference, story, token, and credential values are omitted.

Google consent can still show the Supabase authentication host until optional Google brand verification. This did not block sign-in. Real Google cancellation was not separately exercised; scrubbing, rejection, and retry behavior retain their B local browser proof. Product-owner acceptance and elapsed five-/thirty-day history remain open as described in the M1 receipt.

## Exact release and live proof

| Item | Evidence |
| --- | --- |
| Reviewed implementation | `71718db5301e344cd75c0701aad6f39989110a6e`; independent auth/security and final reader source reviews passed |
| Product merge and served source | `4258aeee715e6ce33517a865e8101a1f72b22dd8`, [PR 13](https://github.com/joydai2026-del/news-curator/pull/13) |
| PR CI | All three checks passed in [run 34253051862](https://github.com/joydai2026-del/news-curator/actions/runs/34253051862) |
| Production chain | [Run 34253278365](https://github.com/joydai2026-del/news-curator/actions/runs/34253278365), event `push`, passed source snapshot, newsletter, build, cache persistence, deployment, and archive finalization |
| Exact served bytes | The live page matches the deployed archive candidate. Live reader/auth scripts, privacy page, rendered callback, inline reader script, and styles match the reviewed source |
| Cache preservation | Three cache-only differences between reviewed source and merged main were preserved. All other files were byte-equivalent. Successful cache persistence produced `a98649e` |
| Duplicate prevention | Manual run 34253332513 was cancelled while queued after the automatic push run was discovered. Its completed readback has no jobs, so it never built, deployed, or finalized a publication |
| Final-path ranking/newsletter | Protected saved-interest ranking reported that published order changed. The newsletter profile/artifact guard passed; this run had zero newly collected newsletter items. The archive contains 162 stories and 190 named coverage mentions. Ten source-freshness warnings were visible |

The coordinating reviewer exercised the real [live feed](https://news.joydong.org/) at desktop width 1440 and phone width 390, plus desktop with the actual deferred reader script held until after the first accordion click. All three passed: opening immediately turns the headline gray, Mark unread restores the unread color while leaving the story open, focus returns to the accordion, reopening marks read again, and collapse retains read state. The read color was `rgb(120, 129, 124)` versus black for unread. Closed card height did not shift. All three screenshots were visually inspected. These anonymous checks created no account and wrote no backend state.

The deployed [callback](https://news.joydong.org/auth/callback/) was independently rendered and visually inspected at widths 1440 and 390. It contains one Google sign-in action, no email field, and no horizontal overflow. Its full configured HTML and static assets match the release source. The initial correction deployment still returned provider-disabled HTTP 400; that historical blocker was subsequently resolved by the separately approved activation above.

## Local auth results

The auth/unit and executable JavaScript contract group passed 55 tests. Headless Chromium passed 11 auth cases, covering Google-button startup, new/returning preference state, reload continuity, same-tab return without cross-tab messaging, four logout outcomes, and three provider-error cases. These are local contracts, not real Google identity-provider proof.

The broader deterministic pass completed with 2,127 passed, seven optional-environment skips, and eleven socket-marked cases deselected. Both unchanged PostgreSQL runtime files and both Playwright modules were explicitly excluded from that command; their evidence is separate. Edge tests passed 159 cases in each of the two CI Node modes. Ruff and JavaScript syntax checks passed. The Google-only auth page and corrected privacy page were rendered and visually inspected at desktop and phone sizes without horizontal overflow.

The deterministic command was repeated on exact implementation `71718db` with the same 2,127 passed, seven skipped, and eleven deselected result. The full reader browser module passed all 13 cases on that frozen commit in 68.18 seconds. No unchanged database migration or runtime was rerun for this frontend-only correction.

Independent auth/security review passed the exact auth commit `fe371afecfa5cd20b954947d9b8854609b504b4d` and privacy/contract commit `60c3d92da098838e8dcc6056905ac0fb81a19edf`. That reviewer independently passed eight focused installed-Chrome cases and the sanitized JavaScript contract. A separate raw Codex attempt timed out; its exact-commit retry failed when the host ran out of disk space. Neither incomplete raw review is counted as a pass.

The current privacy page now distinguishes anonymous browsing from optional signed-in account, interest, and reading-state storage. The browser auth source stores only its minimal Supabase session in session storage, not Google provider tokens or passwords. No unrelated credentials were passed to the independent review child process.

An independent installed-Chrome pass of the four newly added reader cases passed on the owner's current source. One active-mutation case passed in 8.18 seconds; the anonymous and signed early-open cases plus refresh/cross-context persistence passed in 40.76 seconds. This check made no source edits and did not reproduce the earlier readiness timeout. Its signed cases use a local auth contract, not real Google sign-in.

## Post-merge test correction

The product merge's later [main CI run 34253278488](https://github.com/joydai2026-del/news-curator/actions/runs/34253278488) exposed a Python 3.10 job failure in the executable JavaScript auth contract. The Python 3.12 and Edge jobs passed. The failing assertion expected the callback session expiry to equal a timestamp captured before asynchronous PKCE work. Crossing a second boundary made that expected value one second stale. Advancing the test clock by one second reproduced the failure without changing product code.

Test-only commit `0bb412e` keeps every non-time session field and the broadcast shape exact, and bounds the integer expiry by callback start/end plus the response's one-hour lifetime. A second executable contract case forces the clock boundary without sleeping. The targeted auth group passed 56 tests. This correction changes no production assets and requires no new publication; its independent review and CI are follow-up merge gates, not retroactive claims about the failed run.

## Required production readback

Before calling Google sign-in ready, record all of the following on the exact served build:

- Public settings report Google enabled, new-user signup open, and email sign-in disabled.
- The real Google button reaches `accounts.google.com`, not a Supabase provider error.
- A dedicated Web application client uses the exact Supabase callback and permits the intended public audience, not only a test-user allowlist.
- Real Google consent returns through the deployed callback, exchanges the code, and preserves reading and preference state for new and returning users.
- Cancellation and failed callbacks remain scrubbed and retryable.

The implementation follows the [Supabase Google sign-in guide](https://supabase.com/docs/guides/auth/social-login/auth-google) and its [PKCE session flow](https://supabase.com/docs/guides/auth/sessions/pkce-flow). Existing deployment, database, and previous isolated-account cleanup proof remains in the earlier M1 receipt; it is not relabeled as Google sign-in proof.

## Operational handoff

The dedicated client, public audience, scoped activation, and real first/returning sign-in gates are complete. Credential and rollback records remain outside the repository in owner-only storage. Their locations and independent helper reviews are held in the private activation receipt. No secret values belong in this public document, commits, or workflow logs. Do not reuse this basic-identity client for newsletter ingestion or unrelated APIs.

The original previous/intended record and separately observed five-field record must remain together. A rollback is a separately authorized production change and must first match every current scoped field to the observed record. The management secret representation is intentionally treated as opaque. Do not bypass drift checks or restore the entire auth configuration.
