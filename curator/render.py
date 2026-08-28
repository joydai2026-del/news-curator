"""One static page. No framework, no build step, no runtime requests.

Design intent: premium, clean, minimal list. One column, generous whitespace, a
strong type hierarchy, almost no ornament. The reference is a well-set reading
page, not a dashboard.

Everything is inlined. No images, no web fonts, no analytics, no third-party
requests of any kind, so the page renders before a spinner would have appeared.

Three corrections from review are load-bearing here:

  * **Health is count AND state, separately.** A tier that returned ten items
    and then got rate-limited used to render as a reassuring "reddit: 10". It
    now renders as "Reddit: 10 items, degraded (rate-limited after 2/5)".
  * **"Scheduled hourly", not "refreshes hourly".** GitHub delays and drops
    scheduled runs under load, and disables them entirely after 60 days of
    repository inactivity. The page states the schedule and shows how old the
    build actually is, and says so out loud when that is more than three hours.
  * **The accuracy note is narrowed to what the code can actually prove.** We
    can promise the source handed us this headline and this address at build
    time, and that aggregator headlines are labeled as such. We cannot promise
    the link is still live or still carries that title.

v1.1 adds a preview image per row, carried as a `data-image` attribute rather
than an `<img>`. The layout is a separate decision and is deliberately untouched
here, so this ships the DATA a future layout needs without pre-empting it. That
also means the page still makes NO third-party requests: the address is present,
nothing loads it, and the footer says so rather than describing a hotlinking
policy for a picture nobody can see yet. The
attribute holds the publisher's own image address, hotlinked, and is absent when
the publisher declared none, which is why the renderer never invents a
placeholder: an empty attribute would look like an answer.

That addition changes one factual claim in the footer and the copy had to move
with it. v1 said destination pages are never fetched. v1.1 reads the head of an
article to find the image the publisher declared there, so the footer now says
that, and says what is still true: no article text is fetched, stored or
summarized.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .models import Item, TierResult
from .normalize import safe_url

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#fbfbfa; --fg:#16161a; --muted:#6b6b76; --faint:#9a9aa4;
  --line:#e7e7e4; --accent:#16161a; --chip:#f0f0ed; --chip-on:#16161a;
  --chip-on-fg:#fbfbfa; --echo:#6d5c2f; --echo-bg:#f6efdb; --warn:#8a4b2a;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e0e10; --fg:#ececef; --muted:#9c9ca6; --faint:#6c6c76;
    --line:#26262b; --accent:#ececef; --chip:#1d1d22; --chip-on:#ececef;
    --chip-on-fg:#0e0e10; --echo:#d8c38a; --echo-bg:#2a2418; --warn:#e0a184;
  }
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:17px; line-height:1.5;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
.wrap{max-width:44rem; margin:0 auto; padding:4.5rem 1.5rem 6rem}
header{margin-bottom:3.5rem}
h1{margin:0 0 .6rem; font-size:1.5rem; font-weight:620; letter-spacing:-.021em; line-height:1.2}
.sub{margin:0; color:var(--muted); font-size:.875rem}
.dot{color:var(--faint); margin:0 .45em}
.stale{color:var(--warn); font-weight:500}

nav{display:flex; flex-wrap:wrap; gap:.5rem; margin:2rem 0 0}
.chip{
  font:inherit; font-size:.8125rem; font-weight:500; padding:.4rem .8rem;
  border-radius:100px; border:1px solid transparent; background:var(--chip);
  color:var(--muted); cursor:pointer; transition:background .12s ease,color .12s ease;
}
.chip:hover{color:var(--fg)}
.chip[aria-pressed="true"]{background:var(--chip-on); color:var(--chip-on-fg)}
.chip:focus-visible{outline:2px solid var(--accent); outline-offset:2px}

section{margin-top:3.5rem}
section[hidden]{display:none}
h2{margin:0 0 1.25rem; font-size:.75rem; font-weight:600; text-transform:uppercase;
   letter-spacing:.09em; color:var(--faint)}
ol{list-style:none; margin:0; padding:0}
li{padding:1.1rem 0; border-top:1px solid var(--line)}
li:first-child{border-top:none; padding-top:0}
a.head{
  color:var(--fg); text-decoration:none; font-size:1.0625rem; font-weight:500;
  letter-spacing:-.011em; line-height:1.4; display:inline-block;
}
a.head:hover{text-decoration:underline; text-underline-offset:3px; text-decoration-thickness:1px}
a.head:focus-visible{outline:2px solid var(--accent); outline-offset:3px; border-radius:2px}
.meta{margin-top:.4rem; font-size:.8125rem; color:var(--muted);
      display:flex; flex-wrap:wrap; align-items:center; gap:.45rem}
.meta .sep{color:var(--faint)}
.echo{color:var(--echo); background:var(--echo-bg); padding:.08rem .42rem;
      border-radius:100px; font-size:.75rem; font-weight:500}
.via{color:var(--faint)}
.empty{color:var(--muted); font-size:.9375rem; padding:.5rem 0 0; margin:0}

footer{margin-top:5rem; padding-top:1.75rem; border-top:1px solid var(--line);
       color:var(--muted); font-size:.8125rem; line-height:1.65}
footer p{margin:0 0 .7rem}
footer a{color:var(--muted); text-decoration:underline; text-underline-offset:2px}
footer a:hover{color:var(--fg)}
.health{color:var(--faint); font-size:.78125rem}
.health .bad{color:var(--warn)}
@media (max-width:34rem){.wrap{padding:3rem 1.15rem 4rem} body{font-size:16px}}
"""

JS = """
(function(){
  var chips=[].slice.call(document.querySelectorAll('.chip'));
  var secs=[].slice.call(document.querySelectorAll('section[data-topic]'));
  function show(key){
    chips.forEach(function(c){c.setAttribute('aria-pressed', String(c.dataset.filter===key));});
    secs.forEach(function(s){s.hidden = !(key==='__all__' || s.dataset.topic===key);});
    try{localStorage.setItem('nc-filter',key);}catch(e){}
  }
  chips.forEach(function(c){c.addEventListener('click',function(){show(c.dataset.filter);});});
  var saved='__all__';
  try{saved=localStorage.getItem('nc-filter')||'__all__';}catch(e){}
  var known=chips.some(function(c){return c.dataset.filter===saved;});
  show(known?saved:'__all__');

  // Staleness is a property of WHEN YOU LOOK, so it is measured here rather
  // than baked in at build time (where it would always read as zero).
  var el=document.getElementById('stale');
  if(el){
    var built=Date.parse(el.dataset.built);
    var after=parseFloat(el.dataset.after)||3;
    if(!isNaN(built)){
      var hrs=(Date.now()-built)/3600000;
      if(hrs>=after){
        var n=Math.floor(hrs), unit='h';
        if(n>=48){n=Math.floor(hrs/24); unit=' days';}
        el.textContent='last build '+n+unit+' ago';
        el.hidden=false;
        if(el.previousElementSibling){el.previousElementSibling.hidden=false;}
      }
    }
  }
})();
"""

STALE_AFTER_HOURS = 3


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def human_age(item: Item, now: datetime) -> str:
    """How old, and honest about which timestamp that is.

    Some feeds (Atom especially) only carry an "updated" time. Showing that as a
    bare "3h ago" claims a publication time we were never given, so those rows
    say "updated 3h ago" instead.
    """
    minutes = int(item.age_hours(now) * 60)
    if minutes < 1:
        text = "just now"
    elif minutes < 60:
        text = f"{minutes}m ago"
    elif minutes < 1440:
        text = f"{minutes // 60}h ago"
    else:
        days = minutes // 1440
        text = "1 day ago" if days == 1 else f"{days} days ago"
    return f"updated {text}" if item.time_is_estimated else text


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.casefold()).strip("-") or "topic"


def _render_item(item: Item, now: datetime) -> str | None:
    # Revalidate at the output boundary. The fetchers already check, but this is
    # the last gate before a URL becomes a clickable href on a public page, and
    # a defence that only exists upstream is one refactor away from being gone.
    href = safe_url(item.url)
    if href is None:
        return None

    bits = []
    n = len(item.echo_platforms)
    if n > 1:
        bits.append(f'<span class="echo">{n} sources</span>')
    # Aggregator headlines are submitter-written. Say so rather than letting the
    # reader assume the publisher wrote it.
    label = f"via {item.source_name}" if item.is_aggregator else item.source_name
    cls = ' class="via"' if item.is_aggregator else ""
    bits.append(f"<span{cls}>{_e(label)}</span>")
    age = human_age(item, now)
    bits.append(f"<span>{_e(age)}</span>")

    meta = '<span class="sep">&middot;</span>'.join(bits)

    # The publisher's own preview image, revalidated at the output boundary like
    # every other URL. Carried as data, not as an <img>: the layout is a
    # separate decision, and a row whose publisher declared no image carries no
    # attribute at all, so a future layout can tell "none" from "empty".
    image = safe_url(item.image_url) if item.image_url else None
    image_attr = f' data-image="{_e(image)}"' if image else ""

    return (
        f"<li{image_attr}>"
        f'<a class="head" href="{_e(href)}" rel="noopener noreferrer nofollow">{_e(item.title)}</a>'
        f'<div class="meta">{meta}</div></li>'
    )


def _health_line(results: list[TierResult]) -> str:
    """Count AND state, independently.

    Hiding a partial failure behind a healthy-looking item count is exactly the
    silent degradation this line exists to prevent.
    """
    parts = []
    for r in results:
        count = len(r.items)
        if count and r.degraded:
            text = f"{r.tier}: {count} items, degraded ({r.note})"
            parts.append(f'<span class="bad">{_e(text)}</span>')
        elif count:
            parts.append(_e(f"{r.tier}: {count} items"))
        else:
            text = f"{r.tier}: {r.note or 'nothing returned'}"
            span = "bad" if not r.ok else "ok"
            parts.append(f'<span class="{span}">{_e(text)}</span>' if span == "bad" else _e(text))
    return " &middot; ".join(parts)


def edit_topics_url(repo_url: str | None, *, branch: str = "main", file: str = "topics.yaml") -> str | None:
    """GitHub's web editor for the keyword file, or None if we cannot be sure.

    This is the whole of the "add a topic" feature, and its smallness is the
    point. There is no backend to write, no form to secure and no account system
    to run: GitHub already owns the identity, the permission check, the edit
    box, the diff and the audit trail. Whoever can push to the repo can add a
    keyword; whoever cannot, gets GitHub's own fork-and-pull-request flow.

    Returned only for github.com URLs, because `/edit/<branch>/<file>` is
    GitHub's route and would be a broken link anywhere else. A fork on another
    host gets no link rather than a wrong one.
    """
    safe = safe_url(repo_url) if repo_url else None
    if not safe:
        return None
    host = urlsplit(safe).hostname or ""
    if host.lower() not in ("github.com", "www.github.com"):
        return None
    return f"{safe.rstrip('/')}/edit/{branch}/{file}"


def render_html(
    ranked: dict[str, list[Item]],
    results: list[TierResult],
    now: datetime,
    *,
    site_name: str = "News Curator",
    repo_url: str | None = None,
    built_at: datetime | None = None,
) -> str:
    built = built_at or now
    stamp = built.strftime("%b %d, %Y at %H:%M UTC").replace(" 0", " ")

    # Staleness is computed in the READER's browser, not here. The build always
    # renders itself as zero seconds old, so a server-side check could never
    # fire in production. It has to be evaluated when the page is viewed, which
    # is the only moment the answer is interesting.
    stale = (
        f'<span class="dot" hidden>&middot;</span>'
        f'<span class="stale" id="stale" data-built="{_e(built.isoformat())}" '
        f'data-after="{STALE_AFTER_HOURS}" hidden></span>'
    )

    chips = ['<button class="chip" data-filter="__all__" aria-pressed="true">All</button>']
    for name in ranked:
        chips.append(f'<button class="chip" data-filter="{_e(name)}">{_e(name)}</button>')

    sections = []
    for name, items in ranked.items():
        rows = [html for i in items if (html := _render_item(i, now)) is not None]
        body = (
            f"<ol>{''.join(rows)}</ol>"
            if rows
            else '<p class="empty">Nothing matched in this window.</p>'
        )
        sections.append(
            f'<section data-topic="{_e(name)}" id="{_slug(name)}"><h2>{_e(name)}</h2>{body}</section>'
        )

    total = sum(len(v) for v in ranked.values())
    safe_repo = safe_url(repo_url) if repo_url else None
    edit_url = edit_topics_url(safe_repo)

    if edit_url:
        # The manager's path in one line. Editing the keyword file is the only
        # thing anyone needs to change what this page collects, so that is the
        # link, pointed straight at the file rather than at the repo.
        add_line = (
            f'<p><a class="add-topic" href="{_e(edit_url)}">Add a topic or keyword</a> '
            "&mdash; edit <code>topics.yaml</code> on GitHub. If you can commit to this "
            "repository, saving rebuilds the page. Otherwise GitHub opens a pull request "
            "for the owner to merge, and it appears after they do.</p>"
        )
    else:
        add_line = (
            "<p>Add a topic or keyword by editing <code>topics.yaml</code>. "
            "The change appears here on the next build.</p>"
        )

    repo_line = (
        f'<p><a href="{_e(safe_repo)}">Open source on GitHub</a>. Fork it, edit '
        f"<code>topics.yaml</code>, and it becomes yours.</p>"
        if safe_repo
        else "<p>Open source. Fork it, edit <code>topics.yaml</code>, and it becomes yours.</p>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(site_name)}</title>
<meta name="description" content="A self-updating list of the latest headlines matching a set of keywords.">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{_e(site_name)}</h1>
  <p class="sub">Built {_e(stamp)}<span class="dot">&middot;</span>scheduled hourly<span class="dot">&middot;</span>{total} stories{stale}</p>
  <nav>{''.join(chips)}</nav>
</header>
<main>{''.join(sections)}</main>
<footer>
  <p>Headlines matching a keyword list, pulled from Hacker News and a set of RSS feeds,
     ranked by how recent and how well-matched they are. Rebuilt on a schedule.</p>
  <p>Every headline here is the text its source handed us at build time, linked to the
     address that source gave. Rows marked <span class="via">via</span> come from an
     aggregator, where the headline is written by whoever submitted the link rather than
     by the publisher. Nothing on this page is written, rewritten or summarized by a
     machine.</p>
  <p>Each story also carries the address of the preview image its publisher declared for
     it, taken from their feed or from the <code>og:image</code> tag on their page. This
     page does not display it and your browser never requests it, so there are still no
     third-party requests of any kind here. Building the page reads the head of an
     article to find that tag; no article text is stored or summarized, no claim in any
     linked article has been checked, and a link may have moved, changed or died since
     the build.</p>
  <p class="health">Sources this run &mdash; {_health_line(results)}</p>
  {add_line}
  {repo_line}
</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def render_site(
    ranked: dict[str, list[Item]],
    results: list[TierResult],
    now: datetime,
    out_dir: Path,
    *,
    site_name: str = "News Curator",
    repo_url: str | None = None,
    cname_source: Path | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    payload = render_html(ranked, results, now, site_name=site_name, repo_url=repo_url)

    # Write via a temp file in the same directory, then replace, so an
    # interrupted run can never leave a half-written page published.
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)

    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    # A CNAME committed at the repo root has to be copied into the published
    # output or a custom domain silently resets on every deploy. The README
    # tells people to add one, so the build has to honour it.
    if cname_source and cname_source.is_file():
        domain = cname_source.read_text(encoding="utf-8").strip()
        if domain:
            (out_dir / "CNAME").write_text(domain + "\n", encoding="utf-8")

    return path
