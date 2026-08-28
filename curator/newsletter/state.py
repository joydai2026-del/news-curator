"""The durable cursor: how the lane remembers what it already published.

The file is committed to a PUBLIC repository, so its contents are the whole
design constraint. It holds four keys and nothing else:

    {"version": 1, "watermark": "<iso8601>", "salt": "<hex>", "hashes": [...]}

No message ids, no subjects, no addresses, no titles in the clear. A hash is
sha256 over `salt + extracted story title + publisher URL`, which is enough to
recognize a story we already showed and useless for reconstructing anything.

**Honest limit of the salt.** It sits in the same public file as the hashes, so
it does not hide the hashed values from anyone determined to check a guess. It
is not there for that. It is there so the file cannot be turned into a
precomputed rainbow table of every headline on the internet, and so the same
story hashed by two different forks does not collide into a shared identifier.
The actual privacy guarantee is the one above: nothing identifying is stored at
all.

**The watermark only moves on success, and the caller moves it.** `load()` and
`plan_window()` are read-only; `advance()` is a separate, explicit call. A run
that fetched mail and then died during render must NOT have advanced the
cursor, or that mail is gone forever. The poll window therefore overlaps the
watermark (6h by default) and the hash list catches the re-reads.

Pruning is by COUNT, not by age, because storing a timestamp beside each hash
would put a per-story clock in a public file for no gain. Oldest entries fall
off the front once the list passes the cap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..normalize import fold_text

log = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "newsletter_state.json"

ALLOWED_KEYS = ("version", "watermark", "salt", "hashes")

DEFAULT_OVERLAP_HOURS = 6.0
DEFAULT_LOOKBACK_HOURS = 48.0
MAX_HASHES = 2000


@dataclass
class NewsletterState:
    """Exactly the four fields that are allowed on disk."""

    watermark: datetime
    salt: str
    hashes: list[str] = field(default_factory=list)
    version: int = STATE_VERSION

    @property
    def seen(self) -> set[str]:
        return set(self.hashes)

    def story_hash(self, title: str, url: str) -> str:
        return story_hash(self.salt, title, url)

    def to_dict(self) -> dict:
        return {
            "version": int(self.version),
            "watermark": _iso(self.watermark),
            "salt": self.salt,
            "hashes": list(self.hashes),
        }


def story_hash(salt: str, title: str, url: str) -> str:
    """Stable identity for one extracted story.

    The title is folded first so that a publisher re-sending the same headline
    with different quote characters does not read as a new story. The URL is
    included as-is (already sanitized by the caller) and is empty for a story
    whose link had to be dropped, which is fine: title plus salt still
    identifies it.
    """
    material = "\x1f".join((salt, fold_text(title or ""), (url or "").strip()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_state(now: datetime, *, lookback_hours: float = DEFAULT_LOOKBACK_HOURS) -> NewsletterState:
    """A first run: look back one retention window, invent a salt, remember nothing."""
    return NewsletterState(
        watermark=now - timedelta(hours=lookback_hours),
        salt=secrets.token_hex(16),
        hashes=[],
    )


def load(path: Path, *, now: datetime, lookback_hours: float = DEFAULT_LOOKBACK_HOURS) -> NewsletterState:
    """Read the cursor. A missing or unreadable file is a first run, never a crash.

    An unreadable state file must not take the build down: the worst case of
    starting fresh is re-showing 48 hours of stories, and the hash list rebuilds
    itself on the next successful run.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return new_state(now, lookback_hours=lookback_hours)
    except (OSError, ValueError) as exc:
        log.warning("newsletter state unreadable (%s); starting a fresh window", type(exc).__name__)
        return new_state(now, lookback_hours=lookback_hours)

    if not isinstance(raw, dict):
        return new_state(now, lookback_hours=lookback_hours)

    watermark = _parse_iso(raw.get("watermark")) or (now - timedelta(hours=lookback_hours))
    salt = raw.get("salt")
    if not isinstance(salt, str) or not salt.strip():
        salt = secrets.token_hex(16)
    hashes_raw = raw.get("hashes")
    hashes = [h for h in hashes_raw if isinstance(h, str) and h] if isinstance(hashes_raw, list) else []
    version = raw.get("version")
    return NewsletterState(
        watermark=watermark,
        salt=salt.strip(),
        hashes=hashes[-MAX_HASHES:],
        version=int(version) if isinstance(version, int) else STATE_VERSION,
    )


def plan_window(
    state: NewsletterState,
    now: datetime,
    *,
    overlap_hours: float = DEFAULT_OVERLAP_HOURS,
    max_lookback_hours: float = DEFAULT_LOOKBACK_HOURS * 4,
) -> tuple[datetime, datetime]:
    """(start, end) for this poll, overlapping the watermark.

    The overlap is what makes a failed run harmless: the next run re-reads the
    same mail and the hash list drops the repeats. The lookback is capped so
    that a cursor left behind for a month does not ask Gmail for a month.
    """
    start = state.watermark - timedelta(hours=max(0.0, overlap_hours))
    floor = now - timedelta(hours=max_lookback_hours)
    if start < floor:
        start = floor
    if start > now:
        start = now
    return start, now


def advance(
    path: Path,
    state: NewsletterState,
    *,
    watermark: datetime,
    new_hashes: list[str],
    max_hashes: int = MAX_HASHES,
) -> NewsletterState:
    """Write the cursor forward. Call this ONLY after a successful build.

    Returns the state that was written, so a caller can assert on it without
    re-reading the file.
    """
    merged = list(state.hashes)
    known = set(merged)
    for value in new_hashes:
        if value and value not in known:
            merged.append(value)
            known.add(value)
    merged = merged[-max_hashes:]

    written = NewsletterState(watermark=watermark, salt=state.salt, hashes=merged, version=STATE_VERSION)
    _write_atomic(Path(path), written.to_dict())
    log.info("newsletter cursor advanced, %d hashes retained", len(merged))
    return written


def _write_atomic(path: Path, payload: dict) -> None:
    """Write via a temp file and rename, so a killed run never truncates state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
