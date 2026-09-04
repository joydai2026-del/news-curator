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
itself or as `og:image` on the article. It is retained as metadata, never
rehosted or invented: an item with no declared image keeps this empty and the
renderer decides what to do about that.

v2 adds four fields, and every one of them is a place a machine could have been
tempted to write prose. None of them is:

`description` — the summary the SOURCE wrote, cleaned the same way the title is
and nothing more. Not a generated abstract, not a first paragraph scraped from
the article, not an LLM's idea of the gist. A source that shipped no summary
leaves this empty and the card renders without one, which is the honest answer
and looks fine.

`cluster` — other addresses for the SAME story, gathered when dedup collapsed
two rows that pointed at different URLs. It is what lets an unfolded card say
"also covered by" and name them. Bounded and unique, because it is display data
and an unbounded list of near-identical links is noise, not evidence.

`is_newsletter` / `newsletter_sender` — identity for the newsletter lane. They
are declared here, and honoured by the renderer and the image enricher, before
anything fetches mail: the privacy rules that hang off them (never load an
image, never touch the image cache, never render a link we could not clean) are
much easier to get right as a property of the model than as a special case
bolted onto a lane later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re


_STORY_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_MODEL_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


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
    image_url: str = ""  # publisher-declared preview image metadata, may be empty
    description: str = ""  # the SOURCE's own summary, cleaned. Never generated.
    # Language is declared by source configuration, not guessed from a title.
    # Legacy artifacts and source rows are English unless they opt into Chinese.
    language: str = "en"
    # Some aggregator links are useful discovery paths but are not independent
    # corroboration. Google News and buzzing.cc use this boundary.
    echo_eligible: bool = True
    # Source-local ordering metadata for Trending. Values are comparable only
    # inside one source, never across HN points and RSS feed positions.
    native_rank: int | None = None

    # Newsletter-lane identity. Set by the newsletter fetcher, honoured by the
    # renderer (no image, "via <sender>", unlinked headline when no clean URL
    # could be recovered) and by the image enricher (never fetched, never
    # cached).
    is_newsletter: bool = False
    newsletter_sender: str = ""

    # Filled in downstream.
    echo_platforms: set[str] = field(default_factory=set)
    native_categories: set[str] = field(default_factory=set)
    matched_keywords: list[str] = field(default_factory=list)
    # Alternate addresses for this same story, one dict per merged-away copy:
    # {"source_name": ..., "url": ...}. Grown only by dedup, bounded there.
    cluster: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.platform:
            self.platform = self.source_id
        if not self.echo_platforms and self.echo_eligible:
            self.echo_platforms = {self.platform}

    def age_hours(self, now: datetime) -> float:
        return max(0.0, (now - self.published_at).total_seconds() / 3600.0)

    def day_bucket(self, now: datetime) -> str:
        """Return a display-neutral local-day bucket without mutating the item."""
        zone = now.tzinfo
        published_day = self.published_at.astimezone(zone).date() if zone else self.published_at.date()
        delta = now.date() - published_day
        if delta <= timedelta(0):
            return "today"
        if delta == timedelta(days=1):
            return "yesterday"
        return "older"


@dataclass(frozen=True)
class TranslationRecord:
    """A separately stored localized projection, never an authoritative story."""

    story_id: str
    input_digest: str
    source_language: str
    target_language: str
    title: str
    description: str
    provider: str
    model_version: str

    def __post_init__(self) -> None:
        if not _STORY_ID.fullmatch(self.story_id):
            raise ValueError("translation story id is invalid")
        if not _DIGEST.fullmatch(self.input_digest):
            raise ValueError("translation input digest is invalid")
        if (
            self.source_language not in {"en", "zh"}
            or self.target_language not in {"en", "zh"}
            or self.source_language == self.target_language
        ):
            raise ValueError("translation language pair is invalid")
        if not self.title or len(self.title) > 2_000 or len(self.description) > 8_000:
            raise ValueError("translation text is invalid")
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in self.title + self.description):
            raise ValueError("translation text contains control characters")
        if not _VERSION_ID.fullmatch(self.provider) or not _MODEL_RESOURCE_ID.fullmatch(self.model_version):
            raise ValueError("translation provider metadata is invalid")


@dataclass(frozen=True)
class LocalizedItem:
    """Display text layered over one original, already ranked Item."""

    story_id: str
    original: Item
    display_language: str
    title: str
    description: str
    translated: bool = False
    translation_provider: str = ""
    translation_model_version: str = ""
    translation_available: bool = False
    translation_source_language: str = ""

    def __post_init__(self) -> None:
        if not _STORY_ID.fullmatch(self.story_id):
            raise ValueError("localized story id is invalid")
        if self.display_language not in {"en", "zh"} or not self.title:
            raise ValueError("localized display fields are invalid")
        if not self.translated and self.display_language != self.original.language:
            raise ValueError("native localized item language must match its original")
        if self.translation_available:
            if not self.translation_provider or not self.translation_model_version:
                raise ValueError("translation provenance is incomplete")
            if self.translation_source_language not in {"en", "zh"}:
                raise ValueError("translation source provenance is invalid")
        elif self.translation_provider or self.translation_model_version or self.translation_source_language:
            raise ValueError("translation provenance must be explicit")


@dataclass(frozen=True)
class SourceHealth:
    """Safe structured health for one configured source.

    It intentionally carries no URL and no raw exception text. A fork may put
    credentials in a source URL, and Actions summaries are user-visible logs.
    """

    source_id: str
    status: str
    usable_items: int
    newest_at: datetime | None
    age_hours: float | None
    max_age_hours: float
    language: str = "en"
    source_type: str = "rss"
    echo_eligible: bool = True
    reason_code: str = ""


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
    source_health: list[SourceHealth] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.note) or not self.ok
