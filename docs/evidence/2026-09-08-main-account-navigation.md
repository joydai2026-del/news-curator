# Main-page account and navigation correction

Date: September 8, 2026. The reported main-entry and account-visibility defects are corrected and live-proven. Product-owner acceptance remains open. Notion remains read-only planning authority. Earlier release receipts remain historical evidence, not proof of this main-page flow.

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
| Main click, Google transport, callback, automatic main return | A: a fresh unsigned browser tab used the actual main Sign in with Google link, completed returning Google sign-in, and returned automatically to main with visible identity. Early-click fallback also passes B local proof |
| Visible identity, Save and reload, Interests, logout | A: actual main flow, owner-scoped state hydration, open/read, Save, reload, same-tab Interests, logout, unsigned main return and returning main login all passed |
| Refresh, corrupted storage, failed feed check and retry | B: local browser and executable auth contracts |
| Callback and remote logout request timeout | B: request held pending until its supplied abort signal fires, with the requested 15,000 ms bound asserted |
| Cross-tab logout privacy | B: private DOM and session cleared, local read/unread still works, and no post-logout authenticated writes |
| Desktop, short-window and phone navigation | A: real published stories at 1440x1000, 1100x560 and 390x844. Bottom navigation stayed visible, last keyboard chip stayed fully contained, page scroll did not move and horizontal overflow was zero. Screenshots were opened and inspected. B local viewport regressions also pass |
| Independent review, CI, deploy and exact served bytes | A: successful production workflow and exact public HTML/callback/static-asset comparison; B: exact-code independent review and CI passed |
| Test story restored after this live flow | A: Unsave and Mark unread completed, then reload retained the original unread/unsaved flags. Existing interests matched the pre-test value and were never edited |
| Product-owner M1 acceptance | OPEN. No claim that all M1 behavior or all bugs are accepted |

## Reproduction and test scope

The muted, read-only public reproduction loaded the real main page, clicked the real account link and asserted direct Google entry. It returned RED at the dedicated callback stop, without authenticating or writing data. The private operational script is not a public test account or credential fixture.

Local browser tests use isolated provider transport to exercise the complete rendered page path. They are not actual Google consent proof. The final release must run the real main-page link, actual Google choice, automatic callback return, visible account, Save/reload, same-tab Interests and logout on the exact served build.

On frozen code `98a064845603e3a6e978e56848dff27d6c69405e`, the deterministic CI floor passed 2,129 tests, with 12 skipped unavailable local database prerequisites and 11 network-marked cases deselected. Browser modules were explicitly excluded from that command and are recorded separately. No unchanged database runtime is counted as newly proven.

The exact frozen integrated auth/reader/navigation browser suite passed all 31 cases in 356.22 seconds. The earlier affected auth/reader suite passed 28 cases; the final callback retry and cross-tab status/logout deltas also passed two and five cases respectively. Independent security review passed the exact product code, including 13 separately executed focused cases. Root source, navigation-visual and executable auth review also passed.

Both Edge CI modes passed 159 cases each. Fixture regeneration produced no diff. Python source compilation, changed-file Ruff, JavaScript syntax, validated shipped configuration and offline HTML assertions passed. No configured standalone type checker was found. No new dependencies were installed.

No database schema, provider configuration or new dependency was changed. The approved deployment used the existing publication workflow, followed by its normal scheduled run. No extra manual publication was dispatched.

## Production release and real account flow

[PR 16](https://github.com/joydai2026-del/news-curator/pull/16) merged as `3c0ff50067db72b116fe8cd7441adad799800629`. All three checks passed on PR head `a3e06f1` in [CI 34274092658](https://github.com/joydai2026-del/news-curator/actions/runs/34274092658), and [post-merge CI 34274597942](https://github.com/joydai2026-del/news-curator/actions/runs/34274597942) passed.

The automatic push [Curate run 34274597919](https://github.com/joydai2026-del/news-curator/actions/runs/34274597919) completed source snapshot, newsletter, build, state persistence, deployment and archive finalization. Its main HTML matched its archive candidate, and configured callback, auth/reader scripts, styles and privacy page matched deployed source. Anonymous latest-publication read passed at publication14 with164 archived stories. The three live navigation screenshots were bound to that exact candidate.

The normal [scheduled run 34274877413](https://github.com/joydai2026-del/news-curator/actions/runs/34274877413) then succeeded on the same source. Its own candidate and all public assets passed the same exact verification, at publication15 with167 stories. This is the final verified served edition. The merged product is byte-equivalent to reviewed code; only docs and existing cache files differ. Pre-merge cache-only automation changes were preserved.

The actual Google walkthrough on this reviewed production source began in a fresh unsigned tab at the main page. The actual main link completed returning Google sign-in and returned automatically to main with visible account identity. The existing Google grant completed automatically; no new consent or manual account chooser was exercised in this correction. A real initially unread/unsaved story became read immediately on opening, Save completed, and reload retained identity plus read/saved state. Interests opened in the same tab without editing interests. Sign out cleared private flags and returned main to its unsigned state. A subsequent actual main-page Google login restored the saved story state. No manufactured session or routed provider response was used for this A proof.

Cleanup completed through the real UI. A fresh main-page Google login recovered the same saved/read state, then Unsave and Mark unread restored the original flags. Reload retained unread/unsaved state. The read headline rendered gray at `rgb(120,129,124)` and returned to black at `rgb(0,0,0)` when unread. The existing interests text matched its read-only baseline and was never edited. M1 product-owner acceptance and unexercised real error/cancellation paths are not silently marked complete by this functional correction.
