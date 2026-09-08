# Google sign-in and reading feedback correction

Date: September 8, 2026. This corrects the earlier M1 test-ready receipt; it does not declare product-owner acceptance. Notion remains read-only planning authority.

Evidence grades: A means production proof, B means source or local executable proof, C means not verified.

## Current acceptance

Google sign-in is not ready in production. The user-reported failure was reproduced by clicking the deployed Google button: the request ended at Supabase with HTTP 400, `validation_failed`, and `Unsupported provider: provider is not enabled`. No Google account page was reached. A first harness used a mismatched button label; that assertion is not counted as reproduction evidence.

| Gate | State and evidence |
| --- | --- |
| Google-only entry | B: email/code controls and client endpoints removed; Google PKCE remains the only browser sign-in path |
| New and returning users | B: local browser tests start at the actual Google button, bind the generated challenge to the exchanged verifier, and preserve the resulting session and preferences on reload |
| Callback errors | B: query and fragment errors are scrubbed; implicit token fragments and duplicate code/state parameters are rejected; the retry button remains usable |
| Real provider availability | A, failing: live `/auth/v1/settings` reports Google disabled, email enabled, and signup disabled |
| Dedicated Google web client | C: provider activation is blocked pending dedicated web credentials and public-audience verification; newsletter-ingestion credentials must not be reused |
| Actual Google consent and callback | C: not exercised. Routed test responses and admin-created sessions do not prove this gate |
| Reader gray/read feedback | Verification pending the parallel reader correction |
| Deployment of this correction | Not deployed yet |

## Local auth results

The auth/unit and executable JavaScript contract group passed 55 tests. Headless Chromium passed 11 auth cases, covering Google-button startup, new/returning preference state, reload continuity, same-tab return without cross-tab messaging, four logout outcomes, and three provider-error cases. These are local contracts, not real Google identity-provider proof.

The current privacy page now distinguishes anonymous browsing from optional signed-in account, interest, and reading-state storage. The browser auth source stores only its minimal Supabase session in session storage, not Google provider tokens or passwords. No unrelated credentials were passed to the independent review child process.

## Required production readback

Before calling Google sign-in ready, record all of the following on the exact served build:

- Public settings report Google enabled, new-user signup open, and email sign-in disabled.
- The real Google button reaches `accounts.google.com`, not a Supabase provider error.
- A dedicated Web application client uses the exact Supabase callback and permits the intended public audience, not only a test-user allowlist.
- Real Google consent returns through the deployed callback, exchanges the code, and preserves reading and preference state for new and returning users.
- Cancellation and failed callbacks remain scrubbed and retryable.

The implementation follows the [Supabase Google sign-in guide](https://supabase.com/docs/guides/auth/social-login/auth-google) and its [PKCE session flow](https://supabase.com/docs/guides/auth/sessions/pkce-flow). Existing deployment, database, and previous isolated-account cleanup proof remains in the earlier M1 receipt; it is not relabeled as Google sign-in proof.
