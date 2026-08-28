"""Turning whatever a source gave us into something safe and comparable.

Three separate jobs that must not be confused, because collapsing them caused
real bugs:

  * `clean_title`  — what the reader SEES. Faithful to the publisher: smart
                     quotes, dashes and Unicode are preserved. Only markup and
                     stray whitespace are removed.
  * `fold_text`    — what the MATCHER and DEDUPER see. Aggressively normalized.
                     Never displayed.
  * `safe_url`     — a link we are willing to put an `href` on at all.

Two bugs this file exists to prevent, both found in review:

  1. Unescaping entities BEFORE stripping tags turned `2 &lt; 3 &gt; 1` into
     `2 1`, because `< 3 >` then looked like a tag. Tags are stripped from the
     raw text first, and entities are unescaped exactly once afterwards.
  2. HTML-escaping an `href` does not make it safe. `javascript:alert(1)` is a
     perfectly valid string that a compromised feed could hand us, and escaping
     it changes nothing. Scheme is allow-listed instead.
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ALLOWED_SCHEMES = ("http", "https")

# Parameters that never identify the article itself.
_JUNK_PARAM_PREFIXES = ("utm_",)
_JUNK_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "at_medium", "at_campaign", "guccounter", "__twitter_impression",
    "cmpid", "campaign_id", "smid",
}

# Folding table for the matcher only. The displayed title keeps the originals.
_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", " ": " ", "​": "", "⁠": "",
}


def clean_title(raw: str) -> str:
    """The headline as the reader will see it, faithful to the publisher.

    Order matters: tags come out of the RAW text, then entities are unescaped
    exactly once. Reversing these two steps deletes real content.
    """
    if not raw:
        return ""
    text = _TAG.sub(" ", str(raw))
    text = html.unescape(text)
    # Any tag-looking residue after unescaping is literal text the publisher
    # wrote, so it is left alone. The renderer escapes it on the way out.
    text = _CTRL.sub("", text)
    return _WS.sub(" ", text).strip()


def fold_text(text: str) -> str:
    """Normalized form for matching and deduping. Never shown to anyone."""
    if not text:
        return ""
    for bad, good in _FOLD.items():
        text = text.replace(bad, good)
    text = unicodedata.normalize("NFKC", text)
    return _WS.sub(" ", text).strip()


def safe_url(raw: str) -> str | None:
    """Return the URL only if it is an absolute http(s) URL with a host.

    Everything else (`javascript:`, `data:`, `file:`, relative paths, junk)
    returns None and the item is dropped. A feed we do not control must never be
    able to decide what goes in an `href` on a public page.
    """
    if not raw:
        return None
    candidate = _CTRL.sub("", str(raw)).strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return None
    try:
        if not parts.hostname:
            return None
    except ValueError:
        return None
    # Credentials in a URL are either an accident or an attempt to make a
    # hostile host look like a familiar one (https://github.com@evil.example).
    # Neither belongs in an href on a public page.
    if parts.username or parts.password:
        return None
    return candidate


def canonical_url(raw: str) -> str | None:
    """A stable identity for a link, so the same story dedupes to one row.

    Returns None for anything `safe_url` rejects, so callers cannot accidentally
    build an Item around an unsafe link.
    """
    candidate = safe_url(raw)
    if candidate is None:
        return None

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        host = f"{host}:{port}"

    # Order is preserved. Sorting parameters can merge genuinely different
    # resources on sites where parameter order is meaningful.
    keep = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _JUNK_PARAMS
        and not any(k.lower().startswith(p) for p in _JUNK_PARAM_PREFIXES)
    ]
    query = urlencode(keep)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # Fragment always dropped: it never identifies a different article.
    return urlunsplit((scheme, host, path, query, ""))
