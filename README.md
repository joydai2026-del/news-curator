# news-curator

Keywords in, ranked fresh headlines out.

You have topics you care about but don't want to actively track. Write the
keywords down once. A static page then keeps itself up to date with the latest
things published about them, most worthwhile first.

No server, no database, no account. A scheduled GitHub Action fetches, filters,
ranks and renders a single HTML file, and GitHub Pages serves it. Running it
costs nothing.

---

## Fork it and make it yours

Two files decide everything. Neither one is code.

1. **Fork this repo.**
2. **Edit [`topics.yaml`](topics.yaml).** Delete the examples, write your own:

   ```yaml
   topics:
     - name: Formula 1
       keywords:
         - Formula 1
         - F1
         - Grand Prix
       exclude:
         - F1 visa        # keeps US immigration news out of your racing page
   ```

3. **Optionally edit [`sources.yaml`](sources.yaml)** to add feeds or change how
   much you trust each one (`weight`).
4. **Turn on Pages**: Settings → Pages → Source: **GitHub Actions**.
5. **Run it once**: Actions → Curate → Run workflow.

Your page is live, and rebuilds on a schedule from then on.

Run it locally the same way:

```bash
pip install -r requirements.txt
python -m curator.pipeline --root . --out ./site
open site/index.html
```

---

## Where the headlines come from

| Tier | Status | Auth | How reliable, honestly |
|---|---|---|---|
| **Hacker News** (Algolia API) | On | None | High. No key, no quota hit. Verified working from a GitHub Actions runner. |
| **RSS feeds** (18 seeded) | On | None | High. The backbone. All 18 verified working from a GitHub Actions runner. |
| **Reddit** | **Off by default** | None (OAuth recommended) | Low to medium, and untested on CI. See below. |
| **X / Twitter** | **Not covered** | — | Indirect only, via Hacker News and RSS. See below. |

Those first two rows are not aspirational. A real scheduled run on a GitHub
runner fetched 330 items from Hacker News and 1,907 across all 18 feeds, with
zero failures, which is the same result as a residential connection. Datacenter
IPs are not a problem for either tier.

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

Four signals, all tunable in `sources.yaml` under `ranking:`.

| Signal | What it does |
|---|---|
| **Recency** | Exponential decay, 12-hour half-life by default. The dominant term. |
| **Keyword strength** | How many of a topic's keywords hit, plus a bonus when one appears near the front of the headline. |
| **Source weight** | Your trust dial, per source. Raise the outlets you rate. |
| **Cross-source echo** | A bonus when two or more distinct platforms carried the **same link**. |

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
linked to the address that source gave. Nothing is written, rewritten or
summarized by a machine. There is no LLM anywhere in this pipeline.

**It does not promise:**

- That a link is still live or still carries that title. Destination pages are
  never fetched, so a link may have moved, changed or died since the build.
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

---

## Layout

```
topics.yaml          the file you edit
sources.yaml         feeds, weights, ranking dials
curator/
  config.py          load + validate, loudly
  normalize.py       title cleaning, URL safety
  filter.py          strict whole-word keyword matching
  dedup.py           canonical URL, then title similarity
  rank.py            the four signals
  render.py          the static page
  pipeline.py        CLI entry point
  fetchers/          hn.py, rss.py, reddit.py
scripts/probe_sources.py   verify every source yourself
docs/plans/                the v1 spec, including its adversarial review
tests/                     140 tests, no network
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
