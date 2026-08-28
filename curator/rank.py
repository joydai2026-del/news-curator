"""Scoring one item, for one topic, at one moment.

Four terms, all weights in config. The function is pure and takes an explicit
`now`, so it can be tested without mocking the clock.

Native popularity (Hacker News points) deliberately does NOT appear as a fifth
term. It enters earlier, as a floor at fetch time. Feeding it in here would let
one 900-point story permanently outrank everything from sources that have no
score at all, which is most of them.
"""

from __future__ import annotations

import math
from datetime import datetime

from .config import Topic
from .filter import match_position
from .models import Item


def recency_score(item: Item, now: datetime, half_life_hours: float) -> float:
    """1.0 at publication, 0.5 after one half-life, approaching 0 after that."""
    if half_life_hours <= 0:
        return 1.0
    return 0.5 ** (item.age_hours(now) / half_life_hours)


def keyword_score(item: Item, topic: Topic, *, lead_chars: int, lead_bonus: float) -> float:
    """How strongly this item is about this topic.

    More distinct keywords hitting is a stronger signal, with diminishing
    returns. A keyword near the front of the headline usually means the article
    is ABOUT the topic rather than mentioning it in passing.

    The lead bonus uses the real matcher, not a substring search. A substring
    search awarded the bonus to `AI` because the letters `ai` appear inside a
    leading word like `Malaria`, which is the exact confusion this whole module
    is supposed to avoid.
    """
    if not topic.all_terms or not item.matched_keywords:
        return 0.0

    hits = len(set(item.matched_keywords))
    base = min(1.0, math.log1p(hits) / math.log1p(3))

    position = match_position(item.title, item.matched_keywords)
    if position is not None and position < lead_chars:
        base += lead_bonus
    return min(1.0, base)


def echo_score(item: Item, *, max_sources: int) -> float:
    """Bonus when 2+ distinct PLATFORMS carried the same link.

    Independent coverage is a real signal of significance, and it is also how a
    story that broke on X reaches this page at all, since we do not ingest X
    directly.

    `echo_platforms` only ever grows from URL-identical merges, never from fuzzy
    title matches. A guess must not become the evidence behind a badge that
    claims corroboration. Capped so three platforms is not three times as
    important as two.
    """
    n = len(item.echo_platforms)
    if n < 2 or max_sources < 2:
        return 0.0
    return min(1.0, (n - 1) / max(1, max_sources - 1))


def score_item(item: Item, topic: Topic, now: datetime, cfg: dict) -> float:
    rec = recency_score(item, now, float(cfg.get("recency_half_life_hours", 12.0)))
    kw = keyword_score(
        item,
        topic,
        lead_chars=int(cfg.get("title_lead_chars", 40)),
        lead_bonus=float(cfg.get("title_lead_bonus", 0.25)),
    )
    # Normalized around 1.0 so a neutral-weight source contributes nothing
    # either way, and the dial is intuitive to turn.
    src = max(0.0, min(1.0, item.source_weight / 2.0))
    echo = echo_score(item, max_sources=int(cfg.get("echo_max_sources", 3)))

    return (
        float(cfg.get("weight_recency", 1.0)) * rec
        + float(cfg.get("weight_keyword", 0.6)) * kw
        + float(cfg.get("weight_source", 0.4)) * src
        + float(cfg.get("weight_echo", 0.5)) * echo
    )


def rank_items(items: list[Item], topic: Topic, now: datetime, cfg: dict) -> list[Item]:
    """Highest score first. Ties broken by recency, then title, so runs are stable."""
    return sorted(
        items,
        key=lambda i: (-score_item(i, topic, now, cfg), -i.published_at.timestamp(), i.title),
    )
