"""Finding the preview image a publisher already declared for a story.

Two sources, cheapest first, and the order is the whole design:

  1. **The feed itself.** Many feeds ship `media:content`, `media:thumbnail` or
     an image `<enclosure>` per entry. That costs zero extra requests and works
     on publishers that block a bare article fetch, so it is always tried first.
     Handled in the RSS fetcher, not here.

  2. **`og:image` on the article.** Only for items the feed left without one.
     We stream the response and stop as soon as the head ends, so the article
     body is never parsed and nothing is stored. Being precise, because this is
     the sort of promise that quietly becomes false: the final chunk read can
     overlap the first bytes of the body, and those bytes are discarded
     unparsed. Article TEXT is not fetched, not stored and not summarized. This
     reads the one meta tag the publisher put there specifically so that links
     to their story render with their picture.

The image URL is HOTLINKED. Nothing is downloaded, resized, re-hosted or
re-encoded, so the publisher keeps their referrer, their CDN and the ability to
change or withdraw the image at any time.

Why the cache is committed to the repo rather than kept in a CI cache: the
answer for a given article never changes, the job runs every hour, and a
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
import ipaddress
import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .models import Item
from .normalize import safe_url

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


def is_public_host(url: str) -> bool:
    """Does every address this URL resolves to sit on the public internet?

    v1 never fetched a destination page, so this is new attack surface and it
    gets a real gate rather than a comment. A feed we do not control now
    supplies addresses that a CI runner will request, and the request aimed at
    the runner's own network (`127.0.0.1`, `10.x`) or at the cloud metadata
    endpoint (`169.254.169.254`) is the thing to refuse.

    The name is RESOLVED, and EVERY address it resolves to must be global. A
    literal-address check alone is not protection: `evil.example` pointing at
    `169.254.169.254` passes a string test and fails this one. Resolution
    failure is treated as "not public", because a name we cannot resolve is a
    name we cannot vouch for.

    Callers must also follow redirects MANUALLY and re-check each hop, since a
    public address is free to redirect somewhere private. `fetch_image_meta`
    does that.

    The residual is a DNS rebind between this check and the connect, which
    needs pinning the resolved address into the socket to close. That one is
    recorded rather than half-solved: it is a race an attacker must win against
    a request made from an ephemeral container on a public repository.
    """
    host = (urlsplit(url).hostname or "").strip("[]")
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_global

    # Not a canonical address, so it is either a real name or an address written
    # in a form designed to slip past a check like this one. `2130706433`,
    # `127.1`, `0x7f.1` and `0177.0.0.1` are all 127.0.0.1 to a C resolver and
    # none of them parses as an IP above.
    #
    # `0177.0.0.1` is the one that proves the point: `getaddrinfo` returns
    # 177.0.0.1 (global, so it would PASS) while a client applying octal rules
    # connects to 127.0.0.1. Two parsers disagreeing is not a residual to
    # document, it is a bypass. A real domain never ends in an all-numeric
    # label, so requiring that is both cheap and complete.
    last = host.rsplit(".", 1)[-1]
    if not last or last.isdigit() or last.lower().startswith("0x"):
        return False

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not resolved.is_global:
            return False
    return True


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
    session: requests.Session | None = None,
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

    Redirects are followed MANUALLY, one hop at a time, re-checking that each
    destination is public before the next request is made. `allow_redirects`
    would have requests chase a hostile 302 into a private address before any
    check could run, which is the difference between refusing to PARSE an
    internal page and refusing to REQUEST it.
    """
    get = (session or requests).get
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    }

    current = url
    for hop in range(MAX_REDIRECTS + 1):
        if not is_public_host(current):
            log.warning("image fetch refused, non-public destination (hop %d): %s", hop, url)
            return None, "error"
        try:
            resp = get(current, timeout=timeout, stream=True, allow_redirects=False, headers=headers)
        except Exception as exc:
            log.debug("image fetch failed for %s: %s", current, exc)
            return None, "error"

        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location")
                if not location:
                    return None, "error"
                # Resolve relative redirects against the hop we just made.
                current = urljoin(current, location)
                if safe_url(current) is None:
                    log.debug("image fetch redirect to an unsupported scheme: %s", url)
                    return None, "error"
                continue

            # A block page is not an article. Parsing a 403 attaches the
            # publisher's generic "you are blocked" artwork to a real story.
            if resp.status_code != 200:
                log.debug("image fetch got HTTP %s for %s", resp.status_code, current)
                return None, "error"

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (ctype.endswith("/html") or ctype.endswith("+xml") or ctype == "text/plain"):
                # A PDF or an image will never declare an og:image. Definitive.
                return None, "none"

            buf = bytearray()
            head_ended = False
            truncated = False
            deadline = time.monotonic() + timeout
            try:
                for chunk in resp.iter_content(16384):
                    # Trim BEFORE appending, so the cap is a real ceiling rather
                    # than a ceiling plus one chunk.
                    room = max_bytes - len(buf)
                    if room <= 0:
                        truncated = True
                        break
                    buf.extend(chunk[:room])
                    # The window exceeds one chunk, so a sentinel split across a
                    # chunk boundary is still found.
                    if b"</head>" in bytes(buf[-24576:]).lower() or b"<body" in bytes(buf[-24576:]).lower():
                        head_ended = True
                        break
                    if len(buf) >= max_bytes:
                        truncated = True
                        break
                    if time.monotonic() > deadline:
                        # `timeout` is per read, not per transfer, so a server
                        # dripping bytes below it would hold this worker open
                        # indefinitely. This is the total-transfer deadline.
                        log.debug("image stream exceeded its transfer deadline: %s", current)
                        truncated = True
                        break
            except Exception as exc:
                log.debug("image stream failed for %s: %s", current, exc)
                return None, "error"

            markup = bytes(buf).decode(resp.encoding or "utf-8", errors="replace")
            image = parse_image_meta(markup, current)
            if image:
                return image, "ok"
            # Reaching the end of the head and finding nothing is a real answer.
            # Being cut off before it is not, so it must not be cached as one.
            return None, "none" if head_ended and not truncated else "error"
        finally:
            resp.close()

    log.debug("image fetch exceeded %d redirects: %s", MAX_REDIRECTS, url)
    return None, "error"


class ImageCache:
    """Canonical URL -> the image we found, and when we looked.

    Committed to the repo so an hourly job does not ask a publisher the same
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
        """Write only when something changed, so the hourly job makes no empty commits."""
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
) -> dict[str, int]:
    """Attach a preview image to every item that does not already have one.

    Called AFTER ranking and truncation, so the only pages fetched are the ones
    a reader will actually see. That is what keeps an hourly job bounded: the
    ceiling is the number of visible rows, not the number of headlines fetched.

    Returns counters for the run receipt. Never raises: a page without a picture
    is a smaller problem than a build that did not happen.
    """
    cfg = config or {}
    stats = {"total": 0, "from_feed": 0, "from_cache": 0, "fetched": 0,
             "no_image": 0, "errors": 0, "capped": 0, "budget_hit": 0}

    # De-duplicate by canonical URL: the same story shown in two categories is
    # two Item objects and exactly one article to ask about.
    pending: dict[str, list[Item]] = {}
    for item in items:
        stats["total"] += 1
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

    # One Session PER THREAD, not one shared. `requests.Session` is not
    # documented as thread-safe, and sharing one across a pool races on the
    # cookie jar and the redirect state. A thread-local keeps connection reuse
    # (the reason to want a session at all) without the shared mutable state.
    local = threading.local()
    sessions: list[requests.Session] = []
    sessions_lock = threading.Lock()

    def work(key: str) -> tuple[str, str | None, str]:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
            # Kept so the `finally` below can close every one. The thread-local
            # itself is unreachable from here once the pool threads are gone.
            with sessions_lock:
                sessions.append(session)
        # Ask the publisher for the address they actually published, not our
        # normalized comparison key, which has had tracking parameters stripped.
        url = pending[key][0].url
        image, outcome = fetch_image_meta(
            url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes, session=session
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
        for session in sessions:
            session.close()

    return stats
