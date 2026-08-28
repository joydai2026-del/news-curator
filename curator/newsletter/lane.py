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
  * **The caller advances the cursor, and only as far as the run actually got.**
    `fetch()` returns the hashes and the watermark it WOULD commit; the
    orchestrator calls `state.advance()` only after the page is written, so a
    run that fetched mail and then died re-reads that mail next hour. The
    watermark is `now` ONLY when the batch was complete and every message was
    readable. When Gmail had more mail than the run took, or a message could
    not be fetched, the watermark stops at the newest message actually
    processed and the shortfall is named in the status line. Advancing to `now`
    after a short batch is how mail gets silently skipped, which is exactly
    what the design doc forbids.
  * **Who sent it is checked, not assumed.** Gmail's `from:` operator matches
    the From header, which anyone can write. Every message must carry a
    DKIM `pass` for a domain the adapter allows, or it is counted and dropped
    before it is parsed. See `adapters.authentication`.

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
    truncated: bool = False  # more mail matched than this run took
    unreadable_messages: int = 0  # listed, then could not be fetched
    unauthenticated_messages: int = 0  # DKIM present and failing, or wrong domain
    unauthenticated_missing: int = 0  # no Authentication-Results header at all

    @property
    def note(self) -> str:
        """The status line, plus anything the run lost. Counts only."""
        base = gmail_module.REASON_TEXT.get(self.reason, self.reason)
        extra = []
        if self.truncated:
            extra.append("more mail matched than this run read; cursor held back")
        if self.unreadable_messages:
            extra.append(f"{self.unreadable_messages} messages could not be read")
        rejected = self.unauthenticated_messages + self.unauthenticated_missing
        if rejected:
            extra.append(f"{rejected} messages failed sender authentication")
        return f"{base}; {'; '.join(extra)}" if extra else base

    @property
    def lossy(self) -> bool:
        """This run did not see everything the window held.

        Kept separate from `ok`: a truncated run is a SUCCESSFUL run that must
        not move the cursor as if it had finished.
        """
        return self.truncated or self.unreadable_messages > 0

    @property
    def pending_adapters(self) -> list[str]:
        return sorted(s.adapter_id for s in self.status.values() if s.state == "pending")


def enabled(env: dict | None = None, *, flag: bool = False, client=gmail_module) -> bool:
    """The feature flag. Both halves required: the switch AND the secrets.

    `fetch()` calls this rather than re-deriving it, so the invariant has one
    home. It used to read `cfg["enabled"]` and check credentials inline, which
    meant this function stated a rule nothing enforced.
    """
    return bool(flag) and client.has_credentials(os.environ if env is None else env)


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

def fair_cap(records: list[dict], cap: int) -> list[dict]:
    """The lane cap, applied ROUND-ROBIN BY SENDER, newest first within each.

    A plain newest-first cut let one high-volume sender own the whole tab: the
    first live week had TLDR taking 43 of 50 slots and The Rundown publishing
    zero. Every allowlisted sender gets a slot per round before any sender
    gets a second helping; the final list re-sorts newest-first so the page
    order is unchanged in the common case.
    """
    ordered = sorted(records, key=lambda r: r["published_at"], reverse=True)
    by_sender: dict[str, list[dict]] = {}
    for record in ordered:
        by_sender.setdefault(record["source_id"], []).append(record)
    taken: list[dict] = []
    queues = list(by_sender.values())
    while len(taken) < cap and any(queues):
        for queue in queues:
            if queue and len(taken) < cap:
                taken.append(queue.pop(0))
    taken.sort(key=lambda r: r["published_at"], reverse=True)
    return taken


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
    if not enabled(source_env, flag=want, client=client):
        # The switch is on and the secrets are not there. A different status
        # from `disabled`, because this one is a thing to fix.
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
    unauthenticated = 0
    unauthenticated_missing = 0
    newest_processed: datetime | None = None

    for msg in result.messages:
        # Processed = this message was read and judged, whatever the verdict.
        # That is the set the watermark may safely be based on.
        sent = _sent_at(msg, now)
        if newest_processed is None or sent > newest_processed:
            newest_processed = sent

        address = adapters_module.sender_address(msg)
        adapter = adapters_module.for_sender(address)
        if adapter is None or adapter.id not in status:
            unmatched += 1
            continue

        verdict = adapters_module.authentication(msg, adapter)
        if verdict != adapters_module.AUTH_PASS:
            # The From header said this was TLDR; the receiving server's own
            # DKIM stamp did not agree. Counted by adapter slug, never by
            # address or subject, and dropped before anything is parsed.
            if verdict == adapters_module.AUTH_MISSING:
                unauthenticated_missing += 1
            else:
                unauthenticated += 1
            log.info("newsletter message failed sender authentication (%s, %s)",
                     adapter.id, verdict)
            continue

        entry = status[adapter.id]
        entry.seen += 1

        parsed = adapter.extract(msg)
        entry.extracted += parsed.report.stories_found
        entry.dropped_links += parsed.report.links_dropped

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

    records = fair_cap(records, max(0, max_items))
    for record in records:
        status[record["source_id"].split(":", 1)[1]].published += 1

    # Only stories that actually got published are remembered, so one that fell
    # off the cap is not in the hash list and cannot be suppressed as a repeat.
    # It comes back only if the next window still covers its message, which the
    # overlap usually gives and the retention window eventually takes away.
    # That is a bounded loss of a STORY, and it is a different thing from
    # losing a MESSAGE, which is what the watermark below is about.
    published_hashes = [state.story_hash(r["title"], r["url"]) for r in records]

    # The cursor. `now` is only correct when this run consumed the whole
    # window: round 1 proved that advancing to `now` after a truncated batch
    # puts the unread tail permanently outside the next window. When anything
    # was missed, the cursor stops at the newest message actually processed,
    # so the wall-clock gap between that message and now is re-read next hour
    # instead of being skipped. It never moves backwards, because re-reading a
    # window the state already covers buys nothing the hash list does not.
    #
    # Honest limit, and it is the reason `truncated` is also on the status
    # line: Gmail lists newest first, so a truncated batch loses the OLDEST
    # tail, which holding the cursor here does not by itself recover. Draining
    # that tail needs pagination, which this lane deliberately does not do.
    # What this change buys is that the loss is REPORTED and that mail
    # arriving mid-run is not burned. Pagination is the follow-up.
    lossy = result.truncated or result.fetch_failures > 0
    watermark = now
    if lossy:
        watermark = state.watermark
        if newest_processed is not None:
            bounded = min(newest_processed, now)
            watermark = max(watermark, bounded) if watermark else bounded

    log.info(
        "newsletter lane: %d messages, %d unmatched, %d unauthenticated, "
        "%d items after dedup and cap, truncated=%s, unreadable=%d",
        len(result.messages), unmatched, unauthenticated + unauthenticated_missing,
        len(records), result.truncated, result.fetch_failures,
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
        watermark=watermark,
        truncated=result.truncated,
        unreadable_messages=result.fetch_failures,
        unauthenticated_messages=unauthenticated,
        unauthenticated_missing=unauthenticated_missing,
    )
