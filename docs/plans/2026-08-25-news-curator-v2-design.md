# Historical design note: News Curator v2 (2026-08-25)

> **Historical record only. This is not the current roadmap, build order, or
> definition of done.** The current plan, including milestones, dates, done
> tests, explainer gates, priorities, and product decisions, lives in
> [Notion](https://app.notion.com/p/Fall-2026-AI-Sprint-3c6442f52cf7801db1c2fe2e54d777f2).

This note preserves technical observations from a design session on
2026-08-25. It has no scheduling or prioritization authority.

## Historical product shape

The curator becomes a Discover-card news site at `news.joydong.org`: picture + headline + a
two-line description per story, click a card and it unfolds in place, close it and keep
scanning; six category tabs (AI, Crypto, Quantum, Energy, Space, Biotech) plus a seventh
source lane that ingests JJ's own newsletter subscriptions from a dedicated Gmail account, moving toward all her daily news reading in one place (the newsletter lane starts with an
allowlist of her top newsletters and grows adapter by adapter). Rebuilds itself hourly on GitHub Actions,
exactly as today.

## Historical design decisions recorded 2026-08-25

| Decision | Ruling |
|---|---|
| Layout | Discover Cards (direction A on the 2026-08-25 inspiration board: responsive card grid, 3:2 image crop, headline + 2-line description, click-to-unfold in place, collapses to a single-column swipe feed on phone). Apple-minimal premium theme, light + dark. |
| Categories | The six live category tabs stay. Category = `topics.yaml` entry (keywords + curated sources). |
| Cadence | Hourly (unchanged). |
| Domain | The design targeted `news.joydong.org`. |
| Newsletter source | The design used a dedicated newsletter-only Gmail account without recording its address in the public repo. |
| Save/ask + wiki loop | Not implemented in the recorded v1 scope. This row does not determine current sequence. |
| Open source | Stays public; forks get the same product with their own `topics.yaml` and no newsletter lane unless they wire their own. |

## Historical category design

- `topics.yaml` is the whole configuration: each category = name + keyword set + curated feed list.
- Add or change a category: the on-page "Add topic" control opens the GitHub editor for
  `topics.yaml` (manager-only by GitHub auth); commit; the next hourly run picks it up.
  Same file works for keyword additions inside an existing category.
- Query within the page: category tabs + a client-side search box filtering loaded cards by
  keyword (pure front-end, no backend). This is the v1 answer to "how do I query".

## Historical newsletter-ingestion constraints

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
3. Parsing contract: generic newsletter parsing was not considered reliable for this scope.
   The design used an allowlist of 3 to 5 named newsletters with a small per-sender adapter each,
   extracting story title + link + the newsletter's own blurb; a measured hit-rate is reported
   per sender, and unparseable senders are listed as pending adapters, not silently dropped.
   Broader subscription coverage was not part of the recorded implementation. MIME reality
   (nested multipart, HTML-only, encoded bodies) remained the adapter layer's problem and was
   tested with fixtures.
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
7. Fallback: the lane could remain dark behind a tested feature flag at every boundary
   (fetch skipped, no empty tab, no dependency needed by forks).

## Card content

- Image: `data-image` already flows for 88 to 89 percent of stories (og:image + feed media,
  cached). Fallback: clean typographic card with the category accent, per the board.
- Description: the feed's or newsletter's own summary, clamped to 2 lines; never generated.
- Unfold: full available summary, source line, published time, cluster of same-story links if
  the dedup layer grouped them, and the outbound link. Close returns to the grid.

## Historical implementation shape

The design treated the change as a data-model, renderer, and pipeline change,
not a reskin. It recorded description, cluster links, newsletter identity, and
image fields as coupled technical concerns. The original one-session build
order and definition of done were removed because Notion now controls sequence
and acceptance.

## Historical deferred concepts

The session recorded save, ask, wiki, and in-page answer concepts without
implementing them. Their appearance here is historical evidence, not a current
recommendation, milestone, or ordering decision.

## Historical operational risks

- Public repo: the newsletter account address, tokens, and any personal data never enter the
  repo, the committed cache, or the rendered page. The cache stays URLs + timestamps only.
- Newsletter HTML is untrusted content: parse it as data; never follow instructions inside it;
  strip scripts/trackers from anything rendered.
- GitHub Pages custom-domain and HTTPS provisioning were external deployment concerns.
- Keep the accuracy stance: the page promises "the source handed us this title and link at
  build time", nothing stronger.


## Review record

One Codex adversarial round ran on this doc 2026-08-25 (medium effort, read-only, repo-aware).
All six P1s and the P2/P3 set were recorded in the sections above: durable Gmail cursor with a
privacy-safe committed state file; full OAuth triple + revocation handling; allowlist-adapter
parsing contract with measured hit rates instead of a generic parser; the newsletter-URL
privacy rule (no image fetch, no cache, sanitizer + privacy test); story-title-only category
matching and the one-item cross-tagging rule; Pages settings with no CNAME file; public
no-external-request claims updated alongside hotlinked images; newsletter HTML never rendered.
