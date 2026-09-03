# news-curator

Keywords in, ranked fresh headlines out.

Current plan: [Fall 2026 AI Sprint in Notion](https://app.notion.com/p/Fall-2026-AI-Sprint-3c6442f52cf7801db1c2fe2e54d777f2)

You have topics you care about but don't want to actively track. Write the
keywords down once. A static page then keeps itself up to date with the latest
things published about them, most worthwhile first.

No server, no database, no account. A scheduled GitHub Action fetches, filters,
ranks and renders a single HTML file, and GitHub Pages serves it. Running it
costs nothing.

---

## What the page looks like

A responsive grid of Discover cards. Each card is a picture at a 3:2 crop, the
headline under it, two lines of the source's own description, and a foot with
the source, the age, and a "2 sources" marker when more than one platform
carried the same link.

- **Click a card and it unfolds in place**, spanning the row, showing the full
  description, the exact publication time, the keywords that matched, any other
  outlets that covered the same story, and the outbound link. Click again, or
  press Escape, and the grid closes back up. The toggle is a real button, so
  the keyboard works.
- **Category tabs** filter the grid, and each tab keeps its own exact ranking. A
  story belonging to three categories is **one card cross-tagged with three
  slugs**, never three copies, which is why the story count on the page is a
  count of stories.
- **A search box** filters the visible cards by title and description as you
  type. It is pure front end: there is no backend to search, and none is wanted.
- **A story with no picture gets a typographic card** in its category's accent
  colour rather than a hole in the grid. Hacker News and Show HN items almost
  never carry an image, and an image that fails to load in your browser lands on
  the same panel.
- One column on a phone, light and dark, no web fonts, no framework.

## What it covers

Six sections, each with its own keyword list **and** its own curated feeds:
**AI**, **Crypto**, **Quantum computing**, **Energy and nuclear** (including
small modular reactors), **Space technology**, **Biotechnology**.

Both halves matter, and they catch different stories:

- **Keywords** pull a matching headline in from any feed, so a big AI story in a
  general publication reaches the AI section.
- **Curated feeds** are listed under a category in
  [`topics.yaml`](topics.yaml). Listing a feed there is a claim that the
  publication is single-subject, so its stories appear without needing a
  keyword. That is the only way a headline like *"Vogtle 4 enters commercial
  operation"* ever reaches the energy section: it is unmistakably energy news
  and contains not one energy keyword.

`exclude` still vetoes a curated feed's item, so the escape hatch works on both.

---

## Adding a topic or keyword

You do not need a developer, a checkout, or an account system.

1. Click **"Add a topic or keyword"** in the page footer. It opens
   [`topics.yaml`](topics.yaml) in GitHub's own editor.
2. Find your section, add a line under `keywords`, commit.
3. If you can commit to the repository, the page rebuilds on that commit. If you
   cannot, GitHub turns your edit into a pull request instead, and it goes live
   once the owner merges it.

GitHub decides who is allowed to save it, which is the point: whoever can push
edits directly, and whoever cannot gets the fork-and-pull-request flow. There is
no form to secure and no new place for a secret to leak, which is why this is
one link rather than a service.

Locally it is the same file:

```yaml
categories:
  - name: Energy and nuclear
    keywords:
      - small modular reactor
      - HALEU              # <- your new line
```

---

## Fork it and make it yours

Two files decide everything. Neither one is code.

1. **Fork this repo.**
2. **Edit [`topics.yaml`](topics.yaml).** Replace the six categories with your
   own. A category needs a `name` and at least one of `keywords` or `sources`:

   ```yaml
   categories:
     - name: Formula 1
       keywords:
         - Formula 1
         - Grand Prix
       exclude:
         - F1 visa        # keeps US immigration news out of your racing page
       sources:           # optional: feeds that are ALL about this topic
         - {id: autosport, name: Autosport, url: "https://www.autosport.com/rss/feed/f1"}
   ```

3. **Optionally edit [`sources.yaml`](sources.yaml)** for the shared feed pool
   (multi-subject publications, matched by keyword), the ranking dials, and the
   preview-image settings.
4. **Turn on Pages**: Settings → Pages → Source: **GitHub Actions**.
5. **Run it once**: Actions → Curate → Run workflow.

Your page is live, and rebuilds on a schedule from then on.

Run it locally the same way:

```bash
pip install -r requirements.txt
python -m curator.pipeline --root . --out ./site
open site/index.html
```

## Where the headlines come from

| Tier | Status | Auth | How reliable, honestly |
|---|---|---|---|
| **Hacker News** (Algolia API) | On | None | High. No key, no quota hit. Verified working from a GitHub Actions runner. |
| **RSS feeds** (58 seeded) | On | None | High. The backbone. All 58 probed live on 2026-08-28, 58/58 reachable. |
| **Reddit** | **Off by default** | None (OAuth recommended) | Low to medium, and untested on CI. See below. |
| **X / Twitter** | **Not covered** | — | Indirect only, via Hacker News and RSS. See below. |

Those first two rows are not aspirational. A real run fetched 134 items from
Hacker News and 2,746 across all 58 feeds, with zero failures. Datacenter IPs are
not a problem for either tier.

Feeds come from two places, and the split is a claim about the PUBLICATION rather
than about its quality:

- **Curated feeds** (40) live under a category in `topics.yaml`. Single-subject
  publications, whose every story belongs in that section.
- **The shared pool** (18) lives in `sources.yaml`. Multi-subject publications
  whose front page covers several sections and none of them exclusively, so
  keywords decide where each story lands.

TechCrunch appears in both: its AI section feed is curated under AI, and its
front page is shared, so a TechCrunch space story still reaches Space.

Verify the first two yourself, right now, on your own machine:

```bash
python3 scripts/probe_sources.py
```

That prints a live status, byte count and parsed-entry count per source. The
numbers in this README came from that script; none of them are hand-written.

### Reddit is hard

Measured from a residential connection:

| Endpoint | Result |
|---|---|
| `/r/<sub>/hot.json` | **HTTP 403** with every User-Agent tried. Closed. |
| `/r/<sub>/.rss`, six requests fast | **HTTP 200 once, then HTTP 429** on everything after |
| `/r/<sub>/.rss`, spaced 60 s apart | **HTTP 200 every time**, 10 entries each |

The 429 is a short-window burst limit, not a ban. Reddit over RSS works if you
go slow, and only if you go slow. So the fetcher requests subreddits one at a
time with a 30-second gap and stops the whole tier on the first 429 rather than
hammering.

It ships **disabled** for two reasons worth saying plainly:

1. GitHub Actions runners use shared datacenter IPs, which is the traffic class
   Reddit blocks hardest. That cannot be tested from a laptop, so it is an
   unverified risk rather than a claim in either direction.
2. Reddit points programmatic users at registered OAuth apps. Unauthenticated
   RSS polling is tolerated, not sanctioned. If you want Reddit coverage you can
   rely on, register your own app.

Turning it on is harmless either way: a blocked tier is skipped with a note on
the page, never a failed build.

### X / Twitter is not covered

There is no affordable read API, scraping violates the terms of service, and
neither belongs in a repo meant to be published. So this tool does not ingest X,
and does not pretend otherwise.

X content still reaches the page **indirectly**: things being discussed there
usually surface on Hacker News or in the tech feeds within hours, and the
cross-source signal below is good at catching exactly that. This is genuinely
weaker than direct ingestion and slower by hours. It is not a substitute.

---

## How ranking works

Five signals, all tunable in `sources.yaml` under `ranking:`.

| Signal | What it does |
|---|---|
| **Recency** | Exponential decay, 12-hour half-life by default. The dominant term. |
| **Keyword strength** | How many of a topic's keywords hit, plus a bonus when one appears near the front of the headline. |
| **Source weight** | Your trust dial, per source. Raise the outlets you rate. |
| **Cross-source echo** | A bonus when two or more distinct platforms carried the **same link**. |
| **Curated source** | A story from a category's own feed with no keyword hit scores `native_source_score` (0.5) rather than zero, because the feed being single-subject is real evidence. Below a genuine keyword hit on purpose. |

Deduplication runs in two passes: identical canonical URL (certain), then
similar titles (a guess). Only the certain pass feeds the "N sources" badge, so
that badge means what it says.

A merge that collapsed two DIFFERENT addresses records the one it folded away,
and the unfolded card lists them under "also covered by", capped at six. That is
a fact about an address rather than a claim about corroboration, which is why it
survives the fuzzy pass when the badge does not. It also makes the fuzzy pass
auditable by eye for the first time: a wrong merge is now visible on the page
instead of invisible in a log line. A description is never inherited from a
merged-away row, because a blurb is prose one outlet wrote about their own
piece, and printing it under another outlet's headline is the same
misattribution the aggregator rule exists to prevent.

Two guards keep the fuzzy pass from deleting real stories. Differing numbers
veto a merge, so `iOS 18.6.1` and `iOS 18.6.2` stay separate. And the threshold
sits at 0.90 rather than 0.85, because `Apple releases iOS 18.6.1` and `Apple
delays iOS 18.6.1` score 0.875 with identical numbers. Character similarity
cannot tell "releases" from "delays". The cost is asymmetric: a missed merge
shows one extra row, a wrong merge silently deletes a story.

---

## What this promises, and what it does not

**It promises:** every headline is the text its source handed us at build time,
linked to the address that source gave, and every description is the summary
that source wrote for its own story. Each card also shows the preview image its
publisher declared, hotlinked from the publisher. Nothing is written, rewritten
or summarized by a machine. There is no LLM anywhere in this pipeline.

**It does not promise:**

- That your browser makes no third-party requests. It used to: v1.1 carried the
  image address and drew nothing. The page now draws the picture, so your
  browser fetches it from the publisher's server. It is sent with
  `referrer: no-referrer`, at the document level and per image, so the publisher
  is not told which page it was fetched from.
- That a link is still live or still carries that title. Building the page reads
  a linked page only as far as the end of its head, to find the image tag the
  publisher put there for exactly this purpose. No article text is stored or
  summarized, so a link may have moved, changed or died since the build.
- That an "also covered by" list is complete. It names the outlets the
  deduplicator folded into this story on this run, which is not the same as
  every outlet that covered it.
- That anything in a linked article is true. No claim is checked.
- That a keyword match means the story is genuinely *about* your topic. Matching
  proves a phrase appears in a headline. That is a weaker claim, it is checkable
  by eye, and `exclude` exists for when it is not enough.
- That the page is exactly an hour old. GitHub delays and drops scheduled runs
  under load, and disables them entirely after 60 days of repository inactivity.
  The page shows its real build time and says so when that is over three hours.

Rows marked **via** come from an aggregator (Hacker News, Reddit, Lobsters),
where the headline was written by whoever submitted the link rather than by the
publisher. When a publisher's own feed carried the same link, the publisher's
headline wins.

That narrowness is the point. A confidently-worded machine summary that
misrepresents an article is the expensive failure. A wrong ranking is a bad day.

### About the feed list

Every seeded feed is reachable and parses. **Reachable is not the same as
licensed to republish**, and only the first is testable by a script. The default
list sticks to feeds publishers promote for reading and syndication, and leaves
out outlets whose published terms govern RSS reuse specifically. If you add a
feed, that decision is yours.

Each exclusion is recorded with its actual reason at the top of
[`sources.yaml`](sources.yaml) rather than under a blanket policy sentence. Two
worth knowing, because they are not about terms at all:

- **arXiv** is reachable and parses, and is left out on editorial grounds. A few
  hundred same-day preprints would crowd a recency-ranked AI section out of
  existence. It is commented in `sources.yaml` if you disagree.
- **Fierce Biotech and Fierce Pharma** return 200, parse cleanly, and carry 25
  entries each, all of which are dropped: their `<pubDate>` is
  `Aug 28, 2026 10:30am` rather than RFC 822, with no timezone. Undated items are
  dropped rather than stamped "now", and guessing a timezone would misorder a
  recency-ranked page by hours.

---

## Pictures

Each story carries the preview image its publisher declared for it, found in
whichever of two places is cheaper:

1. **The feed**, via `media:content`, `media:thumbnail` or an image enclosure.
   That costs no extra request at all, and it is the only way to get an image
   from publishers who serve their feed happily and refuse a direct article
   fetch. It covers roughly half the feeds here.
2. **The `og:image` tag** on the article, for rows the feed left bare. The
   response is read only as far as the end of the head and then dropped, so the
   article body is never parsed or stored (the last chunk read can overlap the
   start of it, which is why this says "as far as", not "only the head"). It
   runs only for stories that survived ranking.

Answers are cached in `image_cache.json`, committed to the repo and keyed by
canonical URL. A found image and a definitive "this page declares none" are
both kept, so an hourly job does not ask the same question again: a live run
resolved 157 of 180 rows, and the next run fetched nothing. A refusal or a
timeout is not definitive, so it is retried after 24 hours, and a link that
stops appearing is pruned after 45 days and would be looked up again if it came
back.

Images are **hotlinked, never re-hosted**. Nothing is downloaded, resized or
re-encoded, so the publisher keeps their CDN and can change or withdraw the image
at any time.

The card draws it as a real `<img>`, lazily, with `referrerpolicy="no-referrer"`
and a document-level `<meta name="referrer" content="no-referrer">`. The address
also stays on the article as a `data-image` attribute, which is how the deploy
workflow counts image coverage. A card whose publisher declared no image carries
no attribute at all, so "none declared" is still distinguishable from "declared
as nothing", and that card renders the typographic panel instead. So does one
whose image fails to load in your browser: the panel is the layer underneath
every image, not a second code path that could rot.

Newsletter items never load an image and never enter the cache, because a
newsletter URL can carry a subscriber identifier. That rule is enforced in the
enricher as well as in the pipeline, on purpose: a privacy rule living in one
layer is one refactor away from being gone.

---

## Repository layout

```
topics.yaml          the file you edit: six categories, keywords + curated feeds
sources.yaml         shared feed pool, weights, ranking dials, image settings
image_cache.json     preview images already looked up. Written by the build.
curator/
  config.py          load + validate, loudly
  normalize.py       title cleaning, URL safety
  filter.py          keyword matching, and category-native membership
  dedup.py           canonical URL, then title similarity
  rank.py            the five signals
  images.py          og:image parsing, and the committed cache
  render.py          the static page: cards, tabs, search, unfold
  pipeline.py        CLI entry point
  fetchers/          hn.py, rss.py, reddit.py
scripts/probe_sources.py   verify every source yourself
docs/plans/                clearly labeled historical implementation/design notes
tests/                     the suite, network blocked in conftest
```

Three dependencies, all pinned: `requests`, `feedparser`, `PyYAML`. A tool that
runs unattended every hour should have a dependency surface small enough to
audit.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

---

## Custom domain

Commit a `CNAME` file containing your domain at the **repo root**. The build
copies it into the published output on every run, so the domain does not reset
on each deploy. Then point a DNS `CNAME` record at `<user>.github.io` and set
the domain under Settings → Pages. DNS is the part no script can do for you.

## License

MIT. See [LICENSE](LICENSE).
