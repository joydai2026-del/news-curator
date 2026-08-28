"""The accuracy gate.

A search API's idea of a match is not our idea of a match. Querying Hacker News
for `AI` returned, inside the top five results by score, a story about two
airport workers dying of malaria. Algolia does fuzzy and prefix matching, so the
remote query is treated as a candidate generator only. Every candidate is
re-checked here, locally, with whole-word matching, before it is allowed onto
the page.

Matching runs against the headline only. Feed summaries range from one sentence
to a full article dump, and matching against those produces weak matches nobody
can explain by looking at the page.

What this does NOT do, and the page says so: prove the story is genuinely ABOUT
the topic. It proves the term really appears in the headline. That is a weaker
claim, it is checkable by eye, and `exclude` exists for when it is not enough.
"""

from __future__ import annotations

import copy
import re
from functools import lru_cache

from .config import Topic
from .models import Item
from .normalize import fold_text


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive, whitespace-flexible phrase match.

    The boundaries are the entire point: `AI` must not match `malaria`, `said`,
    or `chain`.

    `\\b` alone is wrong at a non-word edge, because `C++` ends in `+` and `\\b`
    would demand a word character there. Requiring whitespace instead was also
    wrong: it broke `C++:` and `(.NET)`. Lookarounds for a WORD character in
    either direction handle both, so `C++` matches inside `C++: a retrospective`
    but `AI` still does not match `malaria`.
    """
    body = r"\s+".join(re.escape(part) for part in term.split())
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def find_match(title: str, term: str) -> re.Match[str] | None:
    """Where a term appears in a title, or None. Position drives the lead bonus."""
    if not title or not term:
        return None
    return _term_pattern(term).search(fold_text(title))


def matched_terms(title: str, terms: list[str]) -> list[str]:
    """Every term that genuinely appears in the title, in config order."""
    return [t for t in terms if find_match(title, t)]


def match_position(title: str, terms: list[str]) -> int | None:
    """Earliest character offset at which any of these terms really matches."""
    positions = [m.start() for t in terms if (m := find_match(title, t))]
    return min(positions) if positions else None


def topic_match(item: Item, topic: Topic) -> list[str] | None:
    """Keywords this item matched for this topic, or None if it does not belong.

    An exclude term vetoes the item for this topic even when a keyword hit. That
    escape hatch is why a keyword system beats an embedding system here: when a
    bad match shows up, the owner can see why and fix it in one line.
    """
    if matched_terms(item.title, topic.exclude):
        return None
    return matched_terms(item.title, topic.all_terms) or None


def assign_topics(items: list[Item], topics: list[Topic]) -> dict[str, list[Item]]:
    """Bucket items by topic. An item may legitimately appear under several.

    Each bucket gets its own copy so `matched_keywords` reflects the topic it is
    displayed under, not whichever topic happened to be checked last.
    """
    buckets: dict[str, list[Item]] = {t.name: [] for t in topics}
    for item in items:
        for topic in topics:
            hits = topic_match(item, topic)
            if hits:
                clone = copy.copy(item)
                clone.matched_keywords = hits
                clone.echo_platforms = set(item.echo_platforms)
                buckets[topic.name].append(clone)
    return buckets
