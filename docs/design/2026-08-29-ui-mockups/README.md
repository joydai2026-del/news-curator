# News Curator redesign, three UI directions (2026-08-29)

Three self-contained HTML mockups for the news.joydong.org redesign. Open any file directly in a
browser (double click, or `open <file>`). Nothing is deployed and no pipeline file was touched.

| File | Direction |
|---|---|
| `a-pressreader.html` | A. PressReader faithful |
| `b-broadsheet.html` | B. Broadsheet |
| `c-evolved-minimal.html` | C. Evolved minimal |

## A. PressReader faithful

The closest reading of the PressReader "For You" page. A sticky left sidebar holds a region and
language selector, an icon nav of every section, and the saved searches block. The main column is a
run of category sections, each with an icon, a large heading, and its own colored rule, over a
three column grid that mixes one large image card, small square thumbnail cards, and text only
cards so the page has visual rhythm. Every card carries the publication name, headline, snippet,
clickable topic tags separated by middle dots, a relative time, and a kebab menu (open, copy link,
hide). Each section ends with a right aligned "More {Category}" that expands it, and sections fill
in as you scroll rather than all at once. This optimizes for browsing breadth: the most scannable of
the three, and the one that looks least like the current site. The region selector is real and does
something: it narrows the general news sources to United States, United Kingdom, or Greater China.

## B. Broadsheet

A digital front page. A centered masthead carries the name, the build time, and the language toggle,
with a sticky category bar under it and a clippings bar for saved searches. Each section opens with a
full width colored band, then one lead story with a large image and a big serif headline beside a
numbered list of the next five, then a dense multi column run of briefs. Typography does the work:
Playfair Display headlines, Source Serif body, hairline and double rules, no rounded corners
anywhere. This optimizes for editorial authority and information density. It fits the most stories
per screen and reads like a newspaper, which is also its risk: it is the furthest from the current
site and the most sensitive to short or missing summaries.

## C. Evolved minimal

The current site's Apple style card aesthetic kept intact, with the missing structure added. A left
rail lists every section with a colored dot and a live count, and clicking one filters the page to
that section instead of scrolling. Sticky, blurred section headers keep your place, cards are image
forward with soft shadows and large radii, and the language toggle sits in the top bar as a
segmented control. Dark mode is true black. This optimizes for the lowest risk path: it is a
structural upgrade rather than a redesign, so most of the existing CSS thinking survives, and it is
the fastest of the three to turn into production code.

## Shared assumptions

- **Data snapshot**: every story comes from `mockup-data.json`, fetched live on
  **2026-08-29 14:06 UTC**. Nothing is invented. The pages are static: they read an embedded copy of
  that snapshot, so the timestamps drift the longer you leave the file unopened.
- **Sections**: general news was added as asked. English mode shows Trending, World (CNN, BBC,
  Guardian), US News (Fox, CBS), Business (CNBC), then the six tech topics from the current site
  (AI, Crypto, Quantum computing, Energy and nuclear, Space technology, Biotechnology), then
  Newsletters. Chinese mode swaps to the Chinese story set: Trending, 国际 (RFI, 中央社), 科技
  (cnBeta, Solidot), 电子报.
- **Language toggle**: EN / 中文 changes the interface labels, the section list, the story set, and
  the relative times ("2h ago" becomes "2小时前"). The choice is remembered per browser. Trending is
  bilingual by nature: Buzzing carries a Chinese title and the English original, so whichever
  language you are in leads and the other sits under it as a second line.
- **Trending**: Hacker News plus Buzzing only. No X or Twitter anywhere.
- **Newsletters**: the section is present in all three and renders as a typographic gradient panel.
  There were **no newsletter stories in this snapshot**, so what you see is the real empty state, not
  a filled section. The rule it states is the design rule: newsletter cards never load an image,
  because a remote image would tell the sender the mail was opened.
- **Saved searches**: fully working and demo grade. Type in the search box, press Save, and the
  query becomes a chip in the sidebar. Chips re-run the search when clicked and are removed with the
  ×. Everything is `localStorage`, per browser, and each mockup uses its own key so the three do not
  share state. Clicking a topic tag on any card also runs that search.
- **Images**: hotlinked from the publishers with lazy loading. Some publishers block hotlinking, so
  those cards fall back to a typographic gradient panel tinted with the category color. That
  fallback is deliberate and you will see it in the mockups, most often in Space technology. Some
  feeds hand over small thumbnails (BBC serves 240 pixels wide), so in direction A a lead card whose
  picture is too small keeps its wide slot but shows the picture at its own size beside the headline
  rather than stretching it.
- **Safety carried into the mockups**: story text is embedded with every angle bracket escaped, so a
  future headline containing markup cannot break out of the data block, and only `https:` links and
  images are ever rendered. Anything else shows as plain unlinked text with the gradient panel.
- **Theme**: light and dark both ship, following the operating system setting. There is no manual
  theme switch, same as the current site.
- **Accuracy wording**: nothing is labeled verified, live, or fact checked. Aggregator headlines say
  "via {source}", and the footer states that the content belongs to the linked sources.
- **Demo only in all three**: story data does not refresh, the kebab menu's "hide this story" lasts
  until reload, "More {Category}" expands from the snapshot rather than paging a server, and the
  region selector in direction A filters the sources already in the snapshot.
