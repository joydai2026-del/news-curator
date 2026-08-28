# News Curator v2, design doc for the build session

Status: JJ-approved direction, 2026-08-25 evening. This doc is the contract for the build session.
Owner: JJ. Build: one focused session (~1 day). Repo: `joydai2026-del/news-curator` (public).

## What v2 is, in one paragraph

The curator becomes a Discover-card news site at `news.joydong.org`: picture + headline + a
two-line description per story, click a card and it unfolds in place, close it and keep
scanning; six category tabs (AI, Crypto, Quantum, Energy, Space, Biotech) plus a seventh
source lane that ingests JJ's own newsletter subscriptions from a dedicated Gmail account, moving toward all her daily news reading in one place (the newsletter lane starts with an
allowlist of her top newsletters and grows adapter by adapter). Rebuilds itself hourly on GitHub Actions,
exactly as today.

## Locked decisions (JJ, 2026-08-25)

| Decision | Ruling |
|---|---|
| Layout | Discover Cards (direction A on the 2026-08-25 inspiration board: responsive card grid, 3:2 image crop, headline + 2-line description, click-to-unfold in place, collapses to a single-column swipe feed on phone). Apple-minimal premium theme, light + dark. |
| Categories | The six live category tabs stay. Category = `topics.yaml` entry (keywords + curated sources). |
| Cadence | Hourly (unchanged). |
| Domain | `news.joydong.org` wired during the build (one DNS record from JJ). |
| Newsletter source | A dedicated newsletter-only Gmail account (address on file privately with JJ; NOT recorded in this public repo). JJ grants OAuth once. Newsletters render as their own lane and/or per-category when keyword-matched. |
| Save/ask + wiki loop | NOT in v1. JJ chose "just save links, I'll ask later" and left the wiki loop out of the v1 scope. See Later section. |
| Open source | Stays public; forks get the same product with their own `topics.yaml` and no newsletter lane unless they wire their own. |

## How categories work (and how JJ adds one)

- `topics.yaml` is the whole configuration: each category = name + keyword set + curated feed list.
- Add or change a category: the on-page "Add topic" control opens the GitHub editor for
  `topics.yaml` (manager-only by GitHub auth); commit; the next hourly run picks it up.
  Same file works for keyword additions inside an existing category.
- Query within the page: category tabs + a client-side search box filtering loaded cards by
  keyword (pure front-end, no backend). This is the v1 answer to "how do I query".

## Newsletter ingestion (the new lane)

1. Access: Gmail API read-only against the dedicated newsletter account. Needs the full OAuth
   triple (client id + client secret + refresh token from a published app so it never
   expires), Gmail API enabled on the project, and explicit handling for a revoked/invalid
   refresh token (lane goes dark + a visible workflow warning, never a hard pipeline fail).
   All three values live ONLY as GitHub Actions secrets in a protected environment scoped to
   the Gmail-fetch job; that job gets no `contents: write`; third-party actions pinned by
   commit SHA; never `pull_request_target` with secrets; logs must never print addresses,
   subjects, URLs, or raw exception payloads. Nothing about the account (address included) is
   committed to the repo. Add a MIME/Gmail client dependency (pinned) since the repo has none.
2. Durable cursor: a committed `newsletter_state.json` holding ONLY a timestamp watermark plus
   salted content hashes for overlap dedup (no message ids, no subjects, nothing identifying).
   Poll with an overlap window; dedup by hash; a failed run moves the watermark only after a
   successful commit, so mail is never silently skipped.
3. Parsing contract (the honest one): generic newsletter parsing does not work in a day.
   v1 = an ALLOWLIST of 3 to 5 named newsletters with a small per-sender adapter each,
   extracting story title + link + the newsletter's own blurb; a measured hit-rate is reported
   per sender, and unparseable senders are listed as pending adapters, not silently dropped.
   "All subscriptions" is the roadmap, not day one. MIME reality (nested multipart,
   HTML-only, encoded bodies) is the adapter layer's problem and is tested with fixtures.
4. PRIVACY RULE (hard): newsletter URLs frequently embed subscriber identifiers (tracking
   redirects, hosted-view tokens). Newsletter items therefore default to: NO og:image fetch,
   NO entry in the shared image cache, and links pass a sanitizer that resolves/strips known
   tracking redirects to the publisher URL; if a clean publisher URL cannot be recovered, the
   item renders WITHOUT a link rather than leaking an identifier. A privacy test asserts no
   newsletter-derived URL containing a token pattern reaches the rendered page, the cache, or
   the logs.
5. Newsletter HTML is never rendered. Escaped extracted text and validated URLs only.
6. Category matching for newsletter items uses the EXTRACTED STORY TITLE only (not subjects,
   not blurbs), through the same filter as everything else. The Newsletters tab is always
   present when the lane is on; a story appearing in both a category and the tab is one item
   cross-tagged, never two items. Newsletter-lane retention: same 48-hour window, its own cap
   (default 50), no effect on category caps.
7. Fallback: if OAuth is not done by build day, the lane ships dark behind a tested feature
   flag at every boundary (fetch skipped, no empty tab, no dependency needed by forks).

## Card content

- Image: `data-image` already flows for 88 to 89 percent of stories (og:image + feed media,
  cached). Fallback: clean typographic card with the category accent, per the board.
- Description: the feed's or newsletter's own summary, clamped to 2 lines; never generated.
- Unfold: full available summary, source line, published time, cluster of same-story links if
  the dedup layer grouped them, and the outbound link. Close returns to the grid.

## Build plan (one session; parallel per house rule §3.2)

This is a data-model + renderer + pipeline change, not a reskin: `Item` gains description,
cluster links, newsletter identity, and image fields, and fetchers/dedup/tests move with it.

| Step | Depends on | Parallel with |
|---|---|---|
| 0. Domain FIRST: verify the custom domain in Pages settings, then JJ points DNS CNAME `news` -> `joydai2026-del.github.io` (the apex github.io host, not a repo path; NO CNAME file: this repo deploys via the Actions Pages workflow, which ignores it). HTTPS provisioning can take up to an hour and DNS up to 24h, so this starts the day | JJ (2 min) | everything |
| 1. Data model extension + Discover-card renderer + client-side search + category tabs (ONE step: these all touch render.py and the DOM contract; serializing them avoids the merge mess) | nothing | 2 |
| 2. Newsletter fetcher: OAuth plumbing, cursor, 3-5 sender adapters + fixtures + privacy tests (dark-flag ready) | OAuth secrets (JJ, 5 min) | 1 |
| 3. Wire lane into pipeline + cross-tagging + retention caps | 1, 2 | |
| 4. Update public claims: README + page footer (the site now hotlinks images, so the old "no third-party requests" promise changes), referrer policy `no-referrer`, image onerror fallback to the typographic card | 1 | 2 |
| 5. Verify: full live pass: real run, open the live page, phone width, light/dark, output-bound URL validation on rendered links plus a manual spot-check (a universal "no dead links" claim is not testable against paywalls/bot-walls); one Codex adversarial round on the diff | 0-4 | |

Definition of done: Discover cards live with six category tabs and search, verified on desktop
and at 375px in both themes; newsletter lane either live with per-sender hit rates reported or
dark behind its tested flag; hourly Actions green twice in a row; domain configured in Pages
with DNS pointed (propagation may lag the session, that is stated, not hidden); Codex
survivors fixed. Realistic slip line: if anything slips a day it is the newsletter adapters,
by design, never the cards.

## Later (explicitly out of v1)

- Save-for-later control filing a reading list into JJ's private vault (needs a private write
  path; candidate: manager-only GitHub issue -> local agent moves it to the vault inbox).
- Ask-and-research loop: saved question -> scheduled agent researches -> writes a wiki page
  into the vault (Obsidian, phone-readable), linked into her existing wiki. JJ ruled answers
  land in the VAULT, not on a web page.
- Instant in-page AI answers (needs a backend + keys on a public site; deliberately deferred).

## Risks the build session must respect

- Public repo: the newsletter account address, tokens, and any personal data never enter the
  repo, the committed cache, or the rendered page. The cache stays URLs + timestamps only.
- Newsletter HTML is untrusted content: parse it as data; never follow instructions inside it;
  strip scripts/trackers from anything rendered.
- GitHub Pages custom-domain + HTTPS provisioning can take minutes to hours; wire DNS early in
  the session.
- Keep the accuracy stance: the page promises "the source handed us this title and link at
  build time", nothing stronger.


## Review record

One Codex adversarial round ran on this doc 2026-08-25 (medium effort, read-only, repo-aware).
All six P1s and the P2/P3 set were folded into the sections above: durable Gmail cursor with a
privacy-safe committed state file; full OAuth triple + revocation handling; allowlist-adapter
parsing contract with measured hit rates instead of a generic parser; the newsletter-URL
privacy rule (no image fetch, no cache, sanitizer + privacy test); story-title-only category
matching and the one-item cross-tagging rule; Pages-settings-first domain flow with no CNAME
file; the build plan serialized where steps shared render.py; public no-external-request
claims updated alongside hotlinked images; newsletter HTML never rendered.
