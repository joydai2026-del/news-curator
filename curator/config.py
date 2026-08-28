"""Load and validate topics.yaml / sources.yaml.

Fails loudly, with messages aimed at a human editing a YAML file. A config typo
that silently produces a wrong page is the worst outcome for a tool that runs
unattended every hour.

The specific trap this guards: `keywords: AI` (a string, not a list) used to be
iterated character by character into `["A", "I"]`, which quietly turned the page
into a firehose. Every list field is now type-checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalize import safe_url


class ConfigError(Exception):
    """Raised with a message aimed at a human editing a YAML file."""


@dataclass
class Topic:
    name: str
    keywords: list[str]
    aliases: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @property
    def all_terms(self) -> list[str]:
        return list(self.keywords) + list(self.aliases)


@dataclass
class RssSource:
    id: str
    name: str
    url: str
    weight: float = 1.0
    is_aggregator: bool = False
    platform: str = ""

    def __post_init__(self) -> None:
        if not self.platform:
            self.platform = self.id


@dataclass
class Config:
    topics: list[Topic]
    rss: list[RssSource]
    settings: dict[str, Any]
    ranking: dict[str, Any]
    dedup: dict[str, Any]
    hackernews: dict[str, Any]
    reddit: dict[str, Any]

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


def load_topics(path: Path) -> list[Topic]:
    if not path.exists():
        raise ConfigError(f"Missing {path}. Copy the example and edit it.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level.")

    entries = raw.get("topics")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ConfigError(f"{path.name}: 'topics' must be a list.")

    topics: list[Topic] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path.name}: topic #{i + 1} must be a mapping with a 'name'.")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ConfigError(f"{path.name}: topic #{i + 1} is missing a 'name'.")
        keywords = _str_list(entry.get("keywords"), f"topic '{name}' keywords", path)
        if not keywords:
            raise ConfigError(f"{path.name}: topic '{name}' has no keywords, so it can never match anything.")
        topics.append(
            Topic(
                name=name,
                keywords=keywords,
                aliases=_str_list(entry.get("aliases"), f"topic '{name}' aliases", path),
                exclude=_str_list(entry.get("exclude"), f"topic '{name}' exclude", path),
            )
        )

    names = [t.name.casefold() for t in topics]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"{path.name}: duplicate topic names: {', '.join(dupes)}")
    return topics


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

    rss: list[RssSource] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path.name}: rss entry #{i + 1} must be a mapping.")
        sid = str(entry.get("id") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not sid or not url:
            raise ConfigError(f"{path.name}: rss entry #{i + 1} needs both 'id' and 'url'.")
        if safe_url(url) is None:
            raise ConfigError(f"{path.name}: rss '{sid}' url must be an absolute http(s) URL, got {url!r}.")
        if sid in seen:
            raise ConfigError(f"{path.name}: duplicate rss id '{sid}'.")
        seen.add(sid)
        rss.append(
            RssSource(
                id=sid,
                name=str(entry.get("name") or sid),
                url=url,
                weight=_positive(entry.get("weight", 1.0), f"rss '{sid}' weight"),
                is_aggregator=bool(entry.get("aggregator", False)),
                platform=str(entry.get("platform") or sid),
            )
        )

    for key in ("settings", "ranking", "dedup", "hackernews", "reddit"):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            raise ConfigError(f"{path.name}: '{key}' must be a mapping.")

    # Every editable number is checked HERE, at load time. Casting them later at
    # the point of use turns an ordinary YAML typo into either a crash in the
    # middle of a scheduled run or, worse, a silent coercion nobody notices.
    for section, keys in (
        ("ranking", ("recency_half_life_hours", "weight_recency", "weight_keyword",
                     "weight_source", "weight_echo", "echo_max_sources",
                     "title_lead_chars", "title_lead_bonus")),
        ("dedup", ("title_similarity_threshold", "time_bucket_hours")),
        ("hackernews", ("weight", "min_points_ranked", "hits_per_page", "max_requests")),
        ("reddit", ("weight", "request_delay_seconds")),
    ):
        block = raw.get(section) or {}
        for key in keys:
            if key in block:
                _number(block[key], f"{section}.{key}")

    threshold = (raw.get("dedup") or {}).get("title_similarity_threshold")
    if threshold is not None and not 0 < float(threshold) <= 1:
        raise ConfigError(f"{path.name}: 'dedup.title_similarity_threshold' must be between 0 and 1.")

    for section in ("hackernews", "reddit"):
        enabled = (raw.get(section) or {}).get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(f"{path.name}: '{section}.enabled' must be true or false.")

    subs = (raw.get("reddit") or {}).get("subreddits")
    if subs is not None and not isinstance(subs, list):
        raise ConfigError(f"{path.name}: 'reddit.subreddits' must be a list.")

    raw["_rss_objects"] = rss
    return raw


def load_config(root: Path) -> Config:
    topics = load_topics(root / "topics.yaml")
    src = load_sources(root / "sources.yaml")
    cfg = Config(
        topics=topics,
        rss=src["_rss_objects"],
        settings=src.get("settings") or {},
        ranking=src.get("ranking") or {},
        dedup=src.get("dedup") or {},
        hackernews=src.get("hackernews") or {},
        reddit=src.get("reddit") or {},
    )
    # Touch the validating accessors now, so a bad number fails at load time
    # rather than an hour later in the middle of a scheduled run.
    _ = (cfg.max_age_hours, cfg.timeout, cfg.max_items_per_topic)
    return cfg
