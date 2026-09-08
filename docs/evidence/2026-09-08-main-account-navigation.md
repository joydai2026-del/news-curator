# Main-page account and navigation correction

Date: September 8, 2026. Acceptance reopened by user feedback. Notion remains read-only planning authority. Earlier release receipts remain historical evidence, not proof of this main-page flow.

A means production proof. B means source or local executable proof. C means not verified.

## Reproduced problem

The actual main-page sign-in click stopped at a separate callback page instead of going directly to Google. After actual Google sign-in and returning to the digest, the header still showed the unsigned entry with no account identity. These are A defects on the previous build. Actual Save persisted across a later real Google session and reload, so a backend Save persistence failure was not established. The production test story's original flags were restored through the UI and existing user interests were untouched.

## Scoped correction

The main link starts Google in the same tab. A same-origin early-click fallback starts Google automatically and consumes its flag before navigation. After valid PKCE code exchange, the callback returns to the main page automatically. PKCE is the existing proof-key protection binding the callback to the browser that started sign-in.

The header shows account text only after the current session succeeds against the owner-scoped feed API. Decoding token text alone does not confirm sign-in. Account text uses DOM text, not HTML. Interests navigation stays in the same tab. External article links retain their new-tab behavior.

The browser keeps the existing minimal Supabase session in session storage. This correction does not add shared persistent credential storage or store Google provider tokens. Same-tab navigation and reload retain the session; a newly opened independent tab may need sign-in. Existing open-tab login/logout notifications remain supported.

Expired sessions refresh with stale-response guards. Auth and reader requests use a 15-second abort bound. Failed checks expose retry instead of permanent loading. Local anonymous read/unread remains available; Save and preference writes require confirmed authentication.

## Verification gates

| Gate | Current evidence |
| --- | --- |
| Previous main-entry and missing-identity symptoms | A: reproduced at the public site and through actual Google login |
| Main click, Google transport, callback, automatic main return | B: local rendered-flow tests, including a click before deferred auth initialization; final exact served proof pending |
| Visible identity, Save and reload, Interests, logout | B: local browser contracts; final real Google main-entry loop pending |
| Refresh, corrupted storage, failed feed check and retry | B: local browser and executable auth contracts |
| Callback and remote logout request timeout | B: request held pending until its supplied abort signal fires, with the requested 15,000 ms bound asserted |
| Cross-tab logout privacy | B: private DOM and session cleared, local read/unread still works, and no post-logout authenticated writes |
| Desktop, short-window and phone navigation | B: navigation commit `3a8d0bb`, three viewport browser regressions passed (six combined navigation/render checks); desktop, short-desktop and phone screenshots independently viewed; final production scroll proof pending |
| Independent review, CI, deploy and exact served bytes | C: release gates pending |
| Product-owner M1 acceptance | OPEN. No claim that all M1 behavior or all bugs are accepted |

## Reproduction and test scope

The muted, read-only public reproduction loaded the real main page, clicked the real account link and asserted direct Google entry. It returned RED at the dedicated callback stop, without authenticating or writing data. The private operational script is not a public test account or credential fixture.

Local browser tests use isolated provider transport to exercise the complete rendered page path. They are not actual Google consent proof. The final release must run the real main-page link, actual Google choice, automatic callback return, visible account, Save/reload, same-tab Interests and logout on the exact served build.

On frozen code `98a064845603e3a6e978e56848dff27d6c69405e`, the deterministic CI floor passed 2,129 tests, with 12 skipped unavailable local database prerequisites and 11 network-marked cases deselected. Browser modules were explicitly excluded from that command and are recorded separately. No unchanged database runtime is counted as newly proven.

The full affected auth/reader browser suite passed 28 cases. The final callback retry delta passed two cases, and the final cross-tab status/logout delta passed five. The exact frozen integrated auth/reader/navigation browser rerun is pending at this snapshot.

Both Edge CI modes passed 159 cases each. Fixture regeneration produced no diff. Python source compilation, changed-file Ruff, JavaScript syntax, validated shipped configuration and offline HTML assertions passed. No configured standalone type checker was found. No new dependencies were installed.

Review/deploy identifiers and the final integrated browser count will be recorded after those gates complete. No database schema, provider configuration or publication changes are part of this source correction.
