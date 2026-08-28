"""Newsletter links, and why most of them cannot be shown as they arrive.

A newsletter link is almost never a link to the article. It is a link to a
tracking hop that knows WHO clicked, because the subscriber id is baked into
the path or the query string. Publishing that URL on a public page would
publish JJ's subscriber identity for every one of those newsletters, forever,
in a git history.

So this module has exactly one job: turn a newsletter link into a publisher
link that carries no identifier, or refuse. Refusing is a normal outcome, not
an error, and the caller renders the story WITHOUT a link when it happens. The
design doc calls this the PRIVACY RULE and it is not negotiable here.

Three rules, applied in this order:

  1. **Static extraction only.** Many trackers carry the destination with them:
     a `?url=` parameter, a percent-encoded path segment (the shape TLDR's
     sendgrid-style `/CL0/https:%2F%2F.../1/...` links use), a base64 payload.
     Those unwrap locally, for free, and are recursed a bounded number of times.
  2. **Strip tracking parameters.** The junk-parameter list in
     `curator.normalize` is IMPORTED and extended here rather than copied. A
     second copy would drift from the first within a month.
  3. **When in doubt, drop the link.** If the URL is still on a known tracker
     host, or still carries an opaque token-shaped component anywhere, this
     returns None. A story with no link is a small loss. A leaked subscriber id
     is permanent.

**No network resolution, deliberately.** Following the redirect would resolve
the destination perfectly, and it would do it by SENDING the tracking token
from a GitHub Actions runner, which is the exact event the tracker is waiting
for: it registers as a click by JJ and tells the sender the mail was read. A
privacy sanitizer must not be the thing that phones home. v1 is static-only.

A redirect parameter is trusted only when its value itself parses as an
absolute http(s) URL. That single check is what keeps `u=` safe: on a Mailchimp
link `u=` is the ACCOUNT id, not a destination, and it fails the check and is
ignored instead of becoming a bogus link.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Imported, not copied: one junk-parameter list for the whole codebase. The
# names are private to normalize.py by convention, and reaching for them here is
# the deliberate lesser evil against two lists that silently diverge.
from ..normalize import _JUNK_PARAM_PREFIXES as _BASE_JUNK_PREFIXES
from ..normalize import _JUNK_PARAMS as _BASE_JUNK_PARAMS
from ..normalize import safe_url

# Parameters that identify the SUBSCRIBER or the send, never the article.
# Single letters look reckless and are not: `e`, `u`, `r`, `mid` are the
# recipient handles used by Mailchimp, Substack and friends. A publisher URL
# that genuinely needs one of these is rarer than a leak.
_NEWSLETTER_JUNK_PARAMS = {
    "e", "u", "r", "rid", "sid", "aid", "mid", "bhid", "_bhlid",
    "ck_subscriber_id", "subscriber_id", "subscriberid", "recipient",
    "recipient_id", "vero_id", "vero_conv", "emci", "emdi", "ceid",
    "lctg", "goal", "s_i", "sc_customer", "elqTrackId", "elq",
}
_NEWSLETTER_JUNK_PREFIXES = ("ck_", "vero_", "mkt_", "hsa_", "_hs", "pk_", "mtm_", "oly_", "trk_")

JUNK_PARAMS = {p.lower() for p in _BASE_JUNK_PARAMS} | {p.lower() for p in _NEWSLETTER_JUNK_PARAMS}
JUNK_PARAM_PREFIXES = tuple(_BASE_JUNK_PREFIXES) + _NEWSLETTER_JUNK_PREFIXES

# Query parameters that sometimes carry the destination. The value still has to
# parse as an absolute http(s) URL before it is believed.
_REDIRECT_PARAMS = ("url", "redirect", "redirect_url", "redirect_uri", "destination",
                    "dest", "target", "link", "to", "u", "r")

# Hosts whose entire job is click tracking. A URL still on one of these after
# extraction is a URL we refuse to publish.
_TRACKER_HOSTS = frozenset({
    "link.mail.beehiiv.com",
    "tracking.tldrnewsletter.com",
    "links.tldr.tech",
})
_TRACKER_HOST_PATTERNS = (
    re.compile(r"^email\.mg\d*\.substack\.com$"),
    re.compile(r"(^|\.)list-manage\d*\.com$"),
    re.compile(r"(^|\.)ct\.sendgrid\.net$"),
    re.compile(r"(^|\.)sendgrid\.net$"),
    re.compile(r"^click\.convertkit-mail\d*\.com$"),
    re.compile(r"^(link|links|click|clicks|track|tracking)\.mail\..+"),
)

# Subdomains that are tracker-shaped. On their own they prove nothing (plenty
# of real sites live at mail.example.com), so they only condemn a URL when the
# path also carries an opaque token.
_TRACKER_SUBDOMAINS = ("link", "links", "click", "clicks", "track", "tracking",
                       "email", "e", "em", "go", "t")

_OPAQUE_CHARS = re.compile(r"^[A-Za-z0-9_\-=~%+.]+$")
_LOWER_SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

MAX_UNWRAP_DEPTH = 4


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_tracker_host(url: str) -> bool:
    """A host whose only product is knowing who clicked."""
    host = _host(url)
    if not host:
        return False
    if host in _TRACKER_HOSTS:
        return True
    if any(pattern.search(host) for pattern in _TRACKER_HOST_PATTERNS):
        return True
    # Substack's own redirector lives on the main domain, path-scoped.
    if (host == "substack.com" or host.endswith(".substack.com")):
        path = urlsplit(url).path or ""
        if path.startswith("/redirect"):
            return True
    return False


def is_token_like(text: str) -> bool:
    """Does this path segment or parameter value look like an identifier?

    Deliberately blunt. A human-readable slug (`the-rise-of-small-models`) is
    all lowercase with word separators; an identifier is long, mixed-case, hex,
    or base64-shaped. Anything ambiguous counts as an identifier, because the
    cost of a false positive is one missing link and the cost of a false
    negative is a published subscriber id.
    """
    if not text or len(text) < 16:
        return False
    if not _OPAQUE_CHARS.match(text):
        return False
    if "=" in text or "%" in text:
        return True
    has_upper = any(c.isupper() for c in text)
    has_lower = any(c.islower() for c in text)
    has_digit = any(c.isdigit() for c in text)
    if has_upper and has_lower and has_digit:
        return True
    separated = _LOWER_SLUG.match(text) and any(c in "._-" for c in text)
    if separated:
        # A long lowercase slug with separators is how publishers write titles.
        return False
    if has_digit and len(text) >= 24:
        return True
    return len(text) >= 40


def _components(url: str) -> list[str]:
    parts = urlsplit(url)
    out = [seg for seg in (parts.path or "").split("/") if seg]
    out += [value for _, value in parse_qsl(parts.query, keep_blank_values=True)]
    if parts.fragment:
        out.append(parts.fragment)
    return out


def is_suspect(url: str) -> bool:
    """True when publishing this URL could publish who subscribed.

    Used by the privacy test as the single yes/no the whole lane is judged on.
    """
    candidate = safe_url(url)
    if candidate is None:
        return True
    if is_tracker_host(candidate):
        return True
    host = _host(candidate)
    first_label = host.split(".")[0] if host else ""
    tokens = [c for c in _components(candidate) if is_token_like(c)]
    if first_label in _TRACKER_SUBDOMAINS and tokens:
        return True
    return bool(tokens)


def _decoded_url(raw: str) -> str | None:
    """Is this blob an http(s) URL once percent- or base64-decoded?"""
    if not raw:
        return None
    for candidate in (raw, unquote(raw)):
        if candidate.lower().startswith(("http://", "https://")):
            return safe_url(candidate)
    if len(raw) >= 16 and _OPAQUE_CHARS.match(raw):
        padded = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "strict")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if decoded.lower().startswith(("http://", "https://")):
            return safe_url(decoded)
    return None


def _unwrap_once(url: str) -> str | None:
    """One hop of static extraction, or None when nothing is recoverable."""
    parts = urlsplit(url)
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() in _REDIRECT_PARAMS:
            found = _decoded_url(value)
            if found and found != url:
                return found
    for segment in (parts.path or "").split("/"):
        found = _decoded_url(segment)
        if found and found != url:
            return found
    return None


def strip_tracking(url: str) -> str | None:
    """Drop junk parameters and the fragment, keep everything else verbatim."""
    candidate = safe_url(url)
    if candidate is None:
        return None
    parts = urlsplit(candidate)
    keep = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in JUNK_PARAMS
        and not any(k.lower().startswith(p.lower()) for p in JUNK_PARAM_PREFIXES)
    ]
    # Fragment dropped: it never identifies a different article, and trackers
    # do sometimes hide the recipient there.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(keep), ""))


def sanitize(raw: str, *, max_depth: int = MAX_UNWRAP_DEPTH) -> str | None:
    """The publisher URL, or None when one cannot be recovered safely.

    None is a normal, expected answer. The caller renders the story with no
    link rather than guessing.
    """
    url = safe_url(raw)
    if url is None:
        return None
    for _ in range(max_depth):
        nxt = _unwrap_once(url)
        if not nxt or nxt == url:
            break
        url = nxt
    stripped = strip_tracking(url)
    if stripped is None:
        return None
    if is_suspect(stripped):
        return None
    return safe_url(stripped)
