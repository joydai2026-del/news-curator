"""One stable public identity for a normalized story."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Item

from .normalize import canonical_url

_NEWSLETTER_PSEUDO_CANONICAL = re.compile(r"newsletter:(?:[0-9a-f]{16}|[0-9a-f]{64})")

def story_id_for_item(item: "Item") -> str:
    """Return the canonical story id used by every durable artifact."""

    private_newsletter_anchor = getattr(item, "newsletter_identity", "")
    raw_anchor = item.canonical_url or item.url
    if (
        item.is_newsletter
        and isinstance(private_newsletter_anchor, str)
        and _NEWSLETTER_PSEUDO_CANONICAL.fullmatch(private_newsletter_anchor)
    ):
        anchor = private_newsletter_anchor
    elif (
        item.is_newsletter
        and isinstance(item.canonical_url, str)
        and _NEWSLETTER_PSEUDO_CANONICAL.fullmatch(item.canonical_url)
    ):
        anchor = item.canonical_url
    else:
        anchor = canonical_url(raw_anchor or "")
    if not anchor:
        anchor = "\0".join(
            (item.source_id, item.title, item.published_at.astimezone(timezone.utc).isoformat())
        )
    return "story:" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()


def coverage_mention_id(
    *, source_kind: str, source_id: str, mentioned_at: datetime,
    url: str, headline: str,
) -> str:
    """Return the one durable identity used by every mention producer."""

    normalized = canonical_url(url) or ""
    moment = mentioned_at.astimezone(timezone.utc).isoformat()
    material = "\x1f".join((source_kind, source_id, moment, normalized, headline))
    return "mention:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
