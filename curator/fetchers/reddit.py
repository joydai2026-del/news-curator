"""Tier C — Reddit. Off by default, and that is not an oversight.

Measured from a residential IP on 2026-08-25:

    /r/<sub>/hot.json          HTTP 403 with every User-Agent tried
    /r/<sub>/.rss, bursty      HTTP 200 once, then HTTP 429 for everything after
    /r/<sub>/.rss, 60s apart   HTTP 200 every time, 10 entries each

The last line is the one that matters: the 429 is a short-window burst limit,
not a ban. Reddit over RSS works if you go slow, and only if you go slow.

It stays disabled by default for two honest reasons:

  1. GitHub Actions runners use shared datacenter IPs, the traffic class Reddit
     blocks hardest, and that cannot be tested from a laptop.
  2. Reddit's own guidance points at registered OAuth for programmatic access.
     Unauthenticated RSS polling is tolerated rather than sanctioned, and a fork
     that wants dependable Reddit coverage should register an app. That is
     documented in the README rather than half-wired here, because a
     half-implemented auth path is worse than an honest missing one.

The fetcher is built to fail politely: serial requests with a long delay, the
whole tier stopping on the first 429 rather than hammering, and partial results
kept and reported rather than discarded.

Reddit is an aggregator: titles are submitter-written and point at someone
else's article, so items are flagged accordingly and lose dedup ties to the
publisher's own feed.
"""

from __future__ import annotations

import logging
import time
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import requests

from ..config import Config
from ..models import Item, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

FEED = "https://www.reddit.com/r/{sub}/.rss?limit=50"


class RateLimited(Exception):
    """Reddit told us to stop. We stop, for the whole tier, for this run."""


def _fetch_sub(sub: str, cfg: Config, weight: float) -> list[Item]:
    resp = requests.get(
        FEED.format(sub=sub),
        timeout=cfg.timeout,
        headers={"User-Agent": cfg.user_agent, "Accept": "application/atom+xml, application/xml, */*"},
    )
    if resp.status_code == 429:
        raise RateLimited("rate-limited")
    if resp.status_code in (401, 403):
        raise RateLimited(f"blocked (HTTP {resp.status_code})")
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    now = datetime.now(timezone.utc)
    items: list[Item] = []

    for entry in parsed.entries:
        title = clean_title(entry.get("title") or "")
        link = safe_url(entry.get("link") or "")
        if not title or link is None:
            continue
        canonical = canonical_url(link)
        if canonical is None:
            continue

        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        estimated = not entry.get("published_parsed")
        if not stamp:
            continue
        try:
            published = datetime.fromtimestamp(timegm(stamp), tz=timezone.utc)
        except (ValueError, OverflowError, TypeError):
            continue

        items.append(
            Item(
                title=title,
                url=link,
                canonical_url=canonical,
                source_id=f"reddit:{sub}",
                source_name=f"r/{sub}",
                platform="reddit",  # all subreddits are one platform for echo
                published_at=min(published, now),
                source_weight=weight,
                is_aggregator=True,
                time_is_estimated=estimated,
            )
        )
    return items


def fetch(cfg: Config) -> TierResult:
    rc = cfg.reddit or {}
    if not rc.get("enabled", False):
        return TierResult(tier="reddit", ok=True, note="off by default (see README)")

    subs = [str(s).strip() for s in (rc.get("subreddits") or []) if str(s).strip()]
    if not subs:
        return TierResult(tier="reddit", ok=True, note="no subreddits configured")

    weight = float(rc.get("weight", 0.9))
    delay = float(rc.get("request_delay_seconds", 30))

    items: list[Item] = []
    done = 0

    for i, sub in enumerate(subs):
        if i:
            time.sleep(delay)
        try:
            items.extend(_fetch_sub(sub, cfg, weight))
            done += 1
        except RateLimited as exc:
            note = f"{exc} after {done}/{len(subs)} subreddits"
            log.warning("reddit: %s", note)
            # Partial data is still data. Report it honestly and stop asking.
            return TierResult(tier="reddit", items=items, ok=False, note=note)
        except Exception as exc:
            log.warning("reddit r/%s failed: %s", sub, exc)

    note = "" if done == len(subs) else f"{done}/{len(subs)} subreddits fetched"
    return TierResult(tier="reddit", items=items, ok=done == len(subs), note=note)
