# news-curator

Keywords in, ranked fresh headlines out.

You have topics you care about but don't want to actively track. Write the
keywords down once. A static page then keeps itself up to date with the latest
things published about them, most worthwhile first.

No server, no database, no account. A scheduled GitHub Action fetches, filters,
ranks and renders a single HTML file, and GitHub Pages serves it. Running it
costs nothing.

---

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

Two guards keep the fuzzy pass from deleting real stories. Differing numbers
veto a merge, so `iOS 18.6.1` and `iOS 18.6.2` stay separate. And the threshold
sits at 0.90 rather than 0.85, because `Apple releases iOS 18.6.1` and `Apple
delays iOS 18.6.1` score 0.875 with identical numbers. Character similarity
cannot tell "releases" from "delays". The cost is asymmetric: a missed merge
shows one extra row, a wrong merge silently deletes a story.

---

## What this promises, and what it does not

**It promises:** every headline is the text its source handed us at build time,
linked to the address that source gave. Each row also carries the address of the
preview image its publisher declared, which the page does not yet display.
Nothing is written, rewritten or summarized by a machine. There is no LLM
anywhere in this pipeline.

**It does not promise:**

- That a link is still live or still carries that title. Building the page reads
  a linked page only as far as the end of its head, to find the image tag the
  publisher put there for exactly this purpose. No article text is stored or
  summarized, so a link may have moved, changed or died since the build.
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

The renderer currently carries the address as a `data-image` attribute on each
row rather than drawing an `<img>`. The layout that uses it is a separate
decision; the data is there for whatever it turns out to be. A row whose
publisher declared no image carries no attribute, so a layout can tell "none
declared" from "declared as nothing".

---

## Layout

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
  render.py          the static page
  pipeline.py        CLI entry point
  fetchers/          hn.py, rss.py, reddit.py
scripts/probe_sources.py   verify every source yourself
docs/plans/                the v1 spec, its adversarial review, and the v1.1 notes
tests/                     262 tests, network blocked in conftest
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
