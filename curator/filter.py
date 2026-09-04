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

**Two ways to belong to a category, and they are different claims.**

  * A KEYWORD hit proves the term really appears in the headline. It does not
    prove the story is about the topic. That is the weaker claim, it is
    checkable by eye, and `exclude` exists for when it is not enough.
  * A NATIVE feed is the editor's claim, made once in config, that a
    single-subject publication belongs in a section. It is what puts "Vogtle 4
    enters commercial operation" in the energy section, a story that is
    unmistakably energy news and contains not one energy keyword. Keyword
    matching alone systematically loses exactly the stories a curated feed
    exists to supply.

`exclude` vetoes both. A native feed is a strong claim, not an unconditional
one, so the escape hatch still works on it.
"""

from __future__ import annotations

import copy
import re
from functools import lru_cache

from .config import Category
from .models import Item
from .normalize import fold_text


@lru_cache(maxsize=4096)
def _term_pattern(term: str, language: str = "en") -> re.Pattern[str]:
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
    if language == "zh":
        # CJK characters are Unicode word characters, so English word
        # boundaries make a phrase such as 人工智能 fail inside 生成式人工智能.
        # A literal Unicode substring is the intended Chinese contract.
        return re.compile(body, re.IGNORECASE)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def find_match(title: str, term: str, *, language: str = "en") -> re.Match[str] | None:
    """Where a term appears in a title, or None. Position drives the lead bonus."""
    if not title or not term:
        return None
    return _term_pattern(term, language).search(fold_text(title))


def matched_terms(title: str, terms: list[str], *, language: str = "en") -> list[str]:
    """Every term that genuinely appears in the title, in config order."""
    return [t for t in terms if find_match(title, t, language=language)]


def match_position(title: str, terms: list[str], *, language: str = "en") -> int | None:
    """Earliest character offset at which any of these terms really matches."""
    positions = [
        m.start()
        for t in terms
        if (m := find_match(title, t, language=language))
    ]
    return min(positions) if positions else None


def is_native(item: Item, category: Category) -> bool:
    """Did one of this category's own curated feeds carry this link?"""
    return category.id in item.native_categories


def topic_match(item: Item, category: Category) -> list[str] | None:
    """Keywords this item matched, or None if it does not belong here at all.

    An empty LIST means "belongs, on the strength of its source, with no keyword
    hit". `None` means "does not belong". Those are different answers and the
    caller must not conflate them, which is why this returns a list-or-None
    rather than a bool.
    """
    if matched_terms(item.title, category.exclude, language=item.language):
        return None
    hits = matched_terms(
        item.title,
        category.terms_for(item.language),
        language=item.language,
    )
    if hits:
        return hits
    return [] if is_native(item, category) else None


def assign_categories(items: list[Item], categories: list[Category]) -> dict[str, list[Item]]:
    """Bucket items by category. An item may legitimately appear under several.

    Each bucket gets its own copy so `matched_keywords` reflects the category it
    is displayed under, not whichever one happened to be checked last.
    """
    buckets: dict[str, list[Item]] = {c.name: [] for c in categories}
    for item in items:
        for category in categories:
            hits = topic_match(item, category)
            if hits is None:
                continue
            clone = copy.copy(item)
            clone.matched_keywords = hits
            clone.echo_platforms = set(item.echo_platforms)
            clone.native_categories = set(item.native_categories)
            # Same reason as the two sets above: `copy.copy` shares mutable
            # fields, and a shared list is one downstream mutation away from
            # cross-category contamination.
            clone.cluster = list(item.cluster)
            buckets[category.name].append(clone)
    return buckets


# v1 name.
assign_topics = assign_categories
