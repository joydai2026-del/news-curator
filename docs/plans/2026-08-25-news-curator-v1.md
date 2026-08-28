# News Curator v1 — Spec

Status: draft for build
Date: 2026-08-25
Repo: public from first commit

---

## 1. Product goal

You have topics you care about but don't want to actively track. You write the
keywords down once. A web page then keeps itself up to date with the latest
things published about those keywords, ranked so the most worthwhile item is at
the top.

The page is static, loads instantly, and is rebuilt every hour by a scheduled
job. There is no server, no database, and no account. Anyone can fork the repo,
replace the keyword file with their own, and have their own copy running on
their own domain within minutes.

Two audiences, deliberately in this order:

1. **The owner.** One page, their topics, genuinely current, genuinely accurate.
2. **Anyone who forks it.** The topic list is data, not code. Nothing about the
   owner's interests is compiled into the program.

### The accuracy promise (deliberately narrow)

> Every headline shown was really published under that title at that link, and
> was really surfaced by the source we attribute it to.

That is the whole promise. We are a **router**, not a summarizer. v1 writes no
prose about any article. It never paraphrases a claim, never says what an
article "argues", never merges two sources into a sentence. The failure mode
this avoids is the expensive one: a confidently-worded AI summary that
misrepresents an article the reader then repeats. A wrong *ranking* is a bad
day; a wrong *claim* is a retraction.

What the promise explicitly does **not** cover: whether the linked article is
itself true, whether the outlet is reputable, or whether our ranking put the
genuinely most important story first. Those are editorial judgments we are not
making, and the page says so in the footer.

---

## 2. v1 scope

**In scope**

- Config-driven topics: `topics.yaml`, a list of topics, each with keywords.
- Config-driven sources: `sources.yaml`, a list of RSS feeds with weights.
- Three fetch tiers: Hacker News (API), RSS feeds, Reddit (best-effort).
- Strict local keyword filtering, applied on top of whatever a source returns.
- Ranking: recency + keyword strength + source weight + cross-source echo.
- Deduplication by canonical URL, then by title similarity.
- A single static `index.html`, one column, light/dark, no images, no tracking.
- Hourly GitHub Actions rebuild, publishing to GitHub Pages.
- A source-health line on the page: which tiers succeeded on this run.

**Explicitly out of scope for v1** (each of these is a real temptation)

| Not doing | Why |
|---|---|
| LLM summaries or "why this matters" blurbs | Breaks the accuracy promise. See §1. |
| Direct X / Twitter ingestion | No affordable API. See §3.4. |
| Full-text article fetching | Slow, fragile, paywall-hostile, and unnecessary for a headline router. |
| Per-user accounts, saved state, read/unread | There is no server. Anyone wanting this should fork. |
| Email or push digests | Different product. The page is the product. |
| Semantic / embedding search | Keyword matching is auditable and explains itself. Embeddings would make "why is this here?" unanswerable, which conflicts with §1. |
| Comment threads, scores, or discussion | We link out. |
| Historical archive / search over past runs | v1 renders the current window only. |

**Non-goal worth stating plainly:** this is not a news aggregator competing with
Techmeme or Google News. It is a personal keyword tap. If the keyword list is
empty, the page is empty, and that is correct behavior.

---

## 3. Source tiers — feasibility, verified by live probes

All probes below were run from a residential macOS machine on 2026-08-25.
Every claim in this section is either **[VERIFIED]** (a probe I ran and read the
result of) or **[UNVERIFIED]** (stated as an expectation, not a fact).

### 3.1 Tier A — Hacker News via Algolia  **[VERIFIED — works, no auth]**

Endpoint: `https://hn.algolia.com/api/v1/search` and `.../search_by_date`

Probe results:

| Check | Result |
|---|---|
| Unauthenticated request | HTTP 200, valid JSON |
| Latency | ~0.23–0.33 s per call |
| 10 rapid sequential calls | 10 ok, 0 failures, no 429 |
| Time filtering via `numericFilters=created_at_i>…` | works |
| Score filtering via `points>=N` | works |
| Combined filter, 48 h + `points>=20`, query `AI` | 40 hits, top item 990 points |

This is the strongest tier: no key, no quota observed, structured JSON with
`title`, `url`, `points`, `num_comments`, `created_at_i`, `objectID`.

**Verified accuracy hazard — this shapes the whole design.** Algolia does fuzzy
and prefix matching. The query `AI` against a 48-hour window returned, inside
the top five by points:

> "Two German airport workers die of malaria after 'mosquito arri…" — 185 points

That story has nothing to do with AI. If we trusted the search engine's notion
of a match, the page would ship visible nonsense on day one, on a page whose
entire selling point is accuracy.

**Consequence:** the remote query is treated as a *candidate generator only*.
Every candidate is re-checked locally against the topic's keywords with our own
strict matcher (§4.2) before it is allowed onto the page. A candidate that the
API returned but our matcher rejects is dropped silently. This rule applies to
every tier, not just HN.

**Endpoint choice:** `search_by_date` returns genuinely newest items, but they
are mostly 1–2 point submissions that nobody has seen yet (verified: top three
had 1, 1, and 1 point). `search` returns relevance-and-popularity ranked items.
v1 queries **both** and merges: `search` with a points floor for quality,
`search_by_date` without one for freshness. Dedup (§4.4) collapses the overlap.

### 3.2 Tier B — RSS feeds  **[VERIFIED — works, this is the backbone]**

Candidate feeds were probed in **two rounds** with a descriptive User-Agent.
The arithmetic matters, so stated exactly: round 1 probed 20 candidates and 17
returned HTTP 200 with XML; round 2 probed 10 more and 8 passed. That is **25
reachable feeds out of 30 candidates**, of which **18 are seeded** into
`sources.yaml`. The gap between 25 reachable and 18 seeded is the rights
question in the note below, not a measurement error.

Reproduce all of it yourself, any time, with `python3 scripts/probe_sources.py`,
which prints live status, byte count and parsed-entry count per source. The
table below is a point-in-time record; the script is the ground truth.

Reachable and seeded:

| Feed | Bytes |
|---|---|
| techcrunch.com/feed/ | 17,710 |
| theverge.com/rss/index.xml | 39,609 |
| arstechnica.com/feed/ | 81,944 |
| wired.com/feed/rss | 46,231 |
| technologyreview.com/feed/ | 139,776 |
| spectrum.ieee.org/feeds/feed.rss | 369,454 |
| simonwillison.net/atom/everything/ | 102,636 |
| openai.com/news/rss.xml | 702,899 |
| deepmind.google/blog/rss.xml | 74,117 |
| blog.google/technology/ai/rss/ | 32,381 |
| news.ycombinator.com/rss | 11,371 |
| export.arxiv.org/rss/cs.AI | 681,073 |
| lobste.rs/rss | 16,269 |
| theregister.com/headlines.atom | 248,032 |
| venturebeat.com/feed/ | 81,286 |
| restofworld.org/feed/latest/ | 14,813 |
| 404media.co/rss/ | 110,280 |
| stratechery.com/feed/ | 82,939 |

Reachable, and deliberately **NOT** seeded:

| Feed | Probe | Why it is not in the defaults |
|---|---|---|
| `feeds.bbci.co.uk/news/technology/rss.xml` | 200, 14,484 B | The BBC's terms govern RSS reuse specifically: unmodified presentation with a prominent credited link for personal use, permission required otherwise. This tool filters and reranks, which is not "unmodified". |
| `rss.nytimes.com/…/Technology.xml` | 200, 67,475 B | Has its own republishing terms. |
| `ft.com/technology?format=rss` | 200, 11,611 B | FT licenses RSS per client, with usage limits. A public `?format=rss` response is not a grant. |

**"HTTP 200" is reachability, not permission.** Those are different questions
and only the first is testable by a script. This distinction was missed in the
first draft of this spec, which described feeds as "publisher-sanctioned"
purely because they returned 200. That was wrong and is corrected here. The
default list now sticks to feeds publishers promote for reading and syndication;
anyone adding one of the above owns that decision.

Verified **failing**, and therefore not seeded either:

| Feed | Result | Note |
|---|---|---|
| `anthropic.com/rss.xml` | 404 | Also tried `/news/rss.xml` and `/feed.xml`, both 404. Anthropic appears to publish no public RSS feed. Listed here so nobody re-adds it on a guess. |
| `reuters.com/technology/rss` | 401 | Requires a licensed feed. |
| `axios.com/technology/feed` | 403 | Blocks non-browser agents. |

Some feeds are large (arXiv cs.AI is 681 KB, OpenAI 703 KB). Fetching ~18 of
these hourly is a few MB per run, which is trivial, but each request still needs
a timeout and a size cap so one pathological feed cannot hang the run. A feed
that hits the size cap is treated as a FAILURE, not a partial success:
publishing half a truncated feed while reporting the source healthy is a quiet
lie.

**Feeds are the backbone precisely because they are boring.** They are a stable,
unauthenticated, publisher-published interface. No tier we depend on should be
more fragile than this one.

### 3.3 Tier C — Reddit  **[VERIFIED — substantially blocked; best-effort only]**

This is the tier where an honest spec differs most from an optimistic one.

Probes, from a residential IP:

| Endpoint | User-Agent | Result |
|---|---|---|
| `reddit.com/r/technology/hot.json` | `python-requests/2.32.3` | **HTTP 403** (189 KB HTML block page) |
| `reddit.com/r/technology/hot.json` | descriptive project UA | **HTTP 403** (identical) |
| `reddit.com/r/programming/hot.json` | both UAs | **HTTP 403** |
| `reddit.com/r/MachineLearning/hot.json` | both UAs | **HTTP 403** |
| `reddit.com/r/technology/.rss` | descriptive UA | **HTTP 200**, 10,101 bytes — first request only |
| `reddit.com/r/programming/.rss` | descriptive UA | **HTTP 429** |
| `reddit.com/r/MachineLearning/.rss` | descriptive UA | **HTTP 429** |
| `old.reddit.com/r/*/.rss` | descriptive UA | HTTP 302 redirect to `www` |
| Follow-up: 4 subreddits, 6 s apart | descriptive UA | **429 on all four** |
| Recovery: 4 requests at 0/60/120/300 s | descriptive UA | **200 on all four**, 19,612 bytes, 10 entries each |

Read plainly: **the `.json` endpoints are closed to us entirely.** A burst of
about six `.rss` requests was enough to earn a 429 on every subsequent request,
including to a subreddit that had just succeeded.

**But the last row changes the conclusion, so it is worth being precise: the 429
is a short-window burst limit, not a ban.** Once the throttle cooled off, four
consecutive requests spaced 60 to 300 seconds apart every one returned HTTP 200
with a full 10-entry feed. Reddit is therefore *workable* over RSS at a low
request rate, and only at a low request rate. Five subreddits at 30-second
spacing is about two and a half minutes per run, which is nothing for an hourly
job.

The honest summary is not "Reddit is blocked". It is: **the JSON API is closed,
the RSS feed works if you are patient, and we cannot verify either from a
datacenter IP.**

**[UNVERIFIED] GitHub Actions runners may behave worse.** Runners use shared
Azure datacenter IP ranges, which is exactly the traffic class Reddit's blocking
is aimed at. I cannot verify this from a laptop, and I am not going to assert it
as fact in either direction. It is a risk, and the design must survive it being
true.

**Design consequence — Reddit is an opt-in tier that fails politely:**

1. Reddit is **disabled by default** in `sources.yaml`, with a pointer to this
   section. A fresh fork does not silently depend on a source we could not
   verify from CI.
2. When enabled, the fetcher requests subreddits **serially with a 30-second
   delay** (6 s earned a 429, 60 s did not; 30 s is the compromise), respects
   `Retry-After`, and stops the entire Reddit tier for the run on the first 429
   rather than hammering through the list. Partial results collected before the
   throttle are kept, not discarded.
3. **Skip-with-note, never fail-the-run.** A blocked Reddit tier degrades the
   page, it does not break the build. The rendered page carries a source-health
   line naming any tier that returned nothing, so a silently-dead source is
   visible rather than looking like a quiet news day. This is the important
   part: a curator that quietly shows less is worse than one that says
   "Reddit: unavailable this run".
4. **The supported path is documented, not implemented in v1.** Reddit's
   sanctioned route is a registered app with OAuth2 client-credentials, which
   raises the rate ceiling substantially. That requires each fork's owner to
   register their own app and add two repository secrets. v1 ships the
   documentation for this in the README and a config hook for it; wiring the
   OAuth flow is the first v2 item.

**Bluntly: v1's Reddit coverage may be zero on GitHub Actions, and is good on a
laptop.** The page is designed to be good without it either way. Anyone who
wants Reddit coverage they can rely on should read the OAuth note in the README
and expect to do setup.

### 3.4 Tier D — X / Twitter  **[NOT FEASIBLE in v1 — stated plainly]**

There is no affordable API. X's free tier does not permit reading a timeline or
search at all, and the paid tiers start at a monthly cost that is absurd next to
a static site that otherwise costs nothing to run. Scraping is against the
terms of service, is aggressively blocked, and would make this repo an
uncomfortable thing to publish publicly.

So v1 does not ingest X, and the README says so in the source table rather than
implying coverage we do not have.

**What we get instead, honestly labeled:** X content reaches the page
*indirectly*. When something is being discussed on X, it typically surfaces on
Hacker News or in the tech feeds within hours, and our cross-source echo signal
(§4.3) is specifically good at catching that. This is genuinely weaker than
direct ingestion and slower by hours. It is not a substitute and will not be
described as one.

**v2-if-a-source-exists.** Direct ingestion goes on the roadmap conditional on a
source appearing that is affordable and permitted. Candidates worth re-checking
later: a Nitter-style instance that is stable and permitted (none currently
are), an official lower-tier API, or a user-supplied bearer token that a fork's
owner provides for their own account. Not committed, not scheduled.

### 3.5 Tier summary as it will appear to a user

| Tier | Status | Auth | Reliability |
|---|---|---|---|
| Hacker News (Algolia) | Live | None | High — verified, fast, no quota hit |
| RSS feeds | Live | None | High — 21 feeds verified serving |
| Reddit | Opt-in, off by default | None (OAuth recommended) | Medium on a laptop if requests are spaced; unverified from CI |
| X / Twitter | Not covered | — | Indirect only, via HN/RSS echo |

---

## 4. Pipeline design

```
topics.yaml + sources.yaml
        │
        ▼
   fetchers  (hn / rss / reddit)     ── each isolated, failure ≠ run failure
        │      produce Item records
        ▼
   normalize  (canonical URL, parsed timestamp, source id)
        │
        ▼
   keyword filter  (STRICT, local, per topic)   ◄── the accuracy gate
        │
        ▼
   dedup  (canonical URL, then title similarity)
        │
        ▼
   rank  (recency · keyword · source weight · echo)
        │
        ▼
   render  →  site/index.html
```

### 4.1 The Item record

Every fetcher, regardless of tier, emits the same flat record:

```
Item(
  title, url, canonical_url, source_id, source_name,
  published_at (UTC, tz-aware), fetched_at,
  score        # native popularity if the source has one, else None
)
```

Fetchers do no filtering and no ranking. They fetch and normalize. Everything
downstream is source-agnostic, which is what makes adding a fourth tier later a
contained change.

### 4.2 Keyword filter — strict, local, explainable

A keyword matches a title when it appears as a **whole-word, case-insensitive**
match. Word boundaries are required so that `AI` does not match `malaria`,
`said`, or `chain` — the exact class of error the HN probe surfaced.

Rules:

- Multi-word keywords (`"AI agents"`) match as a phrase, with flexible internal
  whitespace.
- Keywords may be marked as an exact phrase or given an alias list in
  `topics.yaml`, so `Claude` can also catch `Anthropic Claude` without also
  catching every use of the given name.
- A topic may declare `exclude` terms. An item matching an exclude term is
  dropped from that topic even if it matched a keyword. This is the escape hatch
  for an ambiguous keyword, and it is why a keyword system beats an embedding
  system for this job: the user can see and fix a bad match.
- Matching runs against the **title only** in v1. Feed summaries vary wildly in
  quality and length, and matching against a 2,000-word content dump produces
  weak, unexplainable matches.
- An item may match several topics and will appear under each. It is fetched and
  deduped once.

Every retained item records **which keywords matched**, which drives both the
ranking signal and the on-page topic tag.

### 4.3 Ranking

A single score per item per topic, from four terms. All weights live in config,
not code.

**1. Recency** — exponential decay on age, half-life configurable, default 12
hours. Newer is better, and after roughly two days an item is effectively gone
regardless of other signals. This is the dominant term, because the product
promise is "up to date".

**2. Keyword strength** — how well the item matches this topic: how many of the
topic's keywords hit, with a title-position bonus (a keyword in the first few
words usually means the article is *about* it, not merely mentioning it).

**3. Source weight** — a per-source multiplier from `sources.yaml`, default 1.0.
This is the "premium" dial: a fork's owner raises the weight on outlets they
trust and lowers it on ones they tolerate. Shipping opinionated defaults here
would be presumptuous, so defaults are near-uniform and the README explains the
dial.

**4. Cross-source echo** — a bonus when the same story (post-dedup) was
independently surfaced by two or more distinct sources. Two outlets
independently covering something is a real signal of significance, and it is the
mechanism by which X-originated stories surface (§3.4). The bonus is capped so
that three sources is not three times as important as two, and it counts
*distinct sources*, never two items from the same feed.

Native popularity (HN points) feeds in through the points floor at fetch time
rather than as a fifth ranking term, so that a 900-point HN story does not
permanently outrank everything from sources that have no score at all.

The scoring function is pure and takes an explicit "now", so it is testable
without mocking the clock.

### 4.4 Deduplication

Two passes, cheap first:

**Pass 1 — canonical URL.** Lowercase host, drop `www.`, strip tracking
parameters (`utm_*`, `ref`, `source`, `fbclid`, `gclid`, and friends), drop the
fragment, normalize the trailing slash. Identical canonical URLs are the same
item. This catches the large majority.

**Pass 2 — title similarity.** For items that survived pass 1, compare
normalized titles (casefolded, punctuation and stopwords stripped) with a
similarity ratio above a configured threshold, default 0.85. This catches the
same story at different URLs — a syndicated piece, or an HN submission of an
article we also got from that outlet's own feed.

When items merge, we keep the one from the **higher-weighted source**, but the
merged item remembers every source that carried it, which is exactly the input
the echo signal needs. Title-similarity comparison is scoped to items within the
same time bucket to keep it from becoming quadratic across the whole corpus.

### 4.5 Rendering

One `render.py` producing one self-contained `index.html`. No build step, no
framework, no external requests at page load — the CSS is inlined and there are
no images, no fonts fetched, no analytics, no trackers. It should render before
a spinner would have appeared.

Design intent: **premium, clean, minimal list.** One column, generous
whitespace, a strong type hierarchy and almost no ornament. The visual
references are a well-set reading page, not a dashboard.

- System font stack, so there is no font request and it looks native everywhere.
- Light and dark via `prefers-color-scheme`, both defined explicitly.
- Each item is one line of substance: **headline** (the link, and the only
  strong visual weight on the row), then a quiet meta row of source · age ·
  topic tag.
- Topics as sections, with filter chips at the top that show and hide sections
  client-side. No images anywhere in v1.
- Header: the site name and a quiet "Updated <time> · refreshes hourly".
- Footer: what this is, the honest accuracy note from §1, the source-health line
  from §3.3, and a link to the repo.

The rendered site is committed to a `gh-pages` branch by the workflow, so the
published page is a plain artifact anyone can inspect.

---

## 5. Repository layout

```
news-curator/
├── README.md              what it is, how to fork it, honest source table
├── LICENSE                MIT
├── topics.yaml            EXAMPLE topics — the file a forker edits first
├── sources.yaml           feeds + weights, Reddit commented out
├── requirements.txt       pinned
├── curator/
│   ├── config.py          load + validate yaml, fail loudly on bad config
│   ├── models.py          the Item record
│   ├── fetchers/{hn,rss,reddit}.py
│   ├── filter.py          strict keyword matching
│   ├── dedup.py
│   ├── rank.py
│   ├── render.py
│   └── pipeline.py        wires it together, CLI entry point
├── tests/
├── docs/plans/            this file
└── .github/workflows/     curate.yml (hourly), ci.yml (PRs)
```

Dependencies are deliberately three: `requests`, `feedparser`, `PyYAML`, all
pinned. Everything else is standard library. A tool that rebuilds itself hourly
and unattended should have a dependency surface small enough to audit.

---

## 6. Operations

- **Schedule:** hourly cron plus `workflow_dispatch` for a manual run.
  GitHub's cron is best-effort and can be delayed under load, so the page says
  "refreshes hourly" and always shows a real timestamp rather than implying a
  precise clock.
- **Concurrency:** a concurrency group so a manual run and a cron run cannot
  publish over each other.
- **Failure policy:** an individual fetcher failing is logged, noted on the
  page, and the run continues. The run fails only if *every* tier produced
  nothing, because publishing an empty page over a good one is worse than
  leaving the previous page up.
- **Permissions:** the workflow gets the minimum token scope needed to publish,
  and no repository secrets are required for the default configuration. That is
  a deliberate property: a fork should work with zero setup.
- **CI:** on pull requests, run the tests and a render smoke test against fixed
  fixtures, with no network access, so CI is deterministic.

---

## 7. Success criteria for v1

1. A fresh clone with the example topics produces a rendered page containing
   real, current, correctly-linked items.
2. Zero items on the page fail the strict keyword filter (no malaria stories).
3. The page renders correctly with an empty topic list, and with a source that
   returned nothing.
4. A blocked Reddit tier degrades the page visibly and does not fail the run.
5. Someone who has never seen the repo can change the topics and get their own
   page, guided only by the README.

---

## 8. Adversarial review — Codex

**Round 1** (`codex exec`, read-only, against this spec plus the full `curator/`
implementation): **16 must-fix and 8 should-fix findings. All applied.**

The review was worth more than the spec was. Four findings were reproduced
locally before fixing, and every one was a genuine defect that would have
shipped:

| Reproduced defect | Observed | Now |
|---|---|---|
| `clean_title("2 &lt; 3 &gt; 1")` | returned `"2 1"` | returns `"2 < 3 > 1"` |
| `canonical_url("javascript:alert(1)")` | returned it unchanged, then rendered it as an `href` | returns `None`; scheme allow-listed |
| `title_similarity("…iOS 18.6.1", "…iOS 18.6.2")` | `0.96`, so two releases merged into one row | differing numbers veto a merge |
| `keywords: AI` in `topics.yaml` | silently parsed to `["A", "I"]` | raises `ConfigError` naming the fix |

The findings that changed the DESIGN rather than the code, which are the ones
worth remembering:

1. **The accuracy promise in §1 was unkeepable as written.** "Published under
   that title at that link" cannot be asserted by a tool that never fetches the
   destination, and it is flatly false for Hacker News and Reddit, where the
   headline is written by a submitter, not the publisher. The promise is now
   narrowed to what the code can actually prove (the source handed us this pair
   at build time), aggregator rows are labeled `via`, and on a URL collision the
   publisher's own headline wins over a submitter's paraphrase.
2. **Fuzzy title merges were inventing provenance.** A `SequenceMatcher` guess
   was feeding the "N sources" badge, so two similar headlines produced a
   confident claim of independent corroboration. Echo provenance now comes only
   from URL-identical merges. Hacker News via API and via RSS also collapse to
   one platform rather than counting as two.
3. **"HTTP 200 is not permission."** The spec called feeds
   "publisher-sanctioned" on the strength of a status code. Three reachable
   feeds with explicit RSS reuse terms were removed from the defaults (§3.2),
   and the distinction is now stated wherever the feed list appears.
4. **The empty-page guard was in the wrong place.** It ran on items FETCHED, so
   one irrelevant successful fetch could overwrite a good published page with an
   empty one. It now runs on rows that will actually be VISIBLE, after
   filtering, and the page is written atomically.
5. **Partial failures were hidden behind healthy-looking counts.** A tier that
   returned ten items and then got rate-limited rendered as `reddit: 10`. Count
   and degradation state are now reported independently.
6. **"Refreshes hourly" was a promise GitHub does not keep.** Cron is
   best-effort and scheduled workflows are disabled after 60 days of repository
   inactivity. The page says "scheduled hourly", shows its real build time, and
   warns when that is over three hours old.

Two findings were about the repo rather than the design and are also fixed: the
upstream owner was hardcoded into the footer and User-Agent (a fork would have
advertised the original), and there was no reproducible probe receipt behind the
feed table (`scripts/probe_sources.py` now regenerates it on demand).

**Round 2** re-ran against the applied fixes and asked for line-level evidence
on each one: **11 of 15 VERIFIED FIXED, 4 PARTIALLY FIXED, plus 6 new
findings.** All were addressed.

The four partial fixes are the interesting ones, because each was a case of
fixing the reported instance without closing the class:

| Partial | What was still open | Closed by |
|---|---|---|
| Unsafe URLs | `canonical_url` rejected them, but the RENDERER trusted `Item.url` and would happily emit a hand-built `javascript:` href. URL credentials were also accepted. | Revalidate at the output boundary; reject userinfo (`https://github.com@evil.example`). |
| Empty-page guard | Ran after filtering, but was conditioned on `cfg.topics`, so `topics: []` walked straight past it. | Guard on visible rows unconditionally. |
| Malformed feeds | Oversized feeds were rejected, but a malformed document that yielded any entry was still reported healthy. | `bozo` surfaces as "malformed but salvaged" in the health line and marks the tier degraded. |
| Estimated timestamps | `time_is_estimated` was tracked internally and then displayed as a bare "3h ago", claiming a publication time we never had. | Those rows render "updated 3h ago". |

New findings worth recording:

1. **The staleness indicator could never fire.** It compared build time against
   `now`, which are the same value at render, so it was always zero. Staleness
   is a property of *when you look*, so it moved into the reader's browser: the
   build timestamp ships as `data-built` and the age is computed on view.
2. **A request cap does not bound time.** 60 Hacker News requests at a
   15-second timeout is 15 minutes, which was the entire CI job budget. Added a
   wall-clock budget (120 s default) alongside the request cap, and raised the
   job timeout to 20 minutes.
3. **Fuzzy dedup still merged opposite stories.** "Apple releases iOS 18.6.1"
   and "Apple **delays** iOS 18.6.1" score 0.875 with identical numbers, so the
   numeric guard could not help. The threshold moved to 0.90. Character
   similarity cannot tell "releases" from "delays", so the threshold has to sit
   above where a single verb swap lands. The cost is asymmetric: a missed merge
   shows one extra row, a wrong merge silently deletes a story.
4. **Only three config values were validated.** Ranking, dedup, Hacker News and
   Reddit numbers were cast at point of use, turning a YAML typo into either a
   mid-run crash or a silent coercion. All are now checked at load.
5. **The custom-domain instructions did not work.** The README said to add a
   `CNAME`, but nothing copied it into the published output, so a custom domain
   would reset on every deploy. The build now copies it.
6. **Mutable action tags with write permissions.** Accepted risk, deliberately:
   these are first-party `actions/*` and `@vN` is GitHub's own documented usage.
   Recorded here so it is a decision rather than an oversight.

The test suite (**140 tests, no network**) pins every reproduced defect from
both rounds as a regression test, so a later change cannot silently reintroduce
one.

### Verification at the time of writing

- 121 tests pass, no network required.
- One live end-to-end run: Hacker News returned 331 items, RSS 1,907 items
  across 18 feeds, 2,238 collected, 881 inside the 48-hour window, 701 unique
  after dedup, 60 rows rendered across the two example topics.
- The rendered page was opened and inspected in light and dark mode. Filter
  chips work, publisher and aggregator attribution render distinctly, six rows
  carried a multi-source badge, and no unsafe scheme appears in the output.

---

# v1.1 — The content layer (2026-08-28)

v1 shipped a working machine with example content in it. `topics.yaml` said
"EXAMPLE TOPIC 1 (replace me)" and the feed list was eighteen general technology
publications. v1.1 replaces the content and adds the two things a real reader
needs from it. The pipeline design from v1 is unchanged.

## The ruling this implements

Six sections, each with a strong keyword set **and** a curated list of premium
sources: AI (TechCrunch is a required source), Crypto, Quantum computing, Energy
including small modular reactors, Space technology, Biotechnology. Plus a
picture per story, and a way for a manager to add a keyword without a developer.

## What changed, and the one design decision worth arguing about

### Categories became first-class, and own their feeds

A category is now `keywords` + `sources` in one place, because those answer the
same question: what belongs in this section.

The decision worth writing down is that **a feed listed under a category joins
that category without needing a keyword hit.** Keyword-only matching has a
failure that no keyword list can fix: "Vogtle 4 enters commercial operation" is
unmistakably an energy story and contains not one energy keyword. Publications
like World Nuclear News exist to supply exactly those stories, and gating them
behind a string match throws away the reason to curate a source at all.

So listing a feed under a category is an editorial claim that the publication is
SINGLE-SUBJECT. That is why multi-subject publications stay in the shared pool in
`sources.yaml` and are matched by keyword: TechCrunch's AI section feed is native
to AI, while its front page is shared, so a TechCrunch crypto story still lands
in Crypto rather than dragging the front page into AI wholesale.

Three guards keep the claim honest:

- `exclude` still vetoes a native item. A strong claim, not an unconditional one.
- A native item with no keyword hit scores `native_source_score` (0.5) rather
  than a full keyword match, so a headline that says what it is about still wins.
- Category membership only survives a URL-IDENTICAL dedup merge, never a fuzzy
  title merge, on the same reasoning that already governs the "N sources" badge:
  filing a story under a section on the strength of a guess is exactly the kind
  of confident wrongness this codebase is built to avoid.

`topics:` from v1 still loads, so a fork does not break. It produces categories
with no native feeds, which is what v1 meant.

### Preview images, from the publisher, hotlinked

Two sources, cheapest first:

1. **The feed.** `media:content`, `media:thumbnail`, image enclosures. Zero extra
   requests, and it covers roughly half the shipped feeds. It is also the ONLY
   way to get an image from CoinDesk, The Block and the Industry Dive properties,
   which serve their feed happily and return 403 to a direct article fetch.
2. **`og:image` on the article.** Only for what the feed left bare, and only for
   rows that survived ranking and truncation. The response is read as far as the
   end of the head and then dropped; the body is never parsed.

The ceiling is therefore the number of VISIBLE rows, not the number of headlines
collected, which is what keeps an hourly job bounded. Answers are cached in
`image_cache.json`, committed to the repo and keyed by canonical URL: the answer
for a given article does not change, and a committed file is durable and
auditable in a way a CI cache is not.

Nothing is downloaded, resized or re-hosted. The publisher keeps their CDN and
the ability to change or withdraw the image.

**This changed a promise, and the promise had to change with it.** v1's footer
said destination pages are never fetched. That is no longer true, so the footer
now says what is: the head of an article is read for the image tag the publisher
put there, no article text is fetched, stored or summarized, and no claim in any
linked article has been checked.

Two failure modes were found by measurement rather than reasoning, and both are
now encoded:

- **A 403 or 429 page can carry its own `og:image`.** Several publishers return
  a styled block page with a social preview. Parsing it would attach the
  publisher's "you are blocked" artwork to a real story, so a non-200 response is
  never parsed. A 64 KB probe that ignored status codes looked like it worked.
- **Some heads are bigger than they look.** Springer article pages reach
  `</head>` at about 100 KB, so a 64 KB cap silently missed every one. The cap is
  256 KB and is a config dial.

Errors and clean misses are cached differently, because they are different facts:
"this page declares no image" is permanent, while "we were refused at 14:00" is
about one moment and is retried after 24 hours.

### Adding a topic without a developer (v1, zero backend)

The page footer links to GitHub's web editor for `topics.yaml`. That is the whole
feature, and its smallness is the point: GitHub already owns the identity, the
permission check, the edit box, the diff and the audit trail. Whoever can push to
the repo edits a keyword in the browser and saves; whoever cannot gets GitHub's
own fork-and-pull-request flow. There is no form to secure, no account system to
run, and no new place for a secret to leak.

Saving `topics.yaml` is a push to a path the Curate workflow already watches, so
the page rebuilds immediately rather than waiting for the hour.

The link is built from the repo URL and only rendered for `github.com` hosts,
because `/edit/<branch>/<file>` is GitHub's route and would be a broken link
anywhere else. A fork on another host gets instructions instead of a wrong link.

**A v2 exists and was deliberately not built.** An issue template ("Add a
keyword") plus an Action that parses the issue body, edits the YAML, opens a PR
and closes the issue would let someone with no write access propose a keyword,
and would give every change a conversation thread. It is worth building when
there is a second person asking for keywords. Today there is one manager who has
push access, and for her the editor link is strictly fewer steps.

## Sources: what shipped, and what did not

Every feed was probed live on 2026-08-28. Reachability is not permission to
republish, and only the first is testable by a script, so exclusions are recorded
with their actual reason rather than a blanket policy sentence:

| Excluded | Reason |
|---|---|
| BBC, FT, NYT | Published terms govern RSS reuse specifically. Carried over from v1. |
| Endpoints News | Feed returns HTTP 403 to an ordinary client. The terms question never arose. |
| STAT News | Reachable, but its terms of service could not be retrieved to check, and most feed items are STAT+, so a link lands the reader on a paywall. |
| Nature, Science | Terms pages sit behind an authentication redirect. Both are general-science rather than biotech, so the fit was weak anyway. |
| arXiv cs.AI / cs.LG | Reachable and parsing (312 entries on probe day). Excluded on EDITORIAL grounds: a preprint firehose is not news, and several hundred same-day papers would crowd out a recency-ranked AI section. Left commented in `sources.yaml`. |
| Fierce Biotech, Fierce Pharma | The interesting one. Both return 200, parse cleanly, and carry 25 entries. Every entry is then dropped, because `<pubDate>` is `Aug 28, 2026 10:30am` rather than RFC 822, with no timezone. We drop undated items rather than stamping them "now", and guessing a timezone would silently misorder a recency-ranked section by hours. |
| IEEE Spectrum quantum tag | Does not exist. IEEE publishes topic feeds for `computing` and `energy` but no quantum one, so the computing feed went to the shared pool where keywords decide, and the energy feed is native to Energy. |
| Quanta Magazine | Not excluded, but NOT native to Quantum despite the name. It is a general science and mathematics publication, so it sits in the shared pool. |

The Fierce case produced a code change worth keeping: a feed that answers 200,
parses, and yields nothing usable is now reported as degraded. It previously
looked identical to a slow news day, which is the quietest way for a source to
die.

## Verification

Live end-to-end run on 2026-08-28, cold cache:

| Category | Rows | From its own curated feeds |
|---|---|---|
| AI | 30 | 5 |
| Crypto | 30 | 28 |
| Quantum computing | 30 | 30 |
| Energy and nuclear | 30 | 26 |
| Space technology | 30 | 25 |
| Biotechnology | 30 | 27 |

2,875 items collected across 58 feeds (19 shared, 39 curated) plus Hacker News,
766 inside the 48-hour window, 715 unique after dedup, 180 rendered. Every
category hit the `max_items_per_topic` cap of 30, so none of the six is thin.

Preview images: **157 of 180 rows (87%)**, of which 86 came free from feeds and
71 from an `og:image` lookup; 3 pages declare no image and 19 refused the fetch.
The refusals are concentrated in publishers that serve a feed happily and return
403 to a direct article fetch, which is exactly why the feed is read first. A
second run resolved everything from cache and fetched nothing, which is the
steady state the cache exists to produce. Cold run 40 s, warm run 25 s.

Source probe: **58/58 reachable**, via `python3 scripts/probe_sources.py`, which
now covers both files rather than only the shared pool.

The low AI native count (5 of 30) is expected rather than a defect: AI is the one
category the shared pool already covers heavily, so general-technology
publications win most of those slots on recency and keyword strength.

Tests: **262, and the suite now ENFORCES "no network"** with an autouse socket blocker rather than asserting it in a README line. The new ones cover category parsing, the two ways to
belong to a category, native ranking, the `og:image` parser against offline
markup fixtures, feed-declared image extraction, cache hit/miss/retry/prune
behaviour, and the rendered image and add-topic output.

Two defects were found by the new tests before shipping, both now regressions:

1. The head parser set a "stop at body" flag and never checked it, so an
   `og:image` in the article body would have been read as if the publisher had
   declared it in the head.
2. `--offline` did not disable image fetching, so the offline render smoke test
   would have gone to the network.

### Security note: new attack surface, and what is and is not defended

v1 never fetched a destination page, so the `og:image` lookup is genuinely new
attack surface and is worth stating plainly rather than leaving implied. A feed
we do not control now supplies addresses that a CI runner will request.

Defended:

- **Scheme.** Every image URL goes through the same `safe_url` allow-list as
  every other link, at parse time and again at the render boundary. A
  `javascript:` or `data:` value in an `og:image` tag is dropped twice.
- **Non-public destinations.** `images.is_public_host` refuses loopback, private
  ranges, link-local and the cloud metadata endpoint at `169.254.169.254`,
  checked on the initial URL and AGAIN on the post-redirect URL. The second
  check is the load-bearing one: blocking the request matters less than making
  sure an internal page's meta tag can never be parsed onto a public page.
- **Non-200 responses are never parsed.** Measured, not assumed: several
  publishers return a styled block page carrying its own `og:image`, so a
  parser that ignored status codes would have attached "you are blocked"
  artwork to real stories.
- **Resource use.** 256 KB per page, a 10 s timeout, 8 workers, 120 fetches and
  60 seconds per run, all config dials.

Not defended, deliberately: a hostname that RESOLVES to a private address still
passes, because catching it needs DNS resolution per redirect hop. The residual
is blind (nothing is returned to the page), inside an ephemeral container, on a
public repository with no secrets beyond the job's own token. It is recorded
here as a decision rather than left as an oversight.

### Adversarial review round (Codex, cross-model)

A cross-model adversarial pass returned FIX-FIRST with nine findings. All were
applied. The four that changed real behaviour:

1. **CRITICAL, an unreviewed path to `main`.** `workflow_dispatch` lets a human
   pick any branch, and the cache step's `git push origin HEAD:main` would have
   promoted that whole branch to `main` as an unreviewed merge. The step is now
   gated on `github.ref == 'refs/heads/main'`. Found only because the reviewer
   read the trigger list and the push together; each looks fine alone.
2. **HIGH, the SSRF gate was decorative.** The original check compared the
   hostname against private IP LITERALS, which `evil.example` pointing at
   `169.254.169.254` walks straight past, and `requests` chased redirects before
   any re-check could run. Now the name is RESOLVED and every returned address
   must be global, redirects are followed manually one hop at a time with a
   re-check before each request, and a name that will not resolve is refused.
   The difference is refusing to REQUEST an internal address rather than merely
   refusing to parse what it sent back.
3. **HIGH, fuzzy dedup could empty a section.** Two similar headlines from two
   DIFFERENT categories' curated feeds would merge, and since category
   membership deliberately does not survive a fuzzy merge, the loser's story
   silently vanished from its section. Such a merge is now refused outright.
   This is the existing asymmetry applied one level up: a missed merge shows one
   extra row, a wrong merge deletes a story.
4. **MEDIUM, the ranking claim was off by a tie.** `native_source_score` was
   0.5, and a single keyword hit with no lead bonus scores exactly 0.5, so the
   documented "a native item ranks below a real keyword hit" was a tie broken by
   source weight. The default is now 0.4. The original test missed it by only
   testing a leading keyword.

Also applied: a total-transfer deadline per feed (the `requests` timeout is per
READ, so a server dripping bytes could hold a worker indefinitely); a byte cap
that no longer overshoots by one chunk; non-definitive misses (truncated reads,
refused redirects) cached with the SHORT retry TTL instead of being recorded as
"this page declares no image"; and cache values revalidated on READ, since the
file is committed and therefore hand-editable.

Four claims were corrected rather than defended: the README said "four signals"
and listed five; "49 feeds" when there are 58; "only once ever per link" when
errors retry after 24 hours and pruning can cause a refetch; and "only the head
is read" when the final chunk can overlap the start of the body. The last one is
the kind of promise that quietly becomes false, so it now says "as far as the
end of the head" and explains why.

The reviewer also noted that several tests asserted counters rather than
behaviour: `enrich` tests claiming "never fetched" would have passed against an
implementation that fetched and discarded. Those now monkeypatch the transport
and fail loudly if it is reached. Test count went 231 -> 258, with new coverage
for redirect SSRF, DNS-based private-host bypass, byte-cap enforcement,
definitive-versus-truncated outcomes, fuzzy-merge category loss, and a feed that
returns 200 and yields nothing usable.

### Second review round (fresh-context Claude adversary)

A second reviewer, with no context from the build, went further than the first
and caught four things the Codex round missed, two of which were live bugs in
already-pushed code.

1. **The fuzzy-dedup fix from round one was incomplete, and the reviewer proved
   it with a repro.** Round one refused to merge two rows whose curated
   categories DISAGREED. That misses the common case: a curated energy row and
   a higher-weight GENERAL row (no categories at all) still merged, the general
   copy won on weight, and the story matched no keyword, so it vanished from the
   page entirely. The headline it vanished was the flagship example this whole
   design is justified by, "Vogtle 4 enters commercial operation".

   The fix is the opposite of round one's: `native_categories` is now unioned on
   BOTH dedup passes. The reviewer's argument is decisive, and it is that the
   code was already inconsistent: a fuzzy merge already inherits the image, the
   score and the publish time from the losing row, so it already trusts the
   same-story assertion. Withholding only the category did not mean "we declined
   to guess", it meant the row silently disappeared. The echo badge stays gated
   on certainty, because it makes a public numeric claim; a section assignment
   does not.

2. **The SSRF fix from round one had a hole, also with a live repro.**
   Resolving the hostname closed `evil.example -> 169.254.169.254`, but every
   non-dotted-quad way of writing an address (`2130706433`, `127.1`, `0x7f.1`)
   fails `ipaddress.ip_address` and was treated as a name. Worse,
   `0177.0.0.1` resolves via `getaddrinfo` to `177.0.0.1`, which IS global and
   so PASSED, while a client applying octal rules connects to `127.0.0.1`. Two
   parsers disagreeing is a bypass, not a residual. A hostname whose last label
   is all-digits or `0x`-prefixed is now refused outright, since no real domain
   ends that way.

3. **`budget_seconds` bounded nothing, measured at 20x over.** `as_completed`
   had no timeout, so if every request stalled the loop never reached the
   budget check; and exiting the `with ThreadPoolExecutor` block called
   `shutdown(wait=True)`, which joined every stalled worker anyway. The budget
   is now armed on the wait itself and the pool is shut down with `wait=False,
   cancel_futures=True`. Measured before and after on eight stalling hosts with
   `budget_seconds=1`: **20.0s -> 1.0s**. `sources.yaml` claimed this bound was
   real; now it is.

4. **A shipped config bug.** `Phys.org Quantum` was listed as a curated feed
   under "Quantum computing". Listing a feed there asserts it is single-subject
   for that section, and a quantum-PHYSICS feed publishes entanglement and
   optics work with no computing angle, which bypassed keyword matching
   entirely. It moved to the shared pool where the quantum keywords decide.
   `topics.yaml` warns against bare "quantum" as a keyword for this exact
   reason; the same care had to apply to feeds.

Also fixed: a prune fallback that made any row with an unparseable `seen_at`
immortal (`cutoff < cutoff` is False), so the file could grow without bound
after one hand edit; `safe_url` validated a control-stripped string and returned
the raw one, which only stayed safe by two parsers coincidentally agreeing;
`retain_days: 0` silently becoming 45; `hackernews.budget_seconds` missing from
the load-time validation list; and cap/budget exhaustion being invisible, which
the Hacker News tier already surfaces and this one did not.

Two more false claims went with them. The footer described a "hotlinked" picture
on a page that displays none and makes no third-party request, and promised that
saving `topics.yaml` "rebuilds this page" to an audience whose edits become a
pull request on their own fork and rebuild nothing. Both now say what happens.

**The reviewer's sharpest structural point** was that `tests/test_images.py`
opened by claiming "every fixture below is markup or a fake transport" when the
file contained no transport at all, so four defences documented as "learned by
measurement, encoded below" had no regression test. Those exist now, and the
suite no longer takes "no network" on trust: an autouse fixture in
`conftest.py` blocks `connect`, `create_connection` and `getaddrinfo`, and tests
that genuinely resolve a host opt in with `@pytest.mark.allow_socket`. It caught
a test doing real DNS on its first run.

Round one: 231 -> 258 tests. Round two: 258 -> 262, plus the blocker.
