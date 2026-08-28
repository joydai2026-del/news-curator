"""Collapsing the same story into one row.

Two passes with deliberately different levels of trust, because review found
that treating them the same invented facts:

**Pass 1, canonical URL — certain.** Same link means same article. The surviving
row inherits provenance from everything that merged into it, and that provenance
is what the "N sources" badge on the page reports.

**Pass 2, title similarity — a guess.** It collapses a duplicate row, and that
is ALL it does. It never contributes to the echo badge. `SequenceMatcher` rates
"Apple releases iOS 18.6.1" against "...18.6.2" at 0.96, so a fuzzy match is not
evidence that two outlets covered one story. It is evidence that two headlines
look alike.

Numbers are also compared exactly before a fuzzy merge is allowed, because
version numbers, casualty counts, funding amounts and years are precisely the
characters that distinguish two real stories while barely moving a similarity
ratio.

**Aggregator preference.** Hacker News, Reddit and Lobsters carry
SUBMITTER-written titles pointing at someone else's article. When the publisher's
own feed gave us the same link, the publisher's title and name win. Otherwise the
page would show a stranger's paraphrase under the publisher's byline.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import Item
from .normalize import fold_text

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+")

# Kept short on purpose: an aggressive stopword list makes short headlines
# collide with each other.
_STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "with", "at", "by", "from", "as", "it", "its", "this",
    "that", "how", "why", "what", "new",
}


def normalize_title(title: str) -> str:
    text = _PUNCT.sub(" ", fold_text(title or "").casefold())
    return " ".join(w for w in _WS.sub(" ", text).split() if w not in _STOPWORDS)


def numbers_in(title: str) -> list[str]:
    return _NUM.findall(title or "")


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def same_story(a: Item, b: Item, threshold: float) -> bool:
    """Fuzzy 'these are the same article' test, with two guards.

    **Numeric guard.** Differing numbers veto the merge: `iOS 18.6.1` and
    `iOS 18.6.2` are two releases, and `raises $20M` and `raises $200M` are two
    funding rounds, even though both pairs score far above any threshold.

    **Threshold.** Default 0.90, raised from 0.85 after review found that
    `Apple releases iOS 18.6.1` and `Apple delays iOS 18.6.1` scored 0.875 and
    merged. Those are opposite stories about the same release, and the numeric
    guard cannot catch them because the numbers are identical. Character
    similarity simply cannot tell "releases" from "delays", so the threshold has
    to sit above where a single verb swap lands.

    The cost of being wrong is asymmetric: a missed merge shows one extra row,
    a wrong merge silently deletes a story and misattributes the survivor.
    """
    if numbers_in(a.title) != numbers_in(b.title):
        return False
    return title_similarity(a.title, b.title) >= threshold


def _merge(keep: Item, drop: Item, *, count_echo: bool) -> None:
    """Fold `drop` into `keep`.

    `count_echo` is False for fuzzy merges, so a guess can never inflate the
    corroboration badge.
    """
    if count_echo:
        keep.echo_platforms |= drop.echo_platforms
    if drop.score is not None:
        keep.score = drop.score if keep.score is None else max(keep.score, drop.score)
    # Earliest known publish time is the truest one: a syndicated copy is later
    # than the original, and an aggregator's timestamp is when IT saw the story.
    # A merged-in exact time also beats a kept estimated one.
    if drop.published_at < keep.published_at and not (drop.time_is_estimated and not keep.time_is_estimated):
        keep.published_at = drop.published_at
        keep.time_is_estimated = drop.time_is_estimated


def _preference(item: Item) -> tuple:
    """Sort key deciding which copy of a story survives a merge.

    Publishers before aggregators is the load-bearing part: it is what stops a
    submitter's paraphrase being displayed as the publisher's headline.
    """
    return (
        item.is_aggregator,  # False (publisher) sorts before True (aggregator)
        -item.source_weight,
        -(item.score or 0),
        item.published_at,
    )


def dedupe(items: list[Item], *, threshold: float = 0.90, time_bucket_hours: float = 36.0) -> list[Item]:
    ordered = sorted(items, key=_preference)

    # Pass 1: identical link. Certain, and the only source of echo provenance.
    by_url: dict[str, Item] = {}
    for item in ordered:
        key = item.canonical_url
        if not key:
            continue
        if key in by_url:
            _merge(by_url[key], item, count_echo=True)
        else:
            by_url[key] = item

    # Pass 2: similar title. Collapses a row, contributes nothing to the badge.
    survivors: list[Item] = []
    for item in by_url.values():
        match = None
        for kept in survivors:
            # Scope by time so this stays cheap and so two genuinely different
            # stories months apart can never collide.
            gap = abs((kept.published_at - item.published_at).total_seconds()) / 3600.0
            if gap > time_bucket_hours:
                continue
            if same_story(kept, item, threshold):
                match = kept
                break
        if match is not None:
            _merge(match, item, count_echo=False)
        else:
            survivors.append(item)
    return survivors
