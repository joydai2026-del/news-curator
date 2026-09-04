"""Finding the preview image a publisher already declared for a story.

Two sources, cheapest first, and the order is the whole design:

  1. **The feed itself.** Many feeds ship `media:content`, `media:thumbnail` or
     an image `<enclosure>` per entry. That costs zero extra requests and works
     on publishers that block a bare article fetch, so it is always tried first.
     Handled in the RSS fetcher, not here.

  2. **`og:image` on the article.** Only for items the feed left without one.
     The safe transport reads at most a configured prefix and the parser stops
     at the end of the head. The prefix can overlap the first bytes of the body,
     but those bytes are discarded unparsed and nothing is stored. Article text
     is not retained or summarized. This reads the one meta tag the publisher
     put there specifically so links to their story render with their picture.

The image URL is HOTLINKED. Nothing is downloaded, resized, re-hosted or
re-encoded, so the publisher keeps their referrer, their CDN and the ability to
change or withdraw the image at any time.

Why the cache is committed to the repo rather than kept in a CI cache: the
answer for a given article never changes, the job runs every day, and a
committed file is both durable and auditable. Anyone can read exactly which
picture we associated with which link, and when we asked.

Two failure modes learned by measurement, both encoded below:

  * **A 403 or 429 page can still carry an `og:image` meta tag.** Several
    publishers (Industry Dive properties, The Block, CoinDesk) return a styled
    block page with its own social preview when they refuse a request. Parsing
    that would attach the publisher's generic block-page artwork to a real
    story, so a non-200 response is never parsed.
  * **Some heads are larger than they look.** A Springer article page reaches
    `</head>` at about 100 KB, so a 64 KB cap silently missed every one of them.
    The cap is 256 KB by default and is a config dial.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from .models import Item
from .normalize import safe_url
from .sources import SafeHttpPolicy, SafeHttpTransport, SafeTransportError

log = logging.getLogger(__name__)

CACHE_VERSION = 1

# In priority order. `og:image` is the standard; the rest are fallbacks real
# publishers actually use.
IMAGE_META_KEYS = (
    "og:image",
    "og:image:secure_url",
    "og:image:url",
    "twitter:image",
    "twitter:image:src",
)

DEFAULTS = {
    "enabled": True,
    "max_bytes": 262144,  # 256 KB. Enough to reach </head> on every publisher measured.
    "timeout": 10.0,
    "max_fetches_per_run": 120,
    "budget_seconds": 60.0,
    "workers": 8,
    "retain_days": 45,
    # A clean "this page declares no image" is a permanent fact and is kept for
    # the full retention window. A refusal or a timeout is a fact about one
    # moment, so it is retried after this long rather than becoming permanent.
    "retry_error_after_hours": 24.0,
}


MAX_REDIRECTS = 4


class _HeadImageParser(HTMLParser):
    """Pull image meta tags out of a document head. Stops at <body>.

    An HTML parser rather than a regex because `content` attributes contain
    quotes, angle brackets and entities, and a regex that survives all three is
    longer and less readable than this.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTMLParser has no way to stop mid-document, so the flag has to be
        # honoured on every callback. Without this check the parser reads on
        # into the body, and a stray og:image down in the article markup gets
        # picked up as if the publisher had declared it in the head.
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        # Publishers disagree about `property` vs `name` for og: tags, and both
        # appear in the wild. Accept either.
        key = (a.get("property") or a.get("name") or "").strip().lower()
        content = (a.get("content") or "").strip()
        if key in IMAGE_META_KEYS and content:
            self.found.setdefault(key, content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.done = True


def parse_image_meta(markup: str, base_url: str = "") -> str | None:
    """The image this document declares for social previews, or None.

    Relative URLs are resolved against `base_url`, then run through the same
    scheme allow-list as every other link on the page. A publisher's markup is
    not trusted to decide what ends up in an `src` attribute.
    """
    if not markup:
        return None
    parser = _HeadImageParser()
    try:
        parser.feed(markup)
    except Exception:  # a malformed document is a miss, never a crash
        log.debug("image meta parse failed for %s", base_url or "<unknown>")
    for key in IMAGE_META_KEYS:
        raw = parser.found.get(key)
        if not raw:
            continue
        candidate = urljoin(base_url, raw) if base_url else raw
        cleaned = safe_url(candidate)
        if cleaned:
            return cleaned
    return None


def fetch_image_meta(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    max_bytes: int,
    transport: SafeHttpTransport | None = None,
) -> tuple[str | None, str]:
    """Read one article's head. Returns (image_url_or_None, outcome).

    Three outcomes, and the distinction drives how long each is cached:

      * `"ok"`    — an image was found.
      * `"none"`  — a DEFINITIVE answer that there is no image here: the page
                    was read to the end of its head and declared none, or it is
                    not HTML at all. That will not change, so it is cached for
                    the full retention window.
      * `"error"` — anything non-definitive: refused, timed out, redirected
                    somewhere we will not follow, or truncated before the head
                    ended. Those are facts about one moment, so they get the
                    short retry TTL rather than becoming permanent.

    The shared safe transport resolves and pins each hop, validates the peer
    and TLS before sending request bytes, follows redirects only after another
    complete validation, and never inherits proxy or netrc configuration.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    }
    selected = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=timeout,
            max_wire_bytes=max_bytes,
            max_decoded_bytes=max_bytes,
            max_redirects=MAX_REDIRECTS,
            read_chunk_bytes=min(16_384, max_bytes),
        )
    )
    if isinstance(selected, SafeHttpTransport):
        base = selected.policy
        bound = min(max_bytes, base.max_wire_bytes, base.max_decoded_bytes)
        selected = selected.with_policy(
            SafeHttpPolicy(
                total_timeout_seconds=min(timeout, base.total_timeout_seconds),
                max_wire_bytes=bound,
                max_decoded_bytes=bound,
                max_request_bytes=base.max_request_bytes,
                max_header_bytes=base.max_header_bytes,
                max_redirects=min(MAX_REDIRECTS, base.max_redirects),
                max_content_encodings=base.max_content_encodings,
                per_host_concurrency=base.per_host_concurrency,
                read_chunk_bytes=min(base.read_chunk_bytes, bound),
            )
        )
    try:
        response = selected.get(
            "image-meta",
            url,
            headers=headers,
            allow_truncated_response=True,
            user_agent=user_agent,
        )
    except SafeTransportError as exc:
        log.debug("image fetch failed with %s", exc.reason_code)
        return None, "error"
    except Exception:
        log.debug("image fetch failed")
        return None, "error"

    # A block page is not an article. Parsing a 403 attaches the publisher's
    # generic "you are blocked" artwork to a real story.
    if response.status_code != 200:
        log.debug("image fetch got HTTP %s", response.status_code)
        return None, "error"

    ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype and not (
        ctype.endswith("/html") or ctype.endswith("+xml") or ctype == "text/plain"
    ):
        return None, "none"

    payload = response.body[:max_bytes]
    lowered = payload.lower()
    head_ended = b"</head>" in lowered or b"<body" in lowered
    # Exactly hitting the cap without reaching the end of the head is not a
    # definitive miss. SafeHttpTransport has already enforced the same cap on
    # both compressed and decoded bytes.
    truncated = response.body_truncated and not head_ended
    markup = payload.decode("utf-8", errors="replace")
    image = parse_image_meta(markup, response.url)
    if image:
        return image, "ok"
    return None, "none" if head_ended and not truncated else "error"


class ImageCache:
    """Canonical URL -> the image we found, and when we looked.

    Committed to the repo so a daily job does not ask a publisher the same
    question again. "Again" rather than "twice": a non-definitive miss is
    retried after `retry_error_after_hours`, and a link pruned for going stale
    would be looked up afresh if it ever reappeared.
    """

    def __init__(self, path: Path, entries: dict | None = None) -> None:
        self.path = path
        self.entries: dict[str, dict] = entries or {}
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> ImageCache:
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt cache is a performance problem, never a build failure.
            log.warning("image cache unreadable (%s), starting empty", exc)
            return cls(path)
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            log.info("image cache version mismatch, starting empty")
            return cls(path)
        entries = raw.get("entries")
        return cls(path, entries if isinstance(entries, dict) else {})

    def get(self, key: str, now: datetime, *, retry_error_after_hours: float) -> tuple[bool, str]:
        """(is_a_usable_hit, image_url). A miss means "go and ask"."""
        row = self.entries.get(key)
        if not isinstance(row, dict):
            return False, ""
        outcome = str(row.get("outcome") or "")
        if outcome == "error":
            checked = _parse_time(row.get("checked_at"))
            if checked is None or now - checked >= timedelta(hours=retry_error_after_hours):
                return False, ""
        # Revalidate on the way OUT, not just on the way in. This file is
        # committed to the repo, so its contents are editable by hand and by any
        # future code path that writes it. Trusting a value because "we wrote it
        # once" is exactly the assumption that goes stale after a refactor, and
        # the cost of checking is one function call.
        image = row.get("image")
        if not isinstance(image, str) or not image:
            return True, ""
        return True, safe_url(image) or ""

    def put(self, key: str, image: str | None, outcome: str, now: datetime) -> None:
        self.entries[key] = {
            "image": image or None,
            "outcome": outcome,
            "checked_at": now.replace(microsecond=0).isoformat(),
            "seen_at": now.replace(microsecond=0).isoformat(),
        }
        self._dirty = True

    def touch(self, key: str, now: datetime) -> None:
        """Record that this link is still in circulation, so pruning spares it."""
        row = self.entries.get(key)
        if isinstance(row, dict):
            stamp = now.replace(microsecond=0).isoformat()
            if row.get("seen_at") != stamp:
                row["seen_at"] = stamp
                self._dirty = True

    def prune(self, now: datetime, *, retain_days: float) -> int:
        """Drop links we have not seen in a while, so the file cannot grow forever."""
        cutoff = now - timedelta(days=retain_days)
        # A row whose `seen_at` will not parse is pruned, not kept. The earlier
        # `or cutoff` fallback made it `cutoff < cutoff`, i.e. False, so any
        # malformed row became immortal and the file could grow without bound
        # after a single hand edit or merge-conflict resolution. Failing toward
        # dropping a cache entry is free; failing toward keeping it is not.
        stale = []
        for key, row in self.entries.items():
            if not isinstance(row, dict):
                stale.append(key)
                continue
            seen = _parse_time(row.get("seen_at"))
            if seen is None or seen < cutoff:
                stale.append(key)
        for key in stale:
            del self.entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def save(self) -> bool:
        """Write only when something changed, so the daily job makes no empty commits."""
        if not self._dirty:
            return False
        payload = {
            "version": CACHE_VERSION,
            "note": (
                "Preview images the publisher declared, keyed by canonical URL. "
                "Written by curator/images.py. Images are hotlinked, never rehosted."
            ),
            "entries": dict(sorted(self.entries.items())),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
        return True


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _setting(cfg: dict, key: str):
    value = cfg.get(key, DEFAULTS[key])
    return DEFAULTS[key] if value is None else value


def enrich(
    items: list[Item],
    cache: ImageCache,
    now: datetime,
    *,
    user_agent: str,
    config: dict | None = None,
    transport: SafeHttpTransport | None = None,
) -> dict[str, int]:
    """Attach a preview image to every item that does not already have one.

    Called AFTER ranking and truncation, so the only pages fetched are the ones
    a reader will actually see. That is what keeps a daily job bounded: the
    ceiling is the number of visible rows, not the number of headlines fetched.

    Returns counters for the run receipt. Never raises: a page without a picture
    is a smaller problem than a build that did not happen.
    """
    cfg = config or {}
    stats = {"total": 0, "from_feed": 0, "from_cache": 0, "fetched": 0,
             "no_image": 0, "errors": 0, "capped": 0, "budget_hit": 0,
             "newsletter_skipped": 0}

    # De-duplicate by canonical URL: the same story shown in two categories is
    # two Item objects and exactly one article to ask about.
    pending: dict[str, list[Item]] = {}
    for item in items:
        stats["total"] += 1
        # Newsletter items never reach the network and never reach the cache.
        #
        # Their URLs routinely carry a subscriber identifier: a tracking
        # redirect, a hosted-view token, a per-recipient hash. Fetching one
        # tells the sender which subscriber's mail was processed and when.
        # Writing one into `image_cache.json` would publish it, permanently, in
        # a public repository. The pipeline is also expected to skip these, and
        # that is exactly why the check is repeated here: the pipeline decides
        # what to enrich, this function decides what it is willing to request,
        # and a privacy rule that exists in only one of those is one refactor
        # away from being gone. The `continue` is before `cache.touch` on
        # purpose, so nothing about a newsletter item is even written back as a
        # timestamp.
        if item.is_newsletter:
            stats["newsletter_skipped"] += 1
            continue
        key = item.canonical_url
        if not key:
            continue
        cache.touch(key, now)
        if item.image_url:
            stats["from_feed"] += 1
            continue
        pending.setdefault(key, []).append(item)

    if not pending:
        return stats

    # The cache is consulted BEFORE the enabled gate, on purpose. Reading it
    # touches no network, so an offline run still shows every picture it has
    # already learned about. Gating here instead would make `--offline` render a
    # page with no images at all, which is not what "no network" means.
    retry_after = float(_setting(cfg, "retry_error_after_hours"))
    todo: list[str] = []
    for key, group in pending.items():
        hit, image = cache.get(key, now, retry_error_after_hours=retry_after)
        if hit:
            stats["from_cache"] += 1
            if image:
                for item in group:
                    item.image_url = image
            continue
        todo.append(key)

    if not todo or not bool(_setting(cfg, "enabled")):
        return stats

    max_fetches = int(_setting(cfg, "max_fetches_per_run"))
    budget = float(_setting(cfg, "budget_seconds"))
    timeout = float(_setting(cfg, "timeout"))
    max_bytes = int(_setting(cfg, "max_bytes"))
    workers = max(1, min(16, int(_setting(cfg, "workers"))))

    if len(todo) > max_fetches:
        stats["capped"] = len(todo) - max_fetches
        log.info("image lookups capped at %d this run; %d resolve next run",
                 max_fetches, stats["capped"])
    todo = todo[:max_fetches]

    started = time.monotonic()

    selected_transport = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=timeout,
            max_wire_bytes=max_bytes,
            max_decoded_bytes=max_bytes,
            per_host_concurrency=workers,
            read_chunk_bytes=min(16_384, max_bytes),
        )
    )

    def work(key: str) -> tuple[str, str | None, str]:
        # Ask the publisher for the address they actually published, not our
        # normalized comparison key, which has had tracking parameters stripped.
        url = pending[key][0].url
        image, outcome = fetch_image_meta(
            url,
            user_agent=user_agent,
            timeout=timeout,
            max_bytes=max_bytes,
            transport=selected_transport,
        )
        return key, image, outcome

    pool = futures.ThreadPoolExecutor(max_workers=workers)
    try:
        jobs = {pool.submit(work, key): key for key in todo}
        # `as_completed` WITHOUT a timeout was the hole in this budget: if
        # every in-flight request stalled, the loop blocked forever and the
        # check at the bottom was never reached. The budget has to be armed
        # here, on the wait itself, or it only fires when work is already
        # finishing, which is when it is not needed.
        try:
            completed = futures.as_completed(jobs, timeout=budget)
            for future in completed:
                try:
                    key, image, outcome = future.result()
                except Exception as exc:  # a worker bug must not lose the build
                    log.warning("image lookup raised: %s", exc)
                    stats["errors"] += 1
                    continue

                cache.put(key, image, outcome, now)
                if outcome == "ok" and image:
                    stats["fetched"] += 1
                    for item in pending[key]:
                        item.image_url = image
                elif outcome == "none":
                    stats["no_image"] += 1
                else:
                    stats["errors"] += 1

                if time.monotonic() - started > budget:
                    log.info("image time budget of %.0fs reached", budget)
                    raise futures.TimeoutError
        except futures.TimeoutError:
            # Whatever has not finished is simply a cache miss next run.
            # Cancel what has not started; a request already in flight is
            # bounded by its own transfer deadline, not by this.
            unfinished = [f for f in jobs if not f.done()]
            stats["budget_hit"] = len(unfinished)
            for future in unfinished:
                future.cancel()
            log.info("image budget left %d lookups for the next run", len(unfinished))
    finally:
        # `wait=False` is the other half of the budget. Exiting a `with` block
        # calls shutdown(wait=True), which JOINS every still-running request, so
        # a stalled publisher would hold the build open long past the deadline
        # no matter what the loop above decided. Queued work is cancelled; work
        # already in flight is bounded by its own per-request transfer deadline,
        # which is what makes the total worst case "budget plus one request"
        # rather than "budget plus however long the slowest server feels like".
        pool.shutdown(wait=False, cancel_futures=True)
    return stats
