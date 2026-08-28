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

**Round 2** was run as a verification pass over the applied fixes. Its result is
recorded in `docs/decisions/` if it produced further changes; the test suite
(121 tests, no network) pins every reproduced defect above as a regression test,
so a later change cannot silently reintroduce one.

### Verification at the time of writing

- 121 tests pass, no network required.
- One live end-to-end run: Hacker News returned 331 items, RSS 1,907 items
  across 18 feeds, 2,238 collected, 881 inside the 48-hour window, 701 unique
  after dedup, 60 rows rendered across the two example topics.
- The rendered page was opened and inspected in light and dark mode. Filter
  chips work, publisher and aggregator attribution render distinctly, six rows
  carried a multi-source badge, and no unsafe scheme appears in the output.
