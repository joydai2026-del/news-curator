"""Build useful summaries from text the publisher already supplied.

This is deterministic enrichment, not an LLM feature. A feed summary is used
when it is already useful. Otherwise the bounded, SSRF-safe article fetch reads
publisher metadata and lead paragraphs, then assembles distinct source
sentences. If the result still misses the configured quality floor, callers
clear the description and the renderer omits that story.
"""

from __future__ import annotations

import concurrent.futures as futures
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from .models import Item
from .sources import SafeHttpPolicy, SafeHttpTransport, SafeTransportError


log = logging.getLogger(__name__)

SUMMARY_CACHE_FILE = "summary_cache.json"
CACHE_VERSION = 1
MAX_REDIRECTS = 4

DEFAULTS = {
    "enabled": False,
    "minimum_characters": 180,
    "minimum_sentences": 3,
    "target_characters": 320,
    "maximum_characters": 600,
    "max_bytes": 524288,
    "timeout": 10.0,
    "max_fetches_per_run": 160,
    "budget_seconds": 90.0,
    "workers": 8,
    "retain_days": 7,
    "retry_error_after_hours": 6.0,
}

_META_KEYS = ("description", "og:description", "twitter:description")
# English prose normally separates sentences with whitespace. Chinese and
# Japanese prose normally do not, so their full-width terminal punctuation is
# itself a safe split boundary. Keeping the ASCII full stop whitespace-bound
# avoids splitting decimal numbers and dotted identifiers.
_SENTENCE_END = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+")
_SPACE = re.compile(r"\s+")
_CJK = re.compile(r"[\u3400-\u9fff]")
_BOILERPLATE = (
    "accept cookies",
    "all rights reserved",
    "cookie policy",
    "enable javascript",
    "sign up for",
    "subscribe now",
    "we use cookies",
)


def _clean(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = _SPACE.sub(" ", text).strip()
    return "" if any(text.casefold().startswith(prefix) for prefix in _BOILERPLATE) else text


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(_clean(text)) if part.strip()]


def _sentence_count(text: str) -> int:
    return len([part for part in _sentences(text) if re.search(r"[.!?。！？]$", part)])


def _substantive_sentence(text: str) -> bool:
    """Reject snippets while respecting the greater density of CJK prose."""
    return len(text) >= 35 or len(_CJK.findall(text)) >= 18


def _substantive_paragraph(text: str) -> bool:
    return len(text) >= 45 or len(_CJK.findall(text)) >= 22


def summary_is_usable(
    text: object,
    *,
    minimum_characters: int = int(DEFAULTS["minimum_characters"]),
    minimum_sentences: int = int(DEFAULTS["minimum_sentences"]),
) -> bool:
    cleaned = _clean(text)
    if len(cleaned) < minimum_characters:
        return False
    if any(ord(char) < 32 and char not in "\t\n\r" for char in cleaned):
        return False
    return _sentence_count(cleaned) >= minimum_sentences


def _clip(text: str, maximum_characters: int) -> str:
    if len(text) <= maximum_characters:
        return text
    cut = text[: maximum_characters - 1].rstrip()
    boundary = max(
        cut.rfind("."), cut.rfind("?"), cut.rfind("!"),
        cut.rfind("。"), cut.rfind("！"), cut.rfind("？"),
    )
    if boundary >= maximum_characters // 2:
        return cut[: boundary + 1]
    word = cut.rfind(" ")
    return (cut[:word] if word >= maximum_characters // 2 else cut).rstrip(".,;:!? ") + "…"


def compose_summary(
    existing: str,
    metadata: list[str],
    paragraphs: list[str],
    *,
    minimum_characters: int = int(DEFAULTS["minimum_characters"]),
    minimum_sentences: int = int(DEFAULTS["minimum_sentences"]),
    target_characters: int = int(DEFAULTS["target_characters"]),
    maximum_characters: int = int(DEFAULTS["maximum_characters"]),
) -> str:
    """Assemble distinct publisher sentences without generating new prose."""
    chosen: list[str] = []
    fingerprints: list[str] = []
    for candidate in [existing, *metadata, *paragraphs]:
        for sentence in _sentences(candidate):
            if not _substantive_sentence(sentence):
                continue
            # `str.isalnum` is Unicode-aware. An ASCII-only regex turns an
            # ordinary Chinese sentence into an empty fingerprint and silently
            # discards it from enrichment.
            fingerprint = "".join(char for char in sentence.casefold() if char.isalnum())
            if not fingerprint:
                continue
            if any(fingerprint in seen or seen in fingerprint for seen in fingerprints):
                continue
            chosen.append(sentence)
            fingerprints.append(fingerprint)
            assembled = " ".join(chosen)
            if len(assembled) >= target_characters and _sentence_count(assembled) >= minimum_sentences:
                clipped = _clip(assembled, maximum_characters)
                return clipped if summary_is_usable(
                    clipped,
                    minimum_characters=minimum_characters,
                    minimum_sentences=minimum_sentences,
                ) else ""
    assembled = _clip(" ".join(chosen), maximum_characters)
    return assembled if summary_is_usable(
        assembled,
        minimum_characters=minimum_characters,
        minimum_sentences=minimum_sentences,
    ) else ""


class _ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[str] = []
        self.article_paragraphs: list[str] = []
        self.body_paragraphs: list[str] = []
        self._article_depth = 0
        self._main_depth = 0
        self._body_depth = 0
        self._skip: list[str] = []
        self._paragraph: list[str] | None = None
        self._paragraph_is_article = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "meta":
            row = {key.casefold(): (value or "") for key, value in attrs}
            key = (row.get("property") or row.get("name") or "").strip().casefold()
            value = _clean(row.get("content"))
            if key in _META_KEYS and value and value not in self.meta:
                self.meta.append(value)
            return
        if tag == "body":
            self._body_depth += 1
        elif tag == "article":
            self._article_depth += 1
        elif tag == "main":
            self._main_depth += 1
        if tag in {"script", "style", "nav", "footer", "aside", "form", "noscript", "svg"}:
            self._skip.append(tag)
        if tag == "p" and self._body_depth and not self._skip:
            self._paragraph = []
            self._paragraph_is_article = bool(self._article_depth or self._main_depth)

    def handle_data(self, data: str) -> None:
        if self._paragraph is not None and not self._skip:
            self._paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "p" and self._paragraph is not None:
            text = _clean(" ".join(self._paragraph))
            if _substantive_paragraph(text):
                target = self.article_paragraphs if self._paragraph_is_article else self.body_paragraphs
                if text not in target:
                    target.append(text)
            self._paragraph = None
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        elif tag == "main" and self._main_depth:
            self._main_depth -= 1
        elif tag == "body" and self._body_depth:
            self._body_depth -= 1


def parse_article_text(markup: str) -> tuple[list[str], list[str]]:
    if not markup:
        return [], []
    parser = _ArticleTextParser()
    try:
        parser.feed(markup)
    except Exception:
        log.debug("article summary parser rejected malformed markup")
    paragraphs = parser.article_paragraphs or parser.body_paragraphs
    return parser.meta, paragraphs


def fetch_summary(
    url: str,
    *,
    existing: str,
    user_agent: str,
    timeout: float,
    max_bytes: int,
    minimum_characters: int,
    minimum_sentences: int,
    target_characters: int,
    maximum_characters: int,
    language: str = "en",
    transport: SafeHttpTransport | None = None,
) -> tuple[str | None, str]:
    selected = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=timeout,
            max_wire_bytes=max_bytes,
            max_decoded_bytes=max_bytes,
            max_redirects=MAX_REDIRECTS,
            read_chunk_bytes=min(16_384, max_bytes),
        )
    )
    try:
        accepted_language = "zh-CN,zh;q=0.9,en;q=0.5" if language == "zh" else "en"
        response = selected.get(
            "summary-meta",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": accepted_language,
            },
            allow_truncated_response=True,
            user_agent=user_agent,
        )
    except SafeTransportError as exc:
        log.debug("summary fetch failed with %s", exc.reason_code)
        return None, "error"
    except Exception:
        log.debug("summary fetch failed")
        return None, "error"
    if response.status_code != 200:
        return None, "error"
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
    if content_type and not (
        content_type.endswith("/html") or content_type.endswith("+xml") or content_type == "text/plain"
    ):
        return None, "none"
    markup = response.body[:max_bytes].decode("utf-8", errors="replace")
    metadata, paragraphs = parse_article_text(markup)
    summary = compose_summary(
        existing,
        metadata,
        paragraphs,
        minimum_characters=minimum_characters,
        minimum_sentences=minimum_sentences,
        target_characters=target_characters,
        maximum_characters=maximum_characters,
    )
    if summary:
        return summary, "ok"
    return None, "error" if response.body_truncated else "none"


class SummaryCache:
    def __init__(self, path: Path | None, entries: dict | None = None) -> None:
        self.path = path
        self.entries: dict[str, dict] = entries or {}
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> SummaryCache:
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("summary cache unreadable, starting empty")
            return cls(path)
        entries = raw.get("entries") if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION else None
        return cls(path, entries if isinstance(entries, dict) else {})

    def get(
        self,
        key: str,
        now: datetime,
        *,
        retry_error_after_hours: float,
        minimum_characters: int = int(DEFAULTS["minimum_characters"]),
        minimum_sentences: int = int(DEFAULTS["minimum_sentences"]),
    ) -> tuple[bool, str]:
        row = self.entries.get(key)
        if not isinstance(row, dict):
            return False, ""
        outcome = str(row.get("outcome") or "")
        checked = _parse_time(row.get("checked_at"))
        if outcome in {"error", "none"} and (
            checked is None or now - checked >= timedelta(hours=retry_error_after_hours)
        ):
            return False, ""
        summary = row.get("summary")
        if outcome == "ok":
            if not isinstance(summary, str) or not summary_is_usable(
                summary,
                minimum_characters=minimum_characters,
                minimum_sentences=minimum_sentences,
            ):
                return False, ""
            return True, _clean(summary)
        return True, ""

    def put(self, key: str, summary: str | None, outcome: str, now: datetime) -> None:
        stamp = now.replace(microsecond=0).isoformat()
        self.entries[key] = {
            "summary": summary or None,
            "outcome": outcome,
            "checked_at": stamp,
            "seen_at": stamp,
        }
        self._dirty = True

    def touch(self, key: str, now: datetime) -> None:
        row = self.entries.get(key)
        if isinstance(row, dict):
            stamp = now.replace(microsecond=0).isoformat()
            if row.get("seen_at") != stamp:
                row["seen_at"] = stamp
                self._dirty = True

    def prune(self, now: datetime, *, retain_days: float) -> int:
        cutoff = now - timedelta(days=retain_days)
        stale = [
            key for key, row in self.entries.items()
            if not isinstance(row, dict)
            or (
                _parse_time(row.get("seen_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ) < cutoff
        ]
        for key in stale:
            del self.entries[key]
        self._dirty = self._dirty or bool(stale)
        return len(stale)

    def save(self) -> bool:
        if not self._dirty or self.path is None:
            return False
        payload = {
            "version": CACHE_VERSION,
            "note": "Bounded publisher-supplied summaries. No full article body is retained.",
            "entries": dict(sorted(self.entries.items())),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        temporary.replace(self.path)
        self._dirty = False
        return True


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _setting(config: dict, key: str):
    value = config.get(key, DEFAULTS[key])
    return DEFAULTS[key] if value is None else value


def enrich(
    items: list[Item],
    cache: SummaryCache,
    now: datetime,
    *,
    user_agent: str,
    config: dict | None = None,
    transport: SafeHttpTransport | None = None,
) -> dict[str, int]:
    cfg = config or {}
    stats = {
        "total": len(items), "from_feed": 0, "from_cache": 0, "fetched": 0,
        "unusable": 0, "errors": 0, "capped": 0, "budget_hit": 0,
        "newsletter_skipped": 0,
    }
    if not cfg:
        return stats

    minimum_characters = int(_setting(cfg, "minimum_characters"))
    minimum_sentences = int(_setting(cfg, "minimum_sentences"))
    target_characters = int(_setting(cfg, "target_characters"))
    maximum_characters = int(_setting(cfg, "maximum_characters"))
    retry_after = float(_setting(cfg, "retry_error_after_hours"))
    if not bool(_setting(cfg, "enabled")):
        return stats
    pending: dict[str, list[Item]] = {}

    for item in items:
        if item.is_newsletter:
            stats["newsletter_skipped"] += 1
            if summary_is_usable(
                item.description,
                minimum_characters=minimum_characters,
                minimum_sentences=minimum_sentences,
            ):
                stats["from_feed"] += 1
            else:
                item.description = ""
                stats["unusable"] += 1
            continue
        if not item.canonical_url:
            item.description = ""
            stats["unusable"] += 1
            continue
        # One publisher URL can legitimately have negotiated English and
        # Chinese variants. Keep the language in both the work key and cache
        # key so text can never cross native-language projections.
        pending.setdefault(f"{item.language}:{item.canonical_url}", []).append(item)

    todo: list[str] = []
    for key, group in pending.items():
        best = max((_clean(item.description) for item in group), key=len, default="")
        if summary_is_usable(best, minimum_characters=minimum_characters, minimum_sentences=minimum_sentences):
            for item in group:
                item.description = best
            stats["from_feed"] += 1
            continue
        cache.touch(key, now)
        hit, summary = cache.get(
            key,
            now,
            retry_error_after_hours=retry_after,
            minimum_characters=minimum_characters,
            minimum_sentences=minimum_sentences,
        )
        if hit:
            if summary:
                for item in group:
                    item.description = summary
                stats["from_cache"] += 1
            else:
                for item in group:
                    item.description = ""
                stats["unusable"] += 1
            continue
        todo.append(key)

    maximum_fetches = int(_setting(cfg, "max_fetches_per_run"))
    if len(todo) > maximum_fetches:
        stats["capped"] = len(todo) - maximum_fetches
        deferred = todo[maximum_fetches:]
        for key in deferred:
            for item in pending[key]:
                item.description = ""
            stats["unusable"] += 1
        todo = todo[:maximum_fetches]
    if not todo:
        return stats

    timeout = float(_setting(cfg, "timeout"))
    max_bytes = int(_setting(cfg, "max_bytes"))
    budget = float(_setting(cfg, "budget_seconds"))
    workers = max(1, min(16, int(_setting(cfg, "workers"))))
    selected_transport = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=timeout,
            max_wire_bytes=max_bytes,
            max_decoded_bytes=max_bytes,
            per_host_concurrency=workers,
            read_chunk_bytes=min(16_384, max_bytes),
        )
    )

    def work(key: str) -> tuple[str, str | None, str]:
        group = pending[key]
        existing = max((_clean(item.description) for item in group), key=len, default="")
        return (
            key,
            *fetch_summary(
                group[0].url,
                existing=existing,
                user_agent=user_agent,
                timeout=timeout,
                max_bytes=max_bytes,
                minimum_characters=minimum_characters,
                minimum_sentences=minimum_sentences,
                target_characters=target_characters,
                maximum_characters=maximum_characters,
                language=group[0].language,
                transport=selected_transport,
            ),
        )

    started = time.monotonic()
    pool = futures.ThreadPoolExecutor(max_workers=workers)
    jobs = {pool.submit(work, key): key for key in todo}
    completed_keys: set[str] = set()
    try:
        try:
            for future in futures.as_completed(jobs, timeout=budget):
                key = jobs[future]
                completed_keys.add(key)
                try:
                    _key, summary, outcome = future.result()
                except Exception:
                    summary, outcome = None, "error"
                cache.put(key, summary, outcome, now)
                if summary:
                    for item in pending[key]:
                        item.description = summary
                    stats["fetched"] += 1
                else:
                    for item in pending[key]:
                        item.description = ""
                    stats["unusable"] += 1
                    if outcome == "error":
                        stats["errors"] += 1
                if time.monotonic() - started > budget:
                    raise futures.TimeoutError
        except futures.TimeoutError:
            unfinished = [future for future in jobs if not future.done()]
            stats["budget_hit"] = len(unfinished)
            for future in unfinished:
                future.cancel()
    finally:
        for key in set(todo) - completed_keys:
            for item in pending[key]:
                item.description = ""
            stats["unusable"] += 1
        pool.shutdown(wait=False, cancel_futures=True)
    return stats
