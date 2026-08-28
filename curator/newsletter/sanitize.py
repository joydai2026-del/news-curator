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

**The failure mode is "lose a link", never "leak an identifier".** Review round
1 proved the earlier design had it backwards. The gate used to be a BLOCKLIST of
token shapes: a query parameter it did not recognise was KEPT. So
`?email=jj%40example.com`, `?subid=JJ7742`, `?token=aBcDeFgHiJkLmNoP` and an
address sitting in a path segment all sailed through onto a public page and into
a public git history, permanently. Every miss in a blocklist is irreversible.
The rest of this codebase reaches for allowlists in exactly this spot
(`normalize.ALLOWED_SCHEMES`, the adapter sender allowlist, the topics
allowlist), and now so does this module.

Four rules, applied in this order:

  1. **Static extraction only.** Many trackers carry the destination with them:
     a `?url=` parameter, a percent-encoded path segment (the shape TLDR's
     sendgrid-style `/CL0/https:%2F%2F.../1/...` links use), a base64 payload.
     Those unwrap locally, for free, and are recursed a bounded number of times.
     A link still wrapped when the depth bound runs out is refused, not shipped
     half-unwrapped.
  2. **Refuse anything that smells like an address.** If `@` or `%40` survives
     anywhere in the host, path, query or fragment (checked through repeated
     percent-decoding, so `%2540` cannot hide), the answer is None. The
     subscriber's own address is the highest-value identifier in the system and
     it gets its own rule rather than a heuristic.
  3. **Drop the whole query string. All of it.** No parameter name is
     allowlisted, and the fragment goes too. Round 2 killed the four-name
     allowlist (`p`, `id`, `story`, `v`) that used to sit here, because it was
     the last remaining leak channel and the measurement said closing it was
     free: across all four real-mail captures, 27 URLs survive the sanitizer
     and ZERO of them keep a content parameter. `id` in particular is the most
     likely name for a subscriber identifier, which makes it the worst possible
     member of a privacy allowlist. The cost is a link to a query-addressed
     article (`?p=12345` on an old WordPress install) pointing at the site root
     instead; the benefit is that there is no longer any parameter name an
     identifier can hide behind.
  4. **When in doubt, drop the link.** Still on a known tracker host, or an
     opaque token-shaped path segment: None. A story with no link is a small
     loss. A leaked subscriber id is permanent.

`is_suspect()` is the output-boundary twin of all four rules and is deliberately
the STRONGER predicate: it is true for anything `sanitize()` would reject OR
merely strip, so a URL that passes it is one this module would emit verbatim.
That makes it usable as a last-line assertion over rendered hrefs. It is scoped
to NEWSLETTER-DERIVED links: an ordinary feed URL carrying `?page=2` is suspect
by this definition, which is correct for this lane and wrong as a general
judgement of a publisher link.

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
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from ..normalize import safe_url

# The junk-parameter blocklist this module used to import from `normalize` is
# GONE, deliberately. Round 1 proved it was the wrong tool here: it named the
# parameters we had thought of (`e`, `ck_subscriber_id`, `utm_*`) and kept
# everything else, so `?subid=`, `?token=` and `?ref=` all published. The
# allowlist below subsumes it for newsletter links: nothing survives unless it
# is named. `normalize.canonical_url` still does blocklist stripping for the
# six ordinary feed lanes, where losing a query parameter is a real cost and a
# subscriber id is not the threat.

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

_OPAQUE_CHARS = re.compile(r"^[A-Za-z0-9_\-=~%+.]+$")
_LOWER_SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

# There is no query-parameter allowlist any more, deliberately, and this
# comment is here so a later edit has to argue with it rather than rediscover
# it. `CONTENT_PARAMS = {"p", "id", "story", "v"}` used to live here and was
# removed in review round 2. The case for it was that a publisher sometimes
# names an article in the query; the case against it was measured on real mail
# and won 27-0. Re-adding ANY name re-opens the channel, because the value side
# cannot be judged: `?id=JJ7742` is six characters and `?id=<32 lowercase
# letters>` has no digits or separators, so neither trips a token-shape test,
# and both are perfectly good subscriber identifiers. If a future sender really
# does need a parameter, scope it to a specific HOST rather than allowing a
# name everywhere.

MAX_UNWRAP_DEPTH = 4
# How many times to percent-decode before deciding a component holds no `@`.
# Two hops catches `%40` and `%2540`; a third is free insurance.
_DECODE_HOPS = 3


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


def looks_like_address(text: str) -> bool:
    """Does this component contain an `@`, however many times it is encoded?

    A plain `@`, a `%40`, a `%2540`. The subscriber address is the one string
    the old shape-based detector structurally could not see (`@` was missing
    from `_OPAQUE_CHARS`), which is why it now gets a rule of its own.
    """
    seen = text or ""
    for _ in range(_DECODE_HOPS):
        if "@" in seen or "%40" in seen.lower():
            return True
        nxt = unquote(seen)
        if nxt == seen:
            return False
        seen = nxt
    return False


def carries_address(url: str) -> bool:
    """True when any part of the URL carries an address-shaped component."""
    parts = urlsplit(url)
    fields = [parts.netloc, parts.path, parts.query, parts.fragment]
    fields += [seg for seg in (parts.path or "").split("/") if seg]
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        fields += [name, value]
    return any(looks_like_address(f) for f in fields)


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


def _publisher_url(url: str) -> str | None:
    """The bounded gate: rules 2, 3 and 4 on an already-unwrapped URL.

    Split out from `sanitize()` so `is_suspect()` can be defined in terms of
    the real answer rather than in terms of a second, drifting predicate. The
    old code had `sanitize` ask `is_suspect` and `is_suspect` re-derive the
    rules; a shape one of them missed, both missed.
    """
    candidate = safe_url(url)
    if candidate is None:
        return None
    if is_tracker_host(candidate):
        return None
    if carries_address(candidate):
        return None

    parts = urlsplit(candidate)
    if any(is_token_like(seg) for seg in (parts.path or "").split("/") if seg):
        return None

    # Rule 3: the whole query goes, and the fragment with it. Neither is needed
    # to name an article on any of the five shipped senders' destinations, and
    # both are places a recipient id hides.
    return safe_url(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))


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
    else:
        # Depth exhausted with a wrapper still on top. Publishing the outer hop
        # would publish the tracker's own token, so this is a refusal.
        remaining = _unwrap_once(url)
        if remaining and remaining != url:
            return None
    return _publisher_url(url)


def is_suspect(url: str) -> bool:
    """True when this URL is NOT what the sanitizer would publish.

    Stronger than "would be rejected": it is also true when the sanitizer would
    merely STRIP something, so `is_suspect(u) is False` means `u` is exactly
    what this module emits. That is what makes it usable as an output-boundary
    assertion over rendered hrefs, where the question is "did anything reach
    the page that this module would not have written itself".

    Formally: `is_suspect(u)` iff `sanitize(u) != u` (None included).
    """
    if safe_url(url) is None:
        return True
    return sanitize(url) != url
