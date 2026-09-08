# Google sign-in and reading feedback correction

Date: September 8, 2026. This corrects the earlier M1 test-ready receipt. M1 acceptance is reopened and remains incomplete because real Google sign-in is blocked. Notion remains read-only planning authority.

Evidence grades: A means production proof, B means source or local executable proof, C means not verified.

## Current acceptance

Google sign-in is not ready in production. The user-reported failure was reproduced by clicking the deployed Google button: the request ended at Supabase with HTTP 400, `validation_failed`, and `Unsupported provider: provider is not enabled`. No Google account page was reached. A first harness used a mismatched button label; that assertion is not counted as reproduction evidence.

| Gate | State and evidence |
| --- | --- |
| Google-only entry | A: deployed at the real callback; email/code controls and client endpoints removed; Google PKCE remains the only browser sign-in path |
| New and returning users | B: local browser tests start at the actual Google button, bind the generated challenge to the exchanged verifier, and preserve the resulting session and preferences on reload |
| Callback errors | B: query and fragment errors are scrubbed; implicit token fragments and duplicate code/state parameters are rejected; the retry button remains usable |
| Real provider availability | A, failing: live `/auth/v1/settings` reports Google disabled, email enabled, and signup disabled |
| Dedicated Google web client | C: provider activation is blocked pending dedicated web credentials and public-audience verification; newsletter-ingestion credentials must not be reused |
| Actual Google consent and callback | C: not exercised. Routed test responses and admin-created sessions do not prove this gate |
| Reader gray/read feedback | A: desktop, phone, and delayed reader-script startup pass on the deployed build. Signed hydration, conflict rollback, stale revision rejection, and cross-context persistence remain B, local contract proof |
| Deployment of this correction | A: merged and deployed by successful Curate run 34253278365, including archive finalization |
| Product-owner acceptance | OPEN. Google configuration and real consent/callback proof are still required |

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

The deployed [callback](https://news.joydong.org/auth/callback/) was independently rendered and visually inspected at widths 1440 and 390. It contains one Google sign-in action, no email field, and no horizontal overflow. Its full configured HTML and static assets match the release source. Clicking that real button still ends at the same provider-disabled HTTP 400 rather than Google. The production readback remains `google=false`, `email=true`, and `disable_signup=true`. No production auth configuration was changed in this correction.

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

## Durable Google setup handoff

The remaining external blocker is approval to create the dedicated Google Web client. Do not reuse a desktop client, newsletter OAuth credentials, or an unrelated project client. No credential values belong in this repository or receipt.

| Next action | Required proof |
| --- | --- |
| Create the dedicated Google Web application client after approval | Basic sign-in identity scopes only; exact authorized Supabase redirect `https://odurwknvigshekaprjvj.supabase.co/auth/v1/callback`; intended public External audience in production, not a test-user-only allowlist |
| Apply scoped Supabase provider configuration through a secure channel | Google enabled with the dedicated client, `disable_signup=false`, and `external_email_enabled=false`. Preserve other provider settings and the current callback allowlist |
| Read back the public auth settings and click the real deployed Google button | Google enabled, email disabled, signup open, and actual navigation to `accounts.google.com`. This is provider availability, not completed sign-in |
| Complete actual new and returning Google user flows | Consent returns through the deployed callback, PKCE code exchange succeeds, and preference/reading state persists. Test cancellation and retry. Request interactive user login if no safely authorized real login is available |
| Close acceptance only after that live proof | Keep M1 and product-owner acceptance open until the real Google flow is verified; routed responses or admin-created sessions never substitute |
