"""Load and validate topics.yaml / sources.yaml.

Fails loudly, with messages aimed at a human editing a YAML file. A config typo
that silently produces a wrong page is the worst outcome for a tool that runs
unattended every hour.

The specific trap this guards: `keywords: AI` (a string, not a list) used to be
iterated character by character into `["A", "I"]`, which quietly turned the page
into a firehose. Every list field is now type-checked.

**Categories are the unit of curation.** A category owns two things that used to
be separate concerns: the KEYWORDS it matches on, and the CURATED FEEDS that are
native to it. Both live together in topics.yaml because they answer the same
question, "what belongs in this section", and splitting them across two files
made it impossible to see a section whole.

A feed listed under a category is a claim that the feed is SINGLE-SUBJECT: every
story it publishes belongs in that section. That claim buys the feed an
exemption from keyword matching, which is the whole point, because
"Vogtle 4 enters commercial operation" is obviously an energy story and contains
none of the energy keywords. Multi-subject feeds (a publication's front page)
belong in the shared `rss:` pool in sources.yaml, where keywords decide.

The older `topics:` key still loads, so a fork written against v1 keeps working.
It simply produces categories with no native feeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalize import safe_url


class ConfigError(Exception):
    """Raised with a message aimed at a human editing a YAML file."""


def slugify(name: str) -> str:
    """A stable id derived from a display name, so `id:` is optional."""
    out = "".join(c if c.isalnum() else "-" for c in str(name).casefold())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


@dataclass
class RssSource:
    id: str
    name: str
    url: str
    weight: float = 1.0
    is_aggregator: bool = False
    platform: str = ""
    # Empty for the shared pool. Set to a category id for a curated feed, which
    # is what lets its items join that category without a keyword hit.
    category: str = ""

    def __post_init__(self) -> None:
        if not self.platform:
            self.platform = self.id


@dataclass
class Category:
    """One section of the page: what it matches, and which feeds are native."""

    name: str
    keywords: list[str] = field(default_factory=list)
    # Stable short name, used for native-feed tagging. Derived from `name` when
    # omitted, so config and tests can both leave it out.
    id: str = ""
    aliases: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sources: list[RssSource] = field(default_factory=list)
    # Hacker News search terms. Deliberately SEPARATE from `keywords`: the
    # keyword list is a wide local filter and can hold thirty phrases, but every
    # HN term costs two API requests, so firing all of them would blow the
    # per-run request cap on the first category and silently starve the rest.
    # Falls back to the keyword list when unset, which is the v1 behaviour.
    hn_queries: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = slugify(self.name)

    @property
    def all_terms(self) -> list[str]:
        return list(self.keywords) + list(self.aliases)

    @property
    def search_terms(self) -> list[str]:
        return list(self.hn_queries) if self.hn_queries else self.all_terms


# v1 name. Kept so an existing import does not break.
Topic = Category


@dataclass
class Config:
    categories: list[Category]
    rss: list[RssSource]  # the shared pool only
    settings: dict[str, Any]
    ranking: dict[str, Any]
    dedup: dict[str, Any]
    hackernews: dict[str, Any]
    reddit: dict[str, Any]
    images: dict[str, Any] = field(default_factory=dict)
    # The `newsletter:` block from sources.yaml, passed to the lane as a plain
    # mapping. The lane owns its own defaults; config only validates types.
    newsletter: dict[str, Any] = field(default_factory=dict)

    @property
    def topics(self) -> list[Category]:
        """v1 alias. Categories are what topics grew into."""
        return self.categories

    @property
    def all_feeds(self) -> list[RssSource]:
        """Shared pool plus every category's curated feeds, in a stable order."""
        return list(self.rss) + [s for c in self.categories for s in c.sources]

    @property
    def max_age_hours(self) -> float:
        return _positive(self.settings.get("max_age_hours", 48), "max_age_hours")

    @property
    def timeout(self) -> float:
        return _positive(self.settings.get("request_timeout", 15), "request_timeout")

    @property
    def user_agent(self) -> str:
        return str(self.settings.get("user_agent") or "news-curator (+https://github.com/topics/news-curator)")

    @property
    def max_items_per_topic(self) -> int:
        return int(_positive(self.settings.get("max_items_per_topic", 30), "max_items_per_topic"))

    @property
    def fetch_workers(self) -> int:
        """Parallel feed fetches. Bounded: this is someone else's server."""
        return max(1, min(16, int(_positive(self.settings.get("fetch_workers", 8), "fetch_workers"))))

    @property
    def repo_url(self) -> str | None:
        raw = self.settings.get("repo_url")
        return safe_url(raw) if raw else None

    @property
    def site_name(self) -> str:
        return str(self.settings.get("site_name") or "News Curator")


def _number(value: Any, label: str) -> float:
    """Any finite number. Zero and negatives are allowed where they make sense."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{label}' must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"'{label}' must be a finite number, got {value!r}")
    return number


def _positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{label}' must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        raise ConfigError(f"'{label}' must be a finite positive number, got {value!r}")
    return number


def _str_list(value: Any, label: str, path: Path) -> list[str]:
    """A list of strings, or a loud error. Never a string silently iterated."""
    if value is None:
        return []
    if isinstance(value, str):
        raise ConfigError(
            f"{path.name}: '{label}' must be a LIST, not a single string. "
            f"Write it as:\n    {label}:\n      - {value}"
        )
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{path.name}: '{label}' must be a list, got {type(value).__name__}.")
    out = []
    for entry in value:
        if isinstance(entry, (dict, list, tuple)):
            raise ConfigError(f"{path.name}: '{label}' entries must be plain text, got {entry!r}.")
        text = str(entry).strip()
        if text:
            out.append(text)
    return out


def parse_rss_entry(entry: Any, i: int, label: str, path: Path, *, category: str = "") -> RssSource:
    """One feed row, from either the shared pool or a category's list."""
    if not isinstance(entry, dict):
        raise ConfigError(f"{path.name}: {label} entry #{i + 1} must be a mapping.")
    sid = str(entry.get("id") or "").strip()
    url = str(entry.get("url") or "").strip()
    if not sid or not url:
        raise ConfigError(f"{path.name}: {label} entry #{i + 1} needs both 'id' and 'url'.")
    if safe_url(url) is None:
        raise ConfigError(f"{path.name}: {label} '{sid}' url must be an absolute http(s) URL, got {url!r}.")
    return RssSource(
        id=sid,
        name=str(entry.get("name") or sid),
        url=url,
        weight=_positive(entry.get("weight", 1.0), f"{label} '{sid}' weight"),
        is_aggregator=bool(entry.get("aggregator", False)),
        platform=str(entry.get("platform") or sid),
        category=category,
    )


def _parse_categories(raw: dict, path: Path) -> list[Category]:
    """`categories:` if present, else the v1 `topics:` key."""
    entries = raw.get("categories")
    key = "categories"
    if entries is None:
        entries = raw.get("topics")
        key = "topics"
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ConfigError(f"{path.name}: '{key}' must be a list.")

    categories: list[Category] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path.name}: {key} #{i + 1} must be a mapping with a 'name'.")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ConfigError(f"{path.name}: {key} #{i + 1} is missing a 'name'.")

        cid = str(entry.get("id") or "").strip() or slugify(name)
        if not cid:
            raise ConfigError(f"{path.name}: category '{name}' produces an empty id; give it an explicit 'id'.")

        # `newsletters` belongs to the pipeline's pseudo-category (the tab the
        # newsletter lane renders into). A user category reusing the id or the
        # name would silently share its bucket: items would be duplicated, one
        # tab would swallow the other, and the lane's cap would replace the
        # category's. Reviewed and rejected loudly instead.
        if cid == "newsletters" or name.casefold() == "newsletters":
            raise ConfigError(
                f"{path.name}: category '{name}' uses the reserved name/id 'newsletters', "
                "which belongs to the newsletter lane's own tab. Pick another name or id."
            )

        keywords = _str_list(entry.get("keywords"), f"category '{name}' keywords", path)
        sources_raw = entry.get("sources")
        if sources_raw is not None and not isinstance(sources_raw, list):
            raise ConfigError(f"{path.name}: category '{name}' sources must be a list.")
        sources = [
            parse_rss_entry(row, j, f"category '{name}' sources", path, category=cid)
            for j, row in enumerate(sources_raw or [])
        ]

        # A category with neither keywords nor native feeds can never show
        # anything, which is a config bug rather than an empty section.
        if not keywords and not sources:
            raise ConfigError(
                f"{path.name}: category '{name}' has no keywords and no sources, "
                "so it can never match anything."
            )

        categories.append(
            Category(
                id=cid,
                name=name,
                keywords=keywords,
                aliases=_str_list(entry.get("aliases"), f"category '{name}' aliases", path),
                exclude=_str_list(entry.get("exclude"), f"category '{name}' exclude", path),
                sources=sources,
                hn_queries=_str_list(entry.get("hn_queries"), f"category '{name}' hn_queries", path),
            )
        )

    names = [c.name.casefold() for c in categories]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"{path.name}: duplicate category names: {', '.join(dupes)}")

    ids = [c.id for c in categories]
    dupe_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dupe_ids:
        raise ConfigError(f"{path.name}: duplicate category ids: {', '.join(dupe_ids)}. Set an explicit 'id'.")
    return categories


def load_topics(path: Path) -> list[Category]:
    if not path.exists():
        raise ConfigError(f"Missing {path}. Copy the example and edit it.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level.")
    return _parse_categories(raw, path)


load_categories = load_topics


def load_sources(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing {path}.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level.")

    entries = raw.get("rss") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path.name}: 'rss' must be a list.")
    rss = [parse_rss_entry(entry, i, "rss", path) for i, entry in enumerate(entries)]

    for key in ("settings", "ranking", "dedup", "hackernews", "reddit", "images", "newsletter"):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            raise ConfigError(f"{path.name}: '{key}' must be a mapping.")

    # Every editable number is checked HERE, at load time. Casting them later at
    # the point of use turns an ordinary YAML typo into either a crash in the
    # middle of a scheduled run or, worse, a silent coercion nobody notices.
    for section, keys in (
        ("ranking", ("recency_half_life_hours", "weight_recency", "weight_keyword",
                     "weight_source", "weight_echo", "echo_max_sources",
                     "title_lead_chars", "title_lead_bonus", "native_source_score")),
        ("dedup", ("title_similarity_threshold", "time_bucket_hours")),
        ("hackernews", ("weight", "min_points_ranked", "hits_per_page", "max_requests",
                        "budget_seconds")),
        ("reddit", ("weight", "request_delay_seconds")),
        ("images", ("max_bytes", "timeout", "max_fetches_per_run", "budget_seconds",
                    "workers", "retain_days", "retry_error_after_hours")),
        ("newsletter", ("max_items", "max_age_hours", "max_messages", "overlap_hours",
                        "request_timeout")),
    ):
        block = raw.get(section) or {}
        for key in keys:
            if key in block:
                _number(block[key], f"{section}.{key}")

    threshold = (raw.get("dedup") or {}).get("title_similarity_threshold")
    if threshold is not None and not 0 < float(threshold) <= 1:
        raise ConfigError(f"{path.name}: 'dedup.title_similarity_threshold' must be between 0 and 1.")

    for section in ("hackernews", "reddit", "images", "newsletter"):
        enabled = (raw.get(section) or {}).get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(f"{path.name}: '{section}.enabled' must be true or false.")

    adapters = (raw.get("newsletter") or {}).get("adapters")
    if adapters is not None and not isinstance(adapters, list):
        raise ConfigError(f"{path.name}: 'newsletter.adapters' must be a list.")

    subs = (raw.get("reddit") or {}).get("subreddits")
    if subs is not None and not isinstance(subs, list):
        raise ConfigError(f"{path.name}: 'reddit.subreddits' must be a list.")

    raw["_rss_objects"] = rss
    return raw


def load_config(root: Path) -> Config:
    categories = load_topics(root / "topics.yaml")
    src = load_sources(root / "sources.yaml")
    cfg = Config(
        categories=categories,
        rss=src["_rss_objects"],
        settings=src.get("settings") or {},
        ranking=src.get("ranking") or {},
        dedup=src.get("dedup") or {},
        hackernews=src.get("hackernews") or {},
        reddit=src.get("reddit") or {},
        images=src.get("images") or {},
        newsletter=src.get("newsletter") or {},
    )

    # Feed ids must be unique across BOTH files. A duplicate id is not cosmetic:
    # `platform` defaults to the id and drives the cross-source echo badge, so
    # two feeds sharing an id would quietly claim to corroborate each other.
    seen: dict[str, str] = {}
    for source in cfg.all_feeds:
        where = f"category '{source.category}'" if source.category else "sources.yaml rss"
        if source.id in seen:
            raise ConfigError(
                f"duplicate feed id '{source.id}': defined in {seen[source.id]} and in {where}. "
                "Feed ids must be unique across topics.yaml and sources.yaml."
            )
        seen[source.id] = where

    # Touch the validating accessors now, so a bad number fails at load time
    # rather than an hour later in the middle of a scheduled run.
    _ = (cfg.max_age_hours, cfg.timeout, cfg.max_items_per_topic, cfg.fetch_workers)
    return cfg
