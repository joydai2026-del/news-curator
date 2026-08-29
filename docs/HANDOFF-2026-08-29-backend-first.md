# HANDOFF: news-curator v3, backend first (2026-08-29)

For: Codex (or any agent picking this up). From: claude-code-m4, UI/UX exploration session 2026-08-29.
JJ's directive, verbatim intent: "if designing is not as hard, we should focus on the backend first to make sure we cover all sorts of sources for the news and do the design last."

## State right now

- Live site https://news.joydong.org is UNTOUCHED. All session work is on branch `feat/ui-v3-mockups` (mockups + research only, no pipeline changes).
- 7 design mockups with real data + a review page with per-design keep/modify/delete buttons: `docs/design/2026-08-29-ui-mockups/review.html` (open locally; JJ gave verdicts in chat, see "Design taste" below).
- Research (read both before building): `docs/research/2026-08-29-freshness-trending-retention-research.md` (live-tested endpoints, Grade A/B/C marked) and `docs/research/2026-08-29-repo-map-for-redesign.md` (current architecture + locked constraints).
- Constraints that must not regress: `docs/plans/2026-08-25-news-curator-v2-design.md` + vault `agents/shared/decisions/2026-08-28-news-curator-v2-ship-decisions.md` (privacy gating on the newsletter lane, allowlist adapters, accuracy stance, hourly Actions cadence, GitHub Pages deploy). JJ re-opened LAYOUT and CATEGORY SET this session; she did not re-open privacy or the newsletter lane.

## JJ's locked decisions (2026-08-29, chat + AskUserQuestion)

1. ADD general news categories (World, US News, Business) alongside the six tech topics.
2. EN/中文 language toggle; Chinese mode = Chinese-language sources + Chinese UI labels.
3. Freshness target ~30-60 min is fine: KEEP hourly Actions (no Cloudflare Worker), but fix the dead sources and add a staleness alarm.
4. X/Twitter is DROPPED (no free/legal path in 2026; Nitter killed by C&D 2026-08-24). Trending = Hacker News + buzzing.cc + Reddit if the OAuth app materializes.
5. Google SSO wanted so shared friends get their own saved searches (recommended: Supabase auth + one table; site stays static). Phase after backend.
6. PHASE 2 (locked WHAT, do not shrink): Chinese-specific news sources (politics especially) translated INTO English; the site bridges English and Chinese news worlds in both directions.
7. THIS handoff's order: backend/source coverage FIRST, design implementation LAST.

## Backend work, priority order

### P1: source coverage + freshness (the core of this handoff)
- **Remove `rss.cnn.com` everywhere** (frozen since May 2023, still returns HTTP 200). Replace: CNN news sitemap `https://www.cnn.com/sitemap/news.xml` (~30 min fresh, has titles/timestamps), Fox `https://www.foxnews.com/sitemap.xml?type=news` (~24 min). Needs a news-sitemap parser (new source type next to RSS).
- Add verified-fresh general feeds: BBC `feeds.bbci.co.uk/news/world/rss.xml`, Guardian `theguardian.com/world/rss`, CNBC `cnbc.com/id/100003114/device/rss/rss.html`, CBS `cbsnews.com/latest/rss/main`, Yahoo `news.yahoo.com/rss/`. Map into new World / US News / Business categories (`topics.yaml` + `sources.yaml`; category = topics.yaml entry, that mechanism is locked).
- **Staleness assertion**: fail/warn visibly in the Actions summary when a feed's newest item is older than N hours (this is the exact failure class that let CNN rot for 3 years). Per-feed, configurable N.
- **Trending lane**: HN via Algolia `hn.algolia.com/api/v1/search?tags=front_page` (30 stories, 1 request, keyless; Firebase 400s on bursts) + `buzzing.cc/feed.xml` (744 items, ~16 min fresh, HN+Reddit merged, bilingual titles). Echo-merge by URL already exists in the pipeline; reuse it.
- **Chinese lane** (verified live 2026-08-29): cnBeta `cnbeta.com.tw/backend.php`, Solidot `solidot.org/index.rss`, RFI中文 `rfi.fr/cn/rss`, CNA `feeds.feedburner.com/rsscna/intworld`, UDN `udn.com/rssfeed/news/2/6638?ch=news` (worked in research, returned 0 items in one later fetch, treat as flaky), DW中文 `rss.dw.com/rdf/rss-chi-all` (RSS 1.0: dates are per-item `dc:date`, NOT `pubDate`; naive parsing reads feed-generation time and the feed mixes in year-old items, filter by date). Zaobao and 36kr have NO working RSS; cover via Google News RSS `site:` queries (`news.google.com/rss/search?q=site:36kr.com+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans`). Items need a `language` field (does not exist yet; grep confirmed zero i18n in the codebase).
- **Reddit (optional)**: anonymous JSON is 403 (datacenter IPs blocked hard); RSS is ~1 req/15s and carries no scores. The path is a script-type OAuth app on JJ's account (client_credentials, ~100 QPM free non-commercial). JJ was registering `jj-news-curator` at reddit.com/prefs/apps but reCAPTCHA kept failing in her automation-attached Chrome; she should finish it in a clean browser (values: script type, about url https://news.joydong.org, redirect http://localhost:8080). Secrets then go in a scoped Actions environment like the newsletter lane's. If she does not, ship without Reddit; buzzing.cc already carries Reddit signal.
- **Data model additions**: `language` per source/item; day bucketing (Today/Yesterday) for the frontend; keep the 48h window.
- **Retention**: nothing urgent (articles are never stored; image cache prunes at 45d). Git-history growth from hourly cache commits is the only long-run issue; the researched fix (orphan data branch with amend, or R2) is documented in the research file §3, fine to defer.

### P2: Google SSO personalization
Supabase (JJ has Pro; serverless-first rule): Google provider auth + a saved-searches/interests table, site stays static GitHub Pages, JS talks to Supabase. Friends each get their own saved state. Roughly a day incl. reviews.

### P3: Phase 2 bridge (design before building)
Chinese politics/news sources translated into English (and EN→ZH completing the bridge). Needs a translation step in Actions: bring JJ options with costs first (this is new spend). Do not start without her sign-off on the translation approach.

### P4 (LAST): design implementation
Rewrite `curator/render.py` output to the winning mockup direction. JJ's taste, learned over 4 iteration rounds, in order of arrival: text-more (digg-style ranked top-20 per category) → bounded bento grid, NO endless scroll ("+N more" expands in place, Today|Yesterday switch, grid/list toggle) → muted pastel category tints ONLY (saturated bands read as "AI-sloppy" to her) → left sidebar with sections + dosed images (about one real photo per category block, NEVER under a colored gradient) → liquid-glass chrome (frosted top bar/sidebar/controls; cells stay crisp). B5 (`b5-glass-bento.html`) is the fullest expression; B4 is the no-images fallback. All mockups are self-contained and carry the hardening to port: JSON angle-bracket escaping into the data block, https-only URL allowlist, newsletter cells never get images, no "verified/live" labels. Mobile: swipe-between-categories scroll-snap panels. Keep light+dark, `<html lang>`, CJK font stacks.

## Gotchas from this session worth keeping
- Headless Chrome on this Mac clamps the layout viewport to 500px: a true 375px screenshot needs the page inside a 375px iframe wrapper.
- The mockup data field is `image` (not `image_url`); many top feeds (CNBC, CBS, HN, cnBeta, Solidot, CNA) ship no images at all: any image-led design needs a "highest-ranked story WITH a photo" rule or it renders empty.
- Codex review round 1 flagged (and mockups fixed): script-breakout via `</script>` in embedded JSON; scheme allowlist for hrefs/src. Port both into the real renderer.
- particle.news and PressReader /foryou are the two references JJ likes; particle is "too colorful" for her.

## Process expectations (house rules)
Work on a branch, never main. Adversarial cross-model review before JJ sees anything substantive. Nothing deploys to the live site without JJ's explicit go. The privacy build assertion in `curate.yml` must keep passing; if the card markup changes, update the assertion in lockstep.
