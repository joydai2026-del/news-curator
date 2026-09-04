"""One static page. No framework, no build step, and no runtime media requests.

Design intent (M1 Reading Companion): headline-only accordion rows. A headline
is the entire collapsed surface. Opening it reveals the source-provided summary,
provenance, a deterministic explanation of why it appeared, and the original
link. The page uses the approved warm-paper visual direction, remains useful on
a phone, and does not pretend that future Ask, Save, feedback, or lane controls
already work.

Four corrections from review are load-bearing here, and three of them predate
this layout:

  * **Health is count AND state, separately.** A tier that returned ten items
    and then got rate-limited used to render as a reassuring "reddit: 10". It
    now renders as "Reddit: 10 items, degraded (rate-limited after 2/5)".
  * **"Scheduled daily", not "refreshes daily".** GitHub delays and drops
    scheduled runs under load, and disables them entirely after 60 days of
    repository inactivity. The page states the schedule and shows how old the
    build actually is, and says so out loud when that is more than 27 hours.
  * **The accuracy note is narrowed to what the code can actually prove.** We
    can promise the source handed us this headline, this description and this
    address at build time, and that aggregator headlines are labeled as such. We
    cannot promise the link is still live or still carries that title.
  * **One story is one card.** A story matching three categories used to render
    three times, once per section. It is now a single card cross-tagged with
    three category slugs, which is the only way a search box or an "All" view
    can count honestly.

**Why the ranking lives in attributes rather than in the DOM order.** Each
category ranks its stories independently, so the same card sits at position 2
under AI and position 17 under Crypto. Rendering it twice would restore the
duplicate problem; re-sorting the DOM on every tab switch would move focus and
break the open card. So each card carries `data-rank-<slug>` for every category
it belongs to, plus `data-rank-all`, and switching a tab sets the CSS `order`
property from the matching attribute. One DOM node per story, exact per-tab
ranking, no reflow of anything but position.

The DOM order is the "All" order, so the default view needs no reordering at all
and keyboard order matches what the eye sees. Inside a category tab, `order`
moves cards visually without moving them in the tab sequence. That is the known
cost of the grid-order approach and it is written down rather than hidden.

**Source image metadata is not page media.** The source-provided preview address
stays as a `data-image` attribute because the deploy workflow measures coverage
with it. The Reading Companion does not render an image element or ask the
reader's browser to fetch that address.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
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
  --card:#ffffff; --card-line:#e7e7e4; --card-line-on:#d0d0ca;
  --shadow:0 1px 2px rgba(0,0,0,.045); --shadow-on:0 6px 20px rgba(0,0,0,.07);
  --radius:14px;
  --fb-sat:52%; --fb-a:95%; --fb-b:88%; --fb-ink:34%; --fb-mark:.5;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e0e10; --fg:#ececef; --muted:#9c9ca6; --faint:#6c6c76;
    --line:#26262b; --accent:#ececef; --chip:#1d1d22; --chip-on:#ececef;
    --chip-on-fg:#0e0e10; --echo:#d8c38a; --echo-bg:#2a2418; --warn:#e0a184;
    --card:#151517; --card-line:#26262b; --card-line-on:#3b3b44;
    --shadow:none; --shadow-on:0 6px 24px rgba(0,0,0,.5);
    --fb-sat:34%; --fb-a:17%; --fb-b:12%; --fb-ink:74%; --fb-mark:.34;
  }
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:17px; line-height:1.5;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
.wrap{max-width:78rem; margin:0 auto; padding:4rem 1.5rem 6rem}
header{margin-bottom:2rem}
h1{margin:0 0 .6rem; font-size:1.5rem; font-weight:620; letter-spacing:-.021em; line-height:1.2}
.sub{margin:0; color:var(--muted); font-size:.875rem}
.dot{color:var(--faint); margin:0 .45em}
.stale{color:var(--warn); font-weight:500}

.tools{display:flex; flex-wrap:wrap; align-items:center; gap:.75rem 1rem; margin:2rem 0 0}
nav{display:flex; flex-wrap:wrap; gap:.5rem; margin:0; flex:1 1 20rem}
.chip{
  font:inherit; font-size:.8125rem; font-weight:500; padding:.4rem .8rem;
  border-radius:100px; border:1px solid transparent; background:var(--chip);
  color:var(--muted); cursor:pointer; transition:background .12s ease,color .12s ease;
}
.chip:hover{color:var(--fg)}
.chip[aria-pressed="true"]{background:var(--chip-on); color:var(--chip-on-fg)}
.chip:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
.find{display:flex; align-items:center; gap:.6rem; flex:0 1 auto}
input.q{
  font:inherit; font-size:.8125rem; padding:.42rem .9rem; min-width:13rem; width:100%;
  border-radius:100px; border:1px solid var(--line); background:var(--chip); color:var(--fg);
  -webkit-appearance:none; appearance:none;
}
input.q::placeholder{color:var(--faint)}
input.q:focus-visible{outline:2px solid var(--accent); outline-offset:1px}
.count{margin:0; font-size:.8125rem; color:var(--faint); white-space:nowrap}

.grid{
  display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:1.35rem; margin:2.25rem 0 0; align-items:start;
}
.card{
  border:1px solid var(--card-line); border-radius:var(--radius); background:var(--card);
  overflow:hidden; display:flex; flex-direction:column; box-shadow:var(--shadow);
  cursor:pointer; transition:border-color .16s ease, transform .16s ease, box-shadow .16s ease;
}
.card:hover{border-color:var(--card-line-on); transform:translateY(-2px)}
.card.open{grid-column:1 / -1; transform:none; box-shadow:var(--shadow-on); border-color:var(--card-line-on)}
.card[hidden]{display:none}

.shot{position:relative; display:block; width:100%; aspect-ratio:3 / 2; overflow:hidden; background:var(--chip)}
/* A card with no picture is a typographic card, not a card with a hole in it.
   The panel is shorter than a 3:2 photo because there is no photograph to crop,
   and it carries the category name itself, so the eyebrow in the body below
   would be the same word twice. Both rules key off `data-image`, which is the
   one attribute that says whether a publisher gave us a picture at all. */
.card:not([data-image]) .shot{aspect-ratio:16 / 7}
.card:not([data-image]) > .pad > .eyebrow,
.card.noimg > .pad > .eyebrow{display:none}
/* An unfolded card is the full width of the grid, and 21:9 of 1200px is a
   550px hero that pushes the detail off the screen. The ratio sets the shape,
   the cap keeps the thing you opened the card to read above the fold. */
.card.open .shot{aspect-ratio:21 / 9; max-height:17rem}
.shot img{position:absolute; inset:0; width:100%; height:100%; object-fit:cover; display:block; border:0}
.fb{
  position:absolute; inset:0; display:flex; flex-direction:column; justify-content:flex-end;
  padding:1rem 1.1rem; overflow:hidden;
  background:linear-gradient(135deg,
    hsl(var(--h), var(--fb-sat), var(--fb-a)) 0%,
    hsl(calc(var(--h) + 24), var(--fb-sat), var(--fb-b)) 100%);
}
.fb::after{
  content:""; position:absolute; right:-2.2rem; top:-2.6rem; width:9rem; height:9rem;
  border-radius:50%; border:1px solid hsl(var(--h), var(--fb-sat), var(--fb-ink));
  opacity:var(--fb-mark);
}
.fb b{
  display:block; width:1.75rem; height:2px; border-radius:2px; margin-bottom:.55rem;
  background:hsl(var(--h), var(--fb-sat), var(--fb-ink)); opacity:.75;
}
.fb span{
  position:relative; font-size:.6875rem; font-weight:650; text-transform:uppercase;
  letter-spacing:.1em; color:hsl(var(--h), var(--fb-sat), var(--fb-ink));
}

.pad{padding:.95rem 1.05rem 1.05rem; display:flex; flex-direction:column; flex:1}
.eyebrow{margin:0; font-size:.6875rem; font-weight:650; text-transform:uppercase;
         letter-spacing:.09em; color:var(--faint)}
.hl{margin:.4rem 0 0; font-size:1.0rem; font-weight:600; line-height:1.32; letter-spacing:-.014em;
    display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden}
.card.open .hl{display:block; font-size:1.1875rem}
a.head, span.head{color:var(--fg); text-decoration:none}
a.head:hover{text-decoration:underline; text-underline-offset:3px; text-decoration-thickness:1px}
a.head:focus-visible{outline:2px solid var(--accent); outline-offset:3px; border-radius:2px}
.desc{margin:.45rem 0 0; font-size:.875rem; line-height:1.5; color:var(--muted);
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden}
.card.open .desc{display:none}
.meta{margin-top:.75rem; font-size:.8125rem; color:var(--muted);
      display:flex; flex-wrap:wrap; align-items:center; gap:.45rem}
.meta .sep{color:var(--faint)}
.echo{color:var(--echo); background:var(--echo-bg); padding:.08rem .42rem;
      border-radius:100px; font-size:.75rem; font-weight:500}
.via{color:var(--faint)}
.chev{
  font:inherit; font-size:.75rem; color:var(--muted); margin-left:auto; cursor:pointer;
  background:none; border:1px solid var(--line); border-radius:100px; padding:.12rem .6rem;
  white-space:nowrap;
}
.chev:hover{color:var(--fg); border-color:var(--card-line-on)}
.chev:focus-visible{outline:2px solid var(--accent); outline-offset:2px}

.detail{border-top:1px solid var(--line); margin-top:.9rem; padding-top:.9rem;
        font-size:.875rem; color:var(--muted); line-height:1.55; cursor:auto}
.detail[hidden]{display:none}
.detail .full{margin:0 0 .9rem; color:var(--fg); max-width:64ch}
.detail .row{display:flex; gap:.7rem; align-items:baseline; margin:0 0 .45rem}
.detail .row b{flex:0 0 7.5rem; color:var(--fg); font-weight:600; font-size:.8125rem}
.detail .row span{min-width:0}
.detail a{color:var(--fg); text-decoration:underline; text-underline-offset:2px}
.acts{display:flex; flex-wrap:wrap; gap:.5rem; margin-top:1rem}
.acts a, .acts button{
  font:inherit; font-size:.78125rem; font-weight:500; color:var(--fg); cursor:pointer;
  background:none; border:1px solid var(--line); border-radius:100px; padding:.3rem .85rem;
  text-decoration:none;
}
.acts a:hover, .acts button:hover{border-color:var(--card-line-on)}
.acts a:focus-visible, .acts button:focus-visible{outline:2px solid var(--accent); outline-offset:2px}

.empty{color:var(--muted); font-size:.9375rem; margin:2.25rem 0 0}
.empty[hidden]{display:none}

footer{margin-top:4.5rem; padding-top:1.75rem; border-top:1px solid var(--line);
       color:var(--muted); font-size:.8125rem; line-height:1.65}
footer p{margin:0 0 .7rem; max-width:74ch}
footer a{color:var(--muted); text-decoration:underline; text-underline-offset:2px}
footer a:hover{color:var(--fg)}
.health{color:var(--faint); font-size:.78125rem; max-width:none}
.health .bad{color:var(--warn)}
@media (max-width:600px){
  .wrap{padding:2.5rem 1.15rem 4rem} body{font-size:16px}
  .grid{grid-template-columns:1fr; gap:1.1rem; margin-top:1.75rem}
  .card.open .shot{aspect-ratio:3 / 2; max-height:none}
  .find{flex:1 1 100%}
  .detail .row{flex-direction:column; gap:.1rem}
  .detail .row b{flex:none}
}
@media (prefers-reduced-motion:reduce){
  .card, .chip{transition:none}
  .card:hover{transform:none}
}

/* M1 Reading Companion. These rules deliberately follow the approved
   accordion mockup while keeping the existing one-story/one-node filter and
   ranking contract. */
:root{
  --bg:#f5f4ef; --card:#fffefa; --fg:#1d211f; --muted:#59615d;
  --faint:#78817c; --line:#d9ddd8; --card-line:#d9ddd8;
  --card-line-on:#aebbb3; --accent:#245d48; --accent-soft:#e5f0e9;
  --accent-fg:#fff; --warm:#f2e9d8; --blue:#e5edf4; --shadow:0 18px 55px rgba(38,46,41,.1);
  --serif:ui-serif,"New York",Georgia,"Noto Serif SC",serif;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Noto Sans SC",sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0d100e; --card:#151916; --fg:#f4f6f3; --muted:#b9c0bb;
    --faint:#8d9790; --line:#303731; --card-line:#303731;
    --card-line-on:#526158; --accent:#8fc9ae; --accent-soft:#1b2d23;
    --accent-fg:#0d100e; --warm:#2b2419; --blue:#1b262d; --shadow:0 18px 55px rgba(0,0,0,.36);
  }
}
html{overflow-x:hidden;background:var(--bg)}
body{overflow-x:hidden;background:
  radial-gradient(circle at 12% 0%,var(--warm),transparent 30rem),
  radial-gradient(circle at 92% 18%,var(--blue),transparent 34rem),var(--bg);
  font-family:var(--sans)}
.wrap{max-width:88rem;padding:1.4rem 1.5rem 5rem}
.shell{display:grid;grid-template-columns:15rem minmax(0,1fr);gap:1.5rem;align-items:start}
.rail{position:sticky;top:1.4rem;max-height:calc(100vh - 2.8rem);overflow-y:auto;border:1px solid var(--line);background:color-mix(in srgb,var(--card) 88%,transparent);
  box-shadow:var(--shadow);border-radius:1.35rem;padding:1.35rem 1rem;backdrop-filter:blur(18px)}
.brand{font:600 1.55rem/1.1 var(--serif);letter-spacing:-.025em;padding:0 .5rem}
.brand small{display:block;margin-top:.5rem;font:650 .625rem/1.4 var(--sans);letter-spacing:.11em;text-transform:uppercase;color:var(--faint)}
.rail h2{margin:1.35rem .55rem .5rem;font-size:.625rem;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
.railnav{display:grid;gap:.2rem}
.railnav .chip{width:100%;min-height:44px;display:flex;align-items:center;justify-content:flex-start;border-radius:.7rem;padding:.5rem .65rem;background:transparent;color:var(--muted);text-align:left}
.railnav .chip[aria-pressed="true"]{background:var(--accent-soft);color:var(--accent);font-weight:700}
.railnote{margin:1.25rem .5rem 0;padding-top:.9rem;border-top:1px solid var(--line);font-size:.7rem;line-height:1.55;color:var(--faint)}
.maincol{min-width:0}
.topbar{display:flex;gap:1rem;align-items:center;justify-content:space-between;margin-bottom:1rem}
.crumb{font-size:.75rem;color:var(--faint)}
.profile-slot{min-height:44px;display:flex;align-items:center}
.profile-link{min-width:44px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--accent);border-radius:999px;padding:.5rem .9rem;color:var(--accent);font-size:.78rem;font-weight:650;text-decoration:none;background:var(--card)}
.profile-link:hover{background:var(--accent-soft)}
.profile-link:focus-visible,.accordion-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.intro{border:1px solid var(--line);background:color-mix(in srgb,var(--card) 92%,transparent);border-radius:1.5rem;padding:1.75rem 1.9rem;box-shadow:var(--shadow)}
.intro .eyebrow{font-size:.625rem;font-weight:750;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
.intro h1{margin:.5rem 0 .45rem;font:600 clamp(2rem,4vw,3rem)/1.04 var(--serif);letter-spacing:-.04em;max-width:16ch}
.intro p{margin:0;color:var(--muted);max-width:68ch;font-size:.9rem}
.edition-meta{display:flex;flex-wrap:wrap;gap:.45rem;margin-top:1.05rem}
.edition-meta span{border:1px solid var(--line);border-radius:999px;background:var(--card);padding:.3rem .65rem;font-size:.7rem;color:var(--muted)}
.tools{position:sticky;top:0;z-index:30;display:flex;gap:.75rem;align-items:center;margin:1rem 0;padding:.7rem;border:1px solid var(--line);border-radius:1rem;background:color-mix(in srgb,var(--bg) 91%,transparent);backdrop-filter:blur(16px)}
.mobiletopics{display:none;flex-wrap:nowrap;gap:.45rem;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0}
.mobiletopics::-webkit-scrollbar{display:none}
.mobiletopics .chip{min-width:44px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;border:1px solid var(--line);background:var(--card);padding:.5rem .8rem}
.mobiletopics .chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.find{min-width:min(20rem,42vw)}
input.q{min-width:0;min-height:44px;border-radius:999px;background:var(--card)}
.countline{min-height:1.5rem;margin:.2rem .15rem .8rem;color:var(--faint);font-size:.72rem}
.count{display:inline}
.grid{display:flex;flex-direction:column;gap:0;align-items:stretch;margin:0;border:1px solid var(--line);border-radius:1.1rem;background:var(--card);overflow:hidden;box-shadow:0 8px 28px rgba(38,46,41,.05)}
.card{display:block;width:100%;border:0;border-radius:0;background:transparent;box-shadow:none;overflow:visible;cursor:default;transition:none}
.card+.card{border-top:1px solid color-mix(in srgb,var(--line) 64%,transparent)}
.card:hover,.card.open{border-color:transparent;transform:none;box-shadow:none}
.card[hidden]{display:none}
.story-heading{margin:0;font:inherit}
.accordion-toggle{width:100%;min-height:58px;border:0;background:transparent;text-align:left;display:grid;grid-template-columns:minmax(0,1fr) 34px;gap:.65rem;align-items:center;padding:1.05rem 1.15rem;cursor:pointer}
.accordion-toggle:hover{background:color-mix(in srgb,var(--accent-soft) 38%,transparent)}
.headline{font:600 1.06rem/1.34 var(--serif);letter-spacing:-.012em}
.chev{display:grid;place-items:center;width:30px;height:30px;margin:0;border:1px solid var(--line);border-radius:50%;padding:0;color:var(--faint);background:transparent;transition:transform .2s ease}
.accordion-toggle[aria-expanded="true"] .chev{transform:rotate(180deg);color:var(--accent);border-color:var(--accent)}
.panel{padding:0 1.15rem 1.15rem;background:linear-gradient(180deg,color-mix(in srgb,var(--accent-soft) 55%,transparent),transparent)}
.panelin{border-top:1px solid var(--line);padding-top:1rem;display:grid;grid-template-columns:minmax(0,1fr) 15rem;gap:1.5rem}
.summary{font-family:var(--serif);color:var(--muted);font-size:.96rem;line-height:1.62}
.summary p{margin:0}
.provenance{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.8rem;font:700 .625rem/1.4 var(--sans);letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.provenance-chip{border:1px solid var(--line);border-radius:999px;background:var(--card);padding:.25rem .5rem;color:var(--accent);letter-spacing:0;text-transform:none}
.details{margin-top:.8rem;display:grid;gap:.38rem;font:400 .78rem/1.5 var(--sans);color:var(--muted)}
.detail .row{display:grid;grid-template-columns:6.5rem minmax(0,1fr);gap:.65rem;margin:0;align-items:baseline}
.detail .row b{font-size:.72rem;color:var(--fg);font-weight:650}
.signal{border:1px solid var(--line);background:var(--card);border-radius:.8rem;padding:.75rem}
.signal b{display:block;font-size:.625rem;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin-bottom:.25rem}
.signal span{display:block;font-size:.72rem;line-height:1.45;color:var(--muted)}
.acts{margin-top:1rem}
.acts a,.acts button{min-width:44px;min-height:44px;display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:.5rem .9rem;color:var(--accent);background:var(--card);font-size:.78rem;font-weight:650}
.empty{margin:1.5rem 0}
footer{margin-top:2rem;padding:1.25rem .25rem 0}
.shot,.pad>.eyebrow,.hl,.desc,.meta{display:none!important}
@media (max-width:980px){
  .shell{grid-template-columns:1fr}.rail{display:none}.wrap{padding:1rem 1rem 4rem}
  .tools{top:0}.mobiletopics{display:flex}.panelin{grid-template-columns:1fr;gap:1rem}.topbar{align-items:flex-start}
}
@media (max-width:620px){
  .wrap{padding:.75rem .75rem 3rem}.intro{padding:1.35rem 1.15rem;border-radius:1.15rem}
  .intro h1{font-size:2.15rem}.topbar{gap:.5rem}.crumb{padding-top:.5rem}
  .tools{margin-left:-.1rem;margin-right:-.1rem;flex-direction:column;align-items:stretch}
  .find{min-width:0;width:100%}.accordion-toggle{padding:.95rem .85rem}.headline{font-size:1rem}
  .panel{padding:0 .85rem 1rem}.detail .row{grid-template-columns:1fr;gap:.05rem}
}
@media (prefers-reduced-motion:reduce){.chev{transition:none}}
"""

JS = """
(function(){
  var grid=document.getElementById('grid');
  var chips=[].slice.call(document.querySelectorAll('.chip'));
  var box=document.getElementById('q');
  var count=document.getElementById('count');
  var empty=document.getElementById('empty');
  var tab='__all__';

  // The search index is built from what is already on the page rather than from
  // a duplicate copy in an attribute. The text is right there in the DOM;
  // shipping it twice would grow every card for nothing.
  var index=[].slice.call(document.querySelectorAll('.card')).map(function(card){
    var h=card.querySelector('.headline'), d=card.querySelector('.full');
    return {
      el:card,
      topics:' '+(card.getAttribute('data-topics')||'')+' ',
      text:((h?h.textContent:'')+' '+(d?d.textContent:'')).toLowerCase()
    };
  });

  function collapse(card){
    card.classList.remove('open');
    var d=card.querySelector('.detail'); if(d){d.hidden=true;}
    var b=card.querySelector('.accordion-toggle');
    if(b){b.setAttribute('aria-expanded','false');}
  }
  function expand(card){
    card.classList.add('open');
    var d=card.querySelector('.detail'); if(d){d.hidden=false;}
    var b=card.querySelector('.accordion-toggle');
    if(b){b.setAttribute('aria-expanded','true');}
  }
  function toggle(card){
    if(card.classList.contains('open')){collapse(card); return;}
    index.forEach(function(e){if(e.el!==card){collapse(e.el);}});
    expand(card);
  }

  function apply(){
    var q=((box&&box.value)||'').trim().toLowerCase();
    var attr=(tab==='__all__')?'data-rank-all':'data-rank-'+tab;
    var shown=0;
    index.forEach(function(e){
      var on=(tab==='__all__'||e.topics.indexOf(' '+tab+' ')>=0)&&(!q||e.text.indexOf(q)>=0);
      e.el.hidden=!on;
      if(on){
        // CSS order does the per-tab reordering. One DOM node per story, exact
        // ranking per category, and nothing moves in the document.
        var r=e.el.getAttribute(attr);
        e.el.style.order=(r===null)?'0':r;
        shown++;
      }else if(e.el.classList.contains('open')){
        collapse(e.el);
      }
    });
    if(count){count.textContent=q?(shown+(shown===1?' matching story':' matching stories')):'';}
    if(empty){
      empty.hidden=shown>0;
      empty.textContent=q?('No story here matches \\u201c'+q+'\\u201d.'):'Nothing matched in this window.';
    }
  }

  function setTab(key){
    tab=key;
    chips.forEach(function(c){c.setAttribute('aria-pressed',String(c.dataset.filter===key));});
    try{localStorage.setItem('nc-tab',key);}catch(e){}
    apply();
  }

  chips.forEach(function(c){c.addEventListener('click',function(){setTab(c.dataset.filter);});});
  if(box){box.addEventListener('input',apply);}

  if(grid){
    grid.addEventListener('click',function(ev){
      var t=ev.target;
      if(!t||!t.closest) return;
      if(t.closest('a')) return;                                  // the outbound link wins
      var card=t.closest('.card');
      if(!card) return;
      if(t.closest('.detail')&&!t.closest('.shut')) return;       // let people select the detail text
      toggle(card);
    });
  }
  document.addEventListener('keydown',function(ev){
    if(ev.key!=='Escape') return;
    index.forEach(function(e){if(e.el.classList.contains('open')){collapse(e.el);}});
  });

  var saved='__all__';
  try{saved=localStorage.getItem('nc-tab')||'__all__';}catch(e){}
  setTab(chips.some(function(c){return c.dataset.filter===saved;})?saved:'__all__');

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

STALE_AFTER_HOURS = 27


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


ACCENT_BASE_HUE = 212  # a blue to start from, so the first category reads calm


def _accent_hues(slugs: list[str]) -> dict[str, int]:
    """One hue per category, spread evenly around the wheel.

    The categories live in `topics.yaml` and a fork replaces all six of them, so
    a hardcoded palette would be a list of colours chosen for someone else's
    sections. The first attempt hashed the slug instead, and measuring it killed
    it: the six shipped categories landed on hues 74, 79, 90, 95, 108 and 108,
    which is five greens and an exact collision. A hash gives you a stable
    number, not a spread one, and "every category has its own colour" is a
    statement about spread.

    Position does both. Even spacing guarantees the maximum distance any set of
    categories can have from each other, and the order of `topics.yaml` is as
    stable as its contents. The cost is that adding a seventh category shifts
    the other six, which on a page rebuilt daily from a file edited a few times
    a year is not a cost anyone will notice.
    """
    count = len(slugs) or 1
    return {
        slug: int(ACCENT_BASE_HUE + round(360 * index / count)) % 360
        for index, slug in enumerate(slugs)
    }


def _timestamp(item: Item) -> str:
    """The exact time, spelled out, and labelled for what it actually is."""
    stamp = item.published_at.strftime("%b %d, %Y at %H:%M UTC").replace(" 0", " ")
    return f"Updated {stamp}" if item.time_is_estimated else stamp


@dataclass
class _Card:
    """One story, once, however many categories claimed it.

    `ranks` is slug -> that story's position in that category's ranked list, and
    it is the whole reason a single node can serve every tab. `label` is the
    category shown on the face of the card: the one where this story ranked
    best, because that is the section it most belongs to.
    """

    item: Item
    ranks: dict[str, int] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    image: str = ""
    description: str = ""

    @property
    def best(self) -> tuple[int, str]:
        slug = min(self.ranks, key=lambda s: (self.ranks[s], s))
        return self.ranks[slug], slug

    @property
    def label(self) -> str:
        return self.labels[self.best[1]]


def _collect_cards(ranked: dict[str, list[Item]]) -> tuple[list[_Card], dict[str, str]]:
    """Fold the per-category ranked lists into one card per unique story.

    `assign_categories` hands every category its own COPY of an item, so the
    same story arriving in three buckets is three objects that share a canonical
    URL and differ in `matched_keywords`. Identity therefore has to be the
    canonical URL, not the object, and the keyword lists are unioned so an
    unfolded card shows every term that put it anywhere.

    Returns the cards in "All" order (best rank across categories, then newest
    first) and the slug -> display name map the tabs are built from. Emitting
    the DOM in that order means the default view needs no reordering and its
    keyboard order matches what the eye sees.
    """
    cards: dict[str, _Card] = {}
    order: list[_Card] = []
    names: dict[str, str] = {}

    for name, items in ranked.items():
        slug = _slug(name)
        # Two category names can slugify to the same string ("AI!" and "AI?").
        # Letting them collide would merge two tabs into one silently, so the
        # second one gets a suffix instead.
        if names.get(slug, name) != name:
            suffix = 2
            while names.get(f"{slug}-{suffix}", name) != name:
                suffix += 1
            slug = f"{slug}-{suffix}"
        names[slug] = name

        for position, item in enumerate(items):
            key = item.canonical_url or item.url
            if not key:
                continue
            card = cards.get(key)
            if card is None:
                card = _Card(item=item, image=item.image_url, description=item.description)
                cards[key] = card
                order.append(card)
            # A category may not list the same story twice, but if it ever did,
            # the better position is the true one.
            card.ranks[slug] = min(position, card.ranks.get(slug, position))
            card.labels[slug] = name
            # The copies are otherwise identical, so the first one to carry a
            # picture or a summary supplies it for all of them.
            card.image = card.image or item.image_url
            card.description = card.description or item.description
            for keyword in item.matched_keywords:
                if keyword not in card.keywords:
                    card.keywords.append(keyword)

    order.sort(key=lambda c: (c.best[0], -c.item.published_at.timestamp(), c.item.title))
    return order, names


def _cluster_links(card: _Card) -> str:
    """"Also covered by" — the outlets a merge folded away, named and linked.

    Revalidated here like every other URL. These addresses came from a source we
    do not control, travelled through the deduper, and are about to become
    hrefs on a public page.

    The newsletter sanitizer runs here too, on EVERY cluster link, even though
    the deduper now refuses newsletter URLs upstream. This is the output
    boundary, and a defence that only exists upstream is one refactor away
    from being gone; review round 1 found exactly that gap in this function.
    A cleanable link is cleaned (tracking params stripped); a link the
    sanitizer refuses outright is dropped, entry and all.
    """
    from .newsletter.sanitize import sanitize as nl_sanitize

    links = []
    for entry in card.item.cluster:
        if not isinstance(entry, dict):
            continue
        href = safe_url(str(entry.get("url") or ""))
        if not href:
            continue
        href = nl_sanitize(href)
        if not href:
            continue
        name = str(entry.get("source_name") or "") or (urlsplit(href).hostname or "the source")
        links.append(
            f'<a href="{_e(href)}" rel="noopener noreferrer nofollow">{_e(name)}</a>'
        )
    if not links:
        return ""
    return f'<div class="row"><b>Also covered by</b><span>{", ".join(links)}</span></div>'


def _why_this_appeared(card: _Card, now: datetime) -> str:
    """Explain the deterministic signals visible to this renderer.

    The renderer does not receive private preference values or raw scores, so
    it names only evidence it can prove from the ranked item. The published
    order still reflects saved-interest ranking when the build supplies it.
    """
    rank, _slug_key = card.best
    signals: list[str] = []
    if card.keywords:
        signals.append("topic fit")
    if card.item.age_hours(now) <= 24:
        signals.append("freshness")
    source_count = len(card.item.echo_platforms)
    if source_count > 1:
        signals.append(f"coverage from {source_count} sources")
    if card.item.is_newsletter:
        signals.append("newsletter coverage")
    elif card.item.is_aggregator:
        signals.append(f"a discovery signal from {card.item.source_name}")
    elif card.item.native_categories:
        signals.append("coverage from a configured topic source")
    if not signals:
        signals.append("topic ranking")
    if len(signals) == 1:
        reason = signals[0]
    else:
        reason = ", ".join(signals[:-1]) + f", and {signals[-1]}"
    return f"Best rank #{rank + 1} in {card.label}. Visible signals include {reason}."


def _render_card(card: _Card, now: datetime, hues: dict[str, int], index: int, all_rank: int) -> str | None:
    """One story as one headline-first accordion row.

    Two rules decide whether a card can carry a link, and they are not the same
    rule:

      * An ordinary item whose URL fails revalidation is DROPPED. The fetchers
        already checked, but this is the last gate before a URL becomes a
        clickable href on a public page, and a defence that only exists upstream
        is one refactor away from being gone.
      * A NEWSLETTER item whose URL is empty is RENDERED UNLINKED. That empty
        string is the sanitizer reporting, deliberately, that it could not
        recover a clean publisher address from a link carrying a subscriber
        identifier. The story is still real and still worth showing; what we
        will not do is publish the identifier. So the headline renders as text,
        with no href anywhere on the card.
    """
    item = card.item
    href = safe_url(item.url) if item.url else None
    if href is None and not item.is_newsletter:
        return None

    _ = hues  # Category hues remain part of the stable renderer API.
    echo = len(item.echo_platforms)
    if item.is_newsletter and item.newsletter_sender:
        source_label = f"via {item.newsletter_sender}"
    elif item.is_aggregator:
        source_label = f"via {item.source_name}"
    else:
        source_label = item.source_name

    detail_id = f"d{index}"
    toggle_id = f"t{index}"
    rows = []
    if card.description:
        summary = card.description
    elif href:
        summary = "This source did not provide a summary. Open the original story for the full context."
    else:
        summary = "This source did not provide a summary or a safe public link."
    summary_class = "full" if card.description else "summary-notice"
    summary_label = "Source summary" if card.description else "Summary unavailable"
    if item.is_newsletter and item.newsletter_sender:
        rows.append(f'<div class="row"><b>Newsletter</b><span>{_e(item.newsletter_sender)}</span></div>')
    else:
        rows.append(f'<div class="row"><b>Source</b><span>{_e(item.source_name)}</span></div>')
    rows.append(f'<div class="row"><b>Published</b><span>{_e(_timestamp(item))}</span></div>')
    cluster = _cluster_links(card)
    if cluster:
        rows.append(cluster)
    if card.keywords:
        rows.append(
            f'<div class="row"><b>Matched on</b><span>{_e(", ".join(card.keywords))}</span></div>'
        )
    acts = []
    if href:
        acts.append(
            f'<a href="{_e(href)}" rel="noopener noreferrer nofollow">Read original</a>'
        )
    else:
        rows.append(
            '<div class="row"><b>No link</b><span>This item arrived with a link we could '
            "not clean of subscriber identifiers, so it is shown without one.</span></div>"
        )
    acts.append('<button type="button" class="shut">Close</button>')
    provenance = [
        f'<span class="provenance-chip">{_e(card.label)}</span>',
        f'<span class="provenance-chip">{_e(source_label)}</span>',
        f'<span class="provenance-chip">{_e(human_age(item, now))}</span>',
    ]
    if echo > 1:
        provenance.append(f'<span class="provenance-chip">{echo} sources</span>')
    detail = (
        f'<div class="panel detail" id="{detail_id}" role="region" '
        f'aria-labelledby="{toggle_id}" hidden><div class="panelin">'
        f'<div class="summary"><div class="provenance" aria-label="Story provenance">'
        f'{summary_label}{"".join(provenance)}</div><p class="{summary_class}">{_e(summary)}</p>'
        f'<div class="details">{"".join(rows)}</div><div class="acts">{"".join(acts)}</div></div>'
        f'<aside class="signal"><b>Why this appeared</b><span>{_e(_why_this_appeared(card, now))}'
        f'</span></aside></div></div>'
    )

    topics = " ".join(sorted(card.ranks, key=lambda s: card.ranks[s]))
    rank_attrs = "".join(f' data-rank-{slug}="{rank}"' for slug, rank in sorted(card.ranks.items()))
    # `data-image` stays on the article even though M1 no longer renders an
    # <img>: the deploy workflow counts source image coverage with one cheap
    # regex over this metadata. It still says exactly what it always said, which
    # is what the publisher declared. Newsletter items never carry it.
    image = safe_url(card.image) if (card.image and not item.is_newsletter) else None
    image_attr = f' data-image="{_e(image)}"' if image else ""
    # A machine-readable marker for newsletter-derived cards. The deploy
    # workflow's privacy check keys off it (a newsletter card carrying
    # `data-image` fails the build), and it makes the lane auditable with one
    # grep. Without it that check could never fire, which is worse than not
    # having it.
    newsletter_attr = ' data-newsletter=""' if item.is_newsletter else ""
    return (
        f'<article class="card" data-topics="{_e(topics)}" data-rank-all="{all_rank}"'
        f"{rank_attrs}{image_attr}{newsletter_attr}>"
        f'<h2 class="story-heading"><button type="button" class="accordion-toggle" aria-expanded="false" '
        f'aria-controls="{detail_id}" id="{toggle_id}"><span class="headline">{_e(item.title)}</span>'
        f'<span class="chev" aria-hidden="true">⌄</span></button></h2>'
        f"{detail}"
        f"</article>"
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

    cards, names = _collect_cards(ranked)
    hues = _accent_hues(list(names))

    chips = ['<button class="chip" data-filter="__all__" aria-pressed="true">All</button>']
    for slug, name in names.items():
        chips.append(
            f'<button class="chip" data-filter="{_e(slug)}" aria-pressed="false">{_e(name)}</button>'
        )

    rendered = []
    for position, card in enumerate(cards):
        markup = _render_card(card, now, hues, len(rendered), position)
        if markup is not None:
            rendered.append(markup)

    # The count is of STORIES, not of rows. One story matching three categories
    # used to be counted three times, which made the number bigger and wronger.
    total = len(rendered)
    empty_hidden = " hidden" if rendered else ""

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
<meta name="description" content="A daily reading companion with sourced news summaries.">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<div class="shell">
  <aside class="rail" aria-label="News Curator navigation">
    <div class="brand">{_e(site_name)}<small>Daily Reading Companion</small></div>
    <h2>Topics</h2>
    <nav class="railnav" aria-label="Topic categories">{''.join(chips)}</nav>
    <p class="railnote">One current edition. Open any headline for the source summary and ranking context.</p>
  </aside>
  <div class="maincol">
    <div class="topbar">
      <div class="crumb">{_e(site_name)} / Today's edition</div>
      <div class="profile-slot"><!-- personalization-link --></div>
    </div>
    <header class="intro">
      <div class="eyebrow">Today's edition</div>
      <h1>Your daily reading companion</h1>
      <p>Open a headline for the source summary, provenance, and a plain explanation of why it appeared.</p>
      <div class="edition-meta">
        <span>Built {_e(stamp)}</span><span>scheduled daily</span><span>{total} stories</span>{stale}
      </div>
    </header>
    <div class="tools">
      <nav class="mobiletopics" aria-label="Categories">{''.join(chips)}</nav>
      <div class="find">
        <input class="q" id="q" type="search" placeholder="Search this edition"
               aria-label="Search these stories" autocomplete="off" spellcheck="false">
      </div>
    </div>
    <p class="countline"><span class="count" id="count" role="status" aria-live="polite"></span></p>
    <main>
      <div class="grid" id="grid">{''.join(rendered)}</div>
      <p class="empty" id="empty"{empty_hidden}>Nothing matched in this window.</p>
    </main>
    <footer>
      <p>This edition combines Hacker News, RSS feeds, news sitemaps, and eligible newsletter items,
         then builds one deduplicated story list. When a configured saved-interest profile is
         present, the build uses it as an additional ranking input. Rebuilt on a schedule.</p>
      <p>Every headline and summary is text the named source supplied at build time. Rows marked
         <span class="via">via</span> came through an aggregator or newsletter. Nothing here is
         written, rewritten, or summarized by a machine, and no linked claim has been checked.</p>
      <p>The Reading Companion loads no publisher images, third-party scripts, web fonts, or
         analytics. Original links use <code>no-referrer</code>. The build may read a publisher's
         image metadata for coverage reporting, but the page does not request or display that image.
         No destination article body is stored or summarized.</p>
      <p class="health">Sources this run &mdash; {_health_line(results)}</p>
      {add_line}
      {repo_line}
    </footer>
  </div>
</div>
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
