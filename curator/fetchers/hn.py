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

from ..config import Category, Config
from ..models import Item, SourceHealth, TierResult
from ..normalize import canonical_url, clean_title, safe_url

log = logging.getLogger(__name__)

API = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id="

# A fork with fifty keywords should not fire two hundred requests every day.
# Two independent brakes, because a request cap alone does not bound TIME: 60
# requests each hitting a 15-second timeout is 15 minutes, which is the whole
# CI job budget. The wall-clock budget is the one that actually protects the run.
MAX_REQUESTS_PER_RUN = 60
DEFAULT_BUDGET_SECONDS = 120.0


def _to_item(
    hit: dict,
    weight: float,
    *,
    native_category: str = "",
    native_rank: int | None = None,
) -> Item | None:
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
        language="en",
        native_rank=native_rank,
        native_categories={native_category} if native_category else set(),
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


def fetch(cfg: Config, topics: list[Category]) -> TierResult:
    hn_cfg = cfg.hackernews or {}
    if not hn_cfg.get("enabled", True):
        return TierResult(tier="hackernews", ok=True, note="disabled in sources.yaml")

    weight = float(hn_cfg.get("weight", 1.0))
    hits_per_page = int(hn_cfg.get("hits_per_page", 40))
    min_points = int(hn_cfg.get("min_points_ranked", 20))
    by_date = bool(hn_cfg.get("include_by_date", True))
    cutoff = int(time.time() - cfg.max_age_hours * 3600)

    # Interleave categories rather than draining one at a time. Many categories
    # with twenty keywords each produce far more query plans than the per-run
    # cap allows, and taking them in file order would spend the entire budget on
    # whichever category happens to be written first, leaving the rest with no
    # Hacker News coverage at all and no visible sign of it. Round-robin means
    # the cap degrades every section a little instead of starving five of them.
    #
    # `search_terms` is a category's `hn_queries` when it has them, which is the
    # real fix: a short hand-picked query list per category, because each term
    # costs two API requests while a local keyword costs nothing.
    per_category = [list(dict.fromkeys(c.search_terms)) for c in topics]
    ordered_terms: list[str] = []
    for i in range(max((len(t) for t in per_category), default=0)):
        for terms in per_category:
            if i < len(terms):
                ordered_terms.append(terms[i])

    plans: list[tuple[str, str, str]] = []
    seen_terms: set[str] = set()
    for term in ordered_terms:
        # One query per term: Algolia ORs loose tokens in a way that widens the
        # net unhelpfully when several keywords are jammed together. Two
        # categories sharing a term is one query, not two.
        if term.casefold() in seen_terms:
            continue
        seen_terms.add(term.casefold())
        plans.append((term, "search", f"created_at_i>{cutoff},points>={min_points}"))
        if by_date:
            plans.append((term, "search_by_date", f"created_at_i>{cutoff}"))

    max_requests = int(hn_cfg.get("max_requests", MAX_REQUESTS_PER_RUN))
    budget = float(hn_cfg.get("budget_seconds", DEFAULT_BUDGET_SECONDS))
    capped = len(plans) > max_requests
    plans = plans[:max_requests]

    items: list[Item] = []
    failures = 0
    started = time.monotonic()
    exhausted = False
    source_health: list[SourceHealth] = []

    # One additive front-page request supplies the first-class Trending lane.
    # The legacy topic queries below remain intact for topical coverage.
    front_category = str(hn_cfg.get("front_page_category") or "trending")
    front_limit = int(hn_cfg.get("front_page_hits_per_page", 30))
    front_threshold = float(hn_cfg.get("front_page_max_age_hours", 12))
    front_items: list[Item] = []
    front_status = "fresh"
    front_reason = ""
    try:
        front_hits = _query("search", {"tags": "front_page", "hitsPerPage": front_limit}, cfg)
    except Exception as exc:
        log.warning("HN front_page failed: %s", exc)
        front_status = "unavailable"
        front_reason = "request_failed"
    else:
        for native_rank, hit in enumerate(front_hits[:front_limit]):
            item = _to_item(
                hit,
                weight,
                native_category=front_category,
                native_rank=native_rank,
            )
            if item is not None:
                front_items.append(item)
        if not front_items:
            front_status = "empty"
            front_reason = "no_usable_items"
    items.extend(front_items)

    checked_at = datetime.now(timezone.utc)
    newest = max((item.published_at for item in front_items), default=None)
    age = max(0.0, (checked_at - newest).total_seconds() / 3600.0) if newest else None
    if front_status == "fresh" and age is not None and age > front_threshold:
        front_status = "stale"
        front_reason = "newest_item_too_old"
    source_health.append(
        SourceHealth(
            source_id="hn-front",
            status=front_status,
            usable_items=len(front_items),
            newest_at=newest,
            age_hours=age,
            max_age_hours=front_threshold,
            language="en",
            source_type="api",
            echo_eligible=True,
            reason_code=front_reason,
        )
    )

    for i, (term, endpoint, numeric) in enumerate(plans):
        if time.monotonic() - started > budget:
            exhausted = True
            log.warning("HN time budget of %.0fs reached after %d/%d queries", budget, i, len(plans))
            break
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
        note = "topic queries unavailable this run"
        if front_status != "fresh":
            note = f"front page {front_status}; {note}"
        return TierResult(
            tier="hackernews",
            items=items,
            ok=False,
            note=note,
            source_health=source_health,
        )

    notes = []
    if failures:
        notes.append(f"{failures} of {len(plans)} queries failed")
    if capped:
        notes.append(f"query cap {max_requests} reached, some keywords not searched")
    if exhausted:
        notes.append("time budget reached, some keywords not searched")
    if front_status != "fresh":
        notes.insert(0, f"front page {front_status}")
    return TierResult(
        tier="hackernews",
        items=items,
        ok=not notes,
        note="; ".join(notes),
        source_health=source_health,
    )
