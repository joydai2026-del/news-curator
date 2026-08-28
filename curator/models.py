"""The one record every fetcher emits, whatever tier it came from.

Fetchers do no filtering and no ranking. They fetch and normalize. Everything
downstream is source-agnostic, which is what makes adding a fourth tier later a
contained change instead of a rewrite.

Two fields exist because of review findings and deserve explaining:

`is_aggregator` — Hacker News, Reddit and Lobsters carry SUBMITTER-written
titles pointing at someone else's article. The publisher's own feed carries the
real title for the same link. When both arrive, the publisher wins, otherwise we
would show a stranger's paraphrase and attribute it to the publisher's URL.

`echo_platforms` — only ever grows from URL-IDENTICAL merges. A fuzzy title
match is a guess, and a guess must never become the evidence behind a "3
sources" badge on the page.

`native_categories` — which curated category feeds carried this link. A feed
listed under a category is a claim that the feed is single-subject, so its items
join that category without needing a keyword hit. Like `echo_platforms`, this
only ever grows from URL-identical merges: filing a story under a section on the
strength of a fuzzy title guess is exactly the kind of confident wrongness this
codebase avoids.

`image_url` — the preview image the PUBLISHER declared, either in the feed
itself or as `og:image` on the article. It is hotlinked, never rehosted, and
never invented: an item with no declared image keeps this empty and the renderer
decides what to do about that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    title: str  # display-faithful, as the publisher wrote it
    url: str  # already validated http(s) by normalize.safe_url
    canonical_url: str
    source_id: str  # e.g. "reddit:technology", "hackernews", "verge"
    source_name: str  # e.g. "r/technology", "Hacker News", "The Verge"
    published_at: datetime  # always timezone-aware UTC
    platform: str = ""  # provenance bucket; HN API and HN RSS share one
    source_weight: float = 1.0
    score: int | None = None  # native popularity, if the source has one
    is_aggregator: bool = False
    time_is_estimated: bool = False  # True when only an "updated" time existed
    image_url: str = ""  # publisher-declared preview image, hotlinked, may be empty

    # Filled in downstream.
    echo_platforms: set[str] = field(default_factory=set)
    native_categories: set[str] = field(default_factory=set)
    matched_keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.platform:
            self.platform = self.source_id
        if not self.echo_platforms:
            self.echo_platforms = {self.platform}

    def age_hours(self, now: datetime) -> float:
        return max(0.0, (now - self.published_at).total_seconds() / 3600.0)


@dataclass
class TierResult:
    """What one fetch tier produced, including how it degraded.

    `ok` and `items` are independent on purpose. A tier that returned ten items
    and THEN got rate-limited is a partial success, and the page must say so
    rather than showing a reassuring "reddit: 10".
    """

    tier: str
    items: list[Item] = field(default_factory=list)
    ok: bool = True
    note: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.note) or not self.ok
