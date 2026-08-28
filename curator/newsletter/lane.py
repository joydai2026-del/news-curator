"""The seventh lane: JJ's own newsletter subscriptions, as ordinary stories.

This is the only module the pipeline needs to know about. It reads the cursor,
asks Gmail for the window, routes each message to its adapter, sanitizes the
links, drops what it has already published, and hands back items plus a status
block honest enough to print.

Three properties the rest of the codebase depends on:

  * **Dark by default.** `enabled()` is False unless the caller passes an
    explicit flag AND all three Gmail secrets exist. A fork that clones this
    repo gets no tab, no dependency, and no empty section. Nothing here calls
    Gmail before that check.
  * **Never raises.** Every failure comes back as `LaneResult(dark=True,
    reason=<slug>)`. A revoked refresh token darkens one lane and leaves a
    visible warning; it does not fail the hourly build of six healthy tabs.
  * **The caller advances the cursor.** `fetch()` returns the hashes and the
    watermark it WOULD commit. The orchestrator calls `state.advance()` only
    after the page is written. A run that fetched mail and then died must
    re-read that mail next hour, not skip it.

Privacy, restated because this is where items are born: `image_url` is always
empty (no og:image fetch, no image-cache entry, ever), `url` is either a
sanitized publisher URL or the empty string, and the raw newsletter link never
makes it into a record. The lane reports counts and adapter slugs. It never
reports addresses or subjects.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from ..normalize import canonical_url, clean_title, fold_text
from . import adapters as adapters_module
from . import gmail as gmail_module
from . import state as state_module

log = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_AGE_HOURS = 48.0
DEFAULT_MAX_MESSAGES = 30
DEFAULT_OVERLAP_HOURS = 6.0

DISABLED = "disabled"
NO_ADAPTERS = "no_adapters_enabled"


@dataclass
class AdapterStatus:
    """Per-sender truth, including the senders that produced nothing."""

    adapter_id: str
    name: str
    seen: int = 0  # messages routed to this adapter
    extracted: int = 0  # stories its parser found
    dropped_links: int = 0  # stories whose link could not be made safe
    published: int = 0  # stories that survived age, dedup and the cap

    @property
    def state(self) -> str:
        if self.seen == 0:
            return "idle"  # no mail from this sender in the window
        if self.extracted == 0:
            return "pending"  # mail arrived and the adapter got nothing
        return "ok"

    @property
    def hit_rate(self) -> float | None:
        """Stories per message. None when no mail arrived to measure against."""
        return None if self.seen == 0 else self.extracted / self.seen


@dataclass
class LaneResult:
    items: list = field(default_factory=list)
    status: dict[str, AdapterStatus] = field(default_factory=dict)
    ok: bool = True
    dark: bool = False
    reason: str = gmail_module.OK
    unmatched_messages: int = 0  # mail from a sender with no adapter
    hashes: list[str] = field(default_factory=list)
    watermark: datetime | None = None

    @property
    def note(self) -> str:
        return gmail_module.REASON_TEXT.get(self.reason, self.reason)

    @property
    def pending_adapters(self) -> list[str]:
        return sorted(s.adapter_id for s in self.status.values() if s.state == "pending")


def enabled(env: dict | None = None, *, flag: bool = False) -> bool:
    """The feature flag. Both halves required: the switch AND the secrets."""
    return bool(flag) and gmail_module.has_credentials(os.environ if env is None else env)


# --------------------------------------------------------------------------
# item construction
# --------------------------------------------------------------------------

def _fallback_canonical(title: str) -> str:
    """Identity for a story whose link had to be dropped.

    Unsalted on purpose: it is derived from a public headline, it must stay
    stable across runs so the story dedupes against itself, and it carries
    nothing about the subscriber. The salted hash in the state file is a
    different mechanism for a different job.
    """
    digest = hashlib.sha256(fold_text(title).encode("utf-8")).hexdigest()[:16]
    return f"newsletter:{digest}"


def build_record(
    *,
    title: str,
    url: str,
    blurb: str,
    adapter_id: str,
    display_name: str,
    published_at: datetime,
) -> dict:
    """One newsletter story as a plain dict.

    Kept separate from `Item` so this lane can be built and tested whatever
    shape the shared model is in this hour, and so the privacy invariants
    (`image_url` empty, `url` sanitized-or-empty) live in ONE place.
    """
    clean = clean_title(title)
    safe = url or ""
    canonical = (canonical_url(safe) if safe else None) or _fallback_canonical(clean)
    return {
        "title": clean,
        "url": safe,
        "canonical_url": canonical,
        "source_id": f"newsletter:{adapter_id}",
        "source_name": display_name,
        "platform": f"newsletter:{adapter_id}",
        "published_at": published_at,
        "description": clean_title(blurb)[:adapters_module.MAX_BLURB_CHARS],
        "is_newsletter": True,
        "newsletter_sender": display_name,
        "image_url": "",  # PRIVACY RULE: newsletter items never carry an image
    }


def _item_class():
    """`Item` only if it already carries the newsletter fields, else None.

    The shared model is being extended in a parallel change. Until it lands,
    this lane emits dicts rather than editing someone else's file or crashing.
    """
    try:
        from ..models import Item
    except Exception:
        return None
    names = {f.name for f in dataclass_fields(Item)}
    needed = {"description", "is_newsletter", "newsletter_sender"}
    return Item if needed <= names else None


def to_items(records: list[dict]) -> list:
    """Records as `Item`s when the model supports them, otherwise unchanged."""
    item_class = _item_class()
    if item_class is None:
        return list(records)
    out = []
    for record in records:
        out.append(
            item_class(
                title=record["title"],
                url=record["url"],
                canonical_url=record["canonical_url"],
                source_id=record["source_id"],
                source_name=record["source_name"],
                platform=record["platform"],
                published_at=record["published_at"],
                image_url="",
                description=record["description"],
                is_newsletter=True,
                newsletter_sender=record["newsletter_sender"],
            )
        )
    return out


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def _sent_at(msg: Message, fallback: datetime) -> datetime:
    """The Date header in UTC. A missing or unparseable one falls back."""
    raw = msg.get("Date")
    if not raw:
        return fallback
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return fallback
    if parsed is None:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch(
    cfg: dict | None,
    state: state_module.NewsletterState,
    now: datetime,
    *,
    env: dict | None = None,
    flag: bool | None = None,
    client=gmail_module,
) -> LaneResult:
    """Read the mailbox and return newsletter items. Never raises.

    `cfg` is the `newsletter:` block from sources.yaml (a plain mapping, so the
    pipeline can pass it through without this module importing the config
    loader). `flag` overrides `cfg["enabled"]` for tests and for a command-line
    switch.
    """
    cfg = dict(cfg or {})
    source_env = os.environ if env is None else env
    want = bool(cfg.get("enabled", False)) if flag is None else bool(flag)

    if not want:
        return LaneResult(ok=True, dark=True, reason=DISABLED, watermark=state.watermark)
    if not client.has_credentials(source_env):
        return LaneResult(
            ok=False, dark=True, reason=gmail_module.MISSING_CREDENTIALS, watermark=state.watermark
        )

    adapter_ids = [str(a) for a in (cfg.get("adapters") or adapters_module.ADAPTER_IDS)]
    active = [a for a in adapters_module.ADAPTERS if a.id in set(adapter_ids)]
    if not active:
        return LaneResult(ok=False, dark=True, reason=NO_ADAPTERS, watermark=state.watermark)

    max_items = int(cfg.get("max_items", DEFAULT_MAX_ITEMS))
    max_age_hours = float(cfg.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    max_messages = int(cfg.get("max_messages", DEFAULT_MAX_MESSAGES))
    overlap_hours = float(cfg.get("overlap_hours", DEFAULT_OVERLAP_HOURS))
    timeout = float(cfg.get("request_timeout", gmail_module.DEFAULT_TIMEOUT))

    start, _end = state_module.plan_window(state, now, overlap_hours=overlap_hours)
    senders = adapters_module.sender_queries([a.id for a in active])

    result = client.fetch(
        senders, start, env=source_env, limit=max_messages, timeout=timeout
    )
    status = {a.id: AdapterStatus(adapter_id=a.id, name=a.name) for a in active}
    if not result.ok:
        return LaneResult(
            items=[], status=status, ok=False, dark=True,
            reason=result.reason, watermark=state.watermark,
        )

    cutoff = now - timedelta(hours=max_age_hours)
    already = state.seen
    seen_now: set[str] = set()
    records: list[dict] = []
    unmatched = 0

    for msg in result.messages:
        address = adapters_module.sender_address(msg)
        adapter = adapters_module.for_sender(address)
        if adapter is None or adapter.id not in status:
            unmatched += 1
            continue
        entry = status[adapter.id]
        entry.seen += 1

        parsed = adapter.extract(msg)
        entry.extracted += parsed.report.stories_found
        entry.dropped_links += parsed.report.links_dropped

        sent = _sent_at(msg, now)
        if sent < cutoff:
            continue
        for story in parsed.stories:
            digest = state.story_hash(story.title, story.url)
            if digest in already or digest in seen_now:
                continue
            seen_now.add(digest)
            records.append(
                build_record(
                    title=story.title,
                    url=story.url,
                    blurb=story.blurb,
                    adapter_id=adapter.id,
                    display_name=adapter.name,
                    published_at=sent,
                )
            )

    records.sort(key=lambda r: r["published_at"], reverse=True)
    records = records[: max(0, max_items)]
    for record in records:
        status[record["source_id"].split(":", 1)[1]].published += 1

    # Only stories that actually got published are remembered. One that fell
    # off the cap must be eligible again next run, not silently burned.
    published_hashes = [state.story_hash(r["title"], r["url"]) for r in records]

    log.info(
        "newsletter lane: %d messages, %d unmatched, %d items after dedup and cap",
        len(result.messages), unmatched, len(records),
    )
    for entry in status.values():
        log.info(
            "newsletter adapter %-12s %s seen=%d extracted=%d dropped_links=%d published=%d",
            entry.adapter_id, entry.state, entry.seen, entry.extracted,
            entry.dropped_links, entry.published,
        )

    return LaneResult(
        items=to_items(records),
        status=status,
        ok=True,
        dark=False,
        reason=gmail_module.OK,
        unmatched_messages=unmatched,
        hashes=published_hashes,
        watermark=now,
    )
