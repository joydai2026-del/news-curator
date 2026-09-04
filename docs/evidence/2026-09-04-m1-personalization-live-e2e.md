# Personalization production E2E receipt

- Date: 2026-09-04
- Environment: production
- Live surface: <https://news.joydong.org/>
- Planning source: [Fall 2026 AI Sprint](https://app.notion.com/p/Fall-2026-AI-Sprint-3c6442f52cf7801db1c2fe2e54d777f2)

This is code-adjacent deployment evidence. It does not define project order,
milestones, dates, or completion criteria.

## Result

The authenticated saved-interest path and the server-side ranking path passed on
production. The agreed accordion Reading Companion UI is still not implemented
in the production renderer, so this receipt does not claim that M1 is complete.

## Authenticated preference proof

- Read only the latest authorized News Curator sign-in email for the owner
  mailbox and consumed its one-time code once.
- Established an authenticated production session.
- Created the first saved-interest record.
- Read the owner-scoped record back successfully.
- Updated it through compare-and-swap and read back revision `1`.
- One saved interest was present for the production ranking check. Its value is
  intentionally omitted from public evidence.

The first production write returned HTTP 403 because the authenticated role
could not execute the validators used by the table check constraints. PR
[#5](https://github.com/joydai2026-del/news-curator/pull/5) fixed the database
permission boundary in
`supabase/migrations/202609040002_user_preferences_validator_grants.sql` by
moving the validators to a private schema and granting only the authenticated
role permission to execute them. Live SQL readback confirmed:

- authenticated schema usage: allowed
- authenticated validator execution: allowed
- anonymous validator execution: denied

## Server-side ranking proof

A dedicated, independently revocable Supabase secret key was created for the
GitHub Actions personalization environment. The value was never committed,
logged, or copied into this receipt. Personalization was then enabled for that
protected environment.

Production workflow run:
<https://github.com/joydai2026-del/news-curator/actions/runs/33890890818>

The run completed successfully. Its logs show:

- source snapshot validated: 3,715 items
- saved-interest ranking materialized successfully
- saved-interest ranking validated with `REQUIRE_PERSONALIZATION=true`
- `saved-interest ranking applied; published order changed: yes`
- rendered page verified: 304 linked story cards across 11 category tabs
- GitHub Pages deployment succeeded

The live page returned the deployed build with 304 stories. The same-run log
statement that personalization changed the published order is the ranking proof.
The interest value, matching story title, build locator, and card position are
intentionally omitted from this public receipt.

## Live-surface checks on the deployed build

| Check | Result |
|---|---|
| Page loads and exposes the deployed build label | PASS |
| 304 story cards render with source links | PASS |
| A selected category filter shows its 30 stories | PASS |
| Keyword search narrows to the expected story | PASS |
| Story opens and closes without navigation failure | PASS |
| `Personalize your feed` link opens the production sign-in page | PASS |
| Production sign-in page offers email-code authentication | PASS |

## Remaining product gap

Production still uses the older card renderer. The agreed accordion Reading
Companion shell, including its summary-first reading interaction, remains to be
integrated and live-verified before M1 can be called complete.
