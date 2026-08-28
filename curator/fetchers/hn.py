"""Tier A — Hacker News via the Algolia API.

No auth, no key, and no quota hit in testing (10 rapid sequential calls, 0
failures, ~0.3 s each) from a residential IP. That is the evidence; it is not a
guarantee about datacenter IPs or about sustained load, so requests are capped
per run and failures are tolerated rather than retried hard.

Two endpoints are queried and merged:
  * `search`         — relevance and popularity ranked, with a points floor,
                       which is where quality comes from.
  * `search_by_date` — genuinely newest first, no floor, which is where
                       freshness comes from. Its top results are typically
                       1-point submissions nobody has seen yet, so it cannot be
                       used alone.

**Hacker News is an aggregator, and that matters for honesty.** The `title` is
written by the SUBMITTER, while `url` points at someone else's article. A
submitter can and does retitle. So HN items are flagged `is_aggregator=True`,
and if the publisher's own feed gave us the same link, the publisher's title
wins in dedup. Where HN is the only source for a link, the row is labeled
"Hacker News" so the reader knows whose words the headline is.

Nothing here filters by keyword. Algolia matches fuzzily (querying `AI` returned
a story about malaria), so results are candidates only and the strict local
filter decides what survives.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from ..config import Config, Topic
from ..models import Item, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

API = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id="

# A fork with fifty keywords should not fire two hundred requests every hour.
MAX_REQUESTS_PER_RUN = 60


def _to_item(hit: dict, weight: float) -> Item | None:
    title = clean_title(hit.get("title") or hit.get("story_title") or "")
    if not title:
        return None

    created = hit.get("created_at_i")
    if not created:
        return None
    try:
        published = datetime.fromtimestamp(int(created), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None

    # Ask HN / Show HN text posts have no external URL. Link to the thread so
    # the row still goes somewhere real rather than being dropped.
    url = safe_url(hit.get("url") or "")
    if url is None:
        object_id = str(hit.get("objectID") or "").strip()
        if not object_id.isdigit():
            return None
        url = f"{HN_ITEM}{object_id}"

    canonical = canonical_url(url)
    if canonical is None:
        return None

    return Item(
        title=title,
        url=url,
        canonical_url=canonical,
        source_id="hackernews",
        source_name="Hacker News",
        platform="hackernews",
        published_at=published,
        source_weight=weight,
        score=int(hit.get("points") or 0),
        is_aggregator=True,
    )


def _query(endpoint: str, params: dict, cfg: Config) -> list[dict]:
    resp = requests.get(
        f"{API}/{endpoint}",
        params=params,
        timeout=cfg.timeout,
        headers={"User-Agent": cfg.user_agent},
    )
    resp.raise_for_status()
    payload = resp.json()
    hits = payload.get("hits")
    return hits if isinstance(hits, list) else []


def fetch(cfg: Config, topics: list[Topic]) -> TierResult:
    hn_cfg = cfg.hackernews or {}
    if not hn_cfg.get("enabled", True):
        return TierResult(tier="hackernews", ok=True, note="disabled in sources.yaml")

    weight = float(hn_cfg.get("weight", 1.0))
    hits_per_page = int(hn_cfg.get("hits_per_page", 40))
    min_points = int(hn_cfg.get("min_points_ranked", 20))
    by_date = bool(hn_cfg.get("include_by_date", True))
    cutoff = int(time.time() - cfg.max_age_hours * 3600)

    plans: list[tuple[str, str, str]] = []
    for topic in topics:
        # One query per term: Algolia ORs loose tokens in a way that widens the
        # net unhelpfully when several keywords are jammed together.
        for term in topic.all_terms:
            plans.append((term, "search", f"created_at_i>{cutoff},points>={min_points}"))
            if by_date:
                plans.append((term, "search_by_date", f"created_at_i>{cutoff}"))

    capped = len(plans) > MAX_REQUESTS_PER_RUN
    plans = plans[:MAX_REQUESTS_PER_RUN]

    items: list[Item] = []
    failures = 0

    for term, endpoint, numeric in plans:
        try:
            hits = _query(
                endpoint,
                {"query": term, "tags": "story", "hitsPerPage": hits_per_page, "numericFilters": numeric},
                cfg,
            )
        except Exception as exc:  # network, HTTP, or malformed JSON
            failures += 1
            log.warning("HN %s failed for %r: %s", endpoint, term, exc)
            continue
        for hit in hits:
            item = _to_item(hit, weight)
            if item is not None:
                items.append(item)

    if plans and failures == len(plans):
        return TierResult(tier="hackernews", items=[], ok=False, note="unavailable this run")

    notes = []
    if failures:
        notes.append(f"{failures} of {len(plans)} queries failed")
    if capped:
        notes.append(f"query cap {MAX_REQUESTS_PER_RUN} reached, some keywords not searched")
    return TierResult(tier="hackernews", items=items, ok=True, note="; ".join(notes))
