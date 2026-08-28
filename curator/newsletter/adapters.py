"""The allowlist: five named newsletters, one small adapter each.

The honest contract from the design doc: **generic newsletter parsing does not
work**. Every sender lays its stories out differently, and a parser that tries
to be universal produces a page of navigation links and unsubscribe footers. So
v1 names five senders, gives each an adapter, and REPORTS what each one
extracted. A sender in the allowlist whose adapter found nothing is listed as a
pending adapter, never silently dropped, because "we shipped and the section is
empty" is the failure this rule exists to catch.

**The five share one extractor on purpose.** All five write a headline followed
by the newsletter's own sentence or two about it, so there is one extractor with
per-sender tuning rather than five parsers implying five kinds of knowledge we
do not have. The tuning knobs are named and each is there because real mail
required it:

  * TLDR bolds a headline link and suffixes it "(4 minute read)".
  * The Rundown and The Neuron are beehiiv and bold their headline links too,
    but their hrefs are encrypted, so the DESTINATION comes from the plain-text
    half of the same message (`plain_text_destinations`).
  * Milk Road is beehiiv as well and writes `<h1>` headlines with no link on
    them at all, so a heading opens a story there and the story ships linkless
    (`headings_start_stories`).
  * Ben's Bites writes a list of plain links with short trailing blurbs.

**Everything here was measured against real mail on 2026-08-28**, four issues
per sender, and the fixtures are those messages with the identifiers scrubbed
out. The previous version of this file was tuned against hand-written
reconstructions, and the first live run showed what that is worth: 15 real TLDR
messages produced 0 stories, and the three beehiiv senders dropped 100% of
their links.

**HTML is data, never instructions.** These messages come from outside. They
are parsed with `html.parser`, reduced to anchors and text, and nothing inside
them is executed, followed, or obeyed. Nothing extracted is rendered as HTML by
this codebase; the renderer escapes it.

**Links go through the sanitizer, always.** `extract()` is the only public
entry point and it sanitizes every link before a story leaves this module.
`Story.url_raw` exists so the sanitizer has something to work on and must never
reach a page, a log, or the state file.

Boilerplate (unsubscribe, view in browser, sponsor, referral, social icons) is
dropped with simple heuristics: a word list, an anchor-text length floor, a
leading-imperative check for calls to action, and a requirement that a headline
link be emphasized where the sender emphasizes its headlines. These are
heuristics against four issues per sender from one week, so the measured hit
rate stays a per-run OUTPUT, not a promise made here. Senders redesign.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser
from typing import Callable

from ..normalize import clean_title
from .sanitize import sanitize

MAX_BLURB_CHARS = 600
MIN_TITLE_CHARS = 15
MAX_TITLE_CHARS = 200
# A text run shorter than this, arriving after a blurb has started, is read as
# the next section heading rather than more blurb.
SECTION_HEADING_CHARS = 40

# Anchor text or blurb text containing one of these is furniture, not a story.
BOILERPLATE = (
    "unsubscribe",
    "view in browser",
    "view this email",
    "view online",
    "read online",
    "manage your subscription",
    "manage preferences",
    "update your preferences",
    "email preferences",
    "privacy policy",
    "terms of service",
    "forward this email",
    "refer a friend",
    "referral",
    "share this",
    "advertise",
    "advertisement",
    "sponsored",
    "sponsor",
    "presented by",
    "together with",
    "click here",
    "sign up",
    "subscribe",
    "download the app",
    "follow us",
    "add us to your address book",
    "was this email forwarded",
    "disclaimer",
)

# A headline is a statement; these are invitations. Real mail from the three
# beehiiv senders ends every issue with a run of them ("Join The Rundown Tech",
# "Check out ours here", "Lock in $250/year - yours for life"), and they were
# the ONLY things Milk Road's adapter extracted, so its whole section would have
# been advertising. Matched as a PREFIX, not a substring, because a real
# headline can contain any of these words in the middle of a sentence.
CTA_PREFIXES = (
    "join the",
    "join our",
    "join us",
    "check out",
    "lock in",
    "grab your",
    "claim your",
    "get your",
)

# Tags whose CONTENT is not readable text. Every one of these is a container
# with a closing tag, and that is load-bearing: `handle_starttag` opens a skip
# scope that only `handle_endtag` can close. A VOID element here would open a
# scope that never closes and silently swallow the whole rest of the document.
#
# That is not hypothetical. `meta` and `link` used to be in this set, and real
# TLDR mail writes `<meta ...>` unslashed (six times, in the head). Six skip
# scopes opened, none ever closed, and `read_blocks` returned an empty list for
# a 60KB newsletter: 15 real messages in, 0 stories out. The three beehiiv
# senders happened to write `<meta ... />`, which `html.parser` routes to
# `handle_startendtag` instead, so they parsed fine and the bug looked
# sender-specific rather than structural. Void tags are now simply ignored:
# they have no content to skip.
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
# Elements with no closing tag. Listed so a future edit to `_SKIP_TAGS` cannot
# reintroduce the never-closing-scope bug, and asserted in the tests.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
# Tags that mean "this anchor is a headline" for senders that emphasize theirs.
_EMPHASIS_TAGS = frozenset({"h1", "h2", "h3", "h4", "strong", "b"})

# An address written in the newsletter's PROSE, which the sanitizer never sees.
# `sanitize` guards URLs and only URLs, so a title or blurb was an unguarded
# channel straight to a public page. The first live run proved it is not
# hypothetical: TLDR's referral line ("send us a resume at <address>") reached
# the artifact verbatim. That particular address is TLDR's own public jobs
# inbox, so nothing was lost this time, but "You are subscribed as <address>" is
# the same sentence shape and it is the reader's identity. Prose is redacted
# rather than trusted, for the same reason the query-string gate is an allowlist.
#
# The separator is an alternation rather than a bare `@` because two encodings
# of the same character reach prose intact. `＠` (U+FF20, fullwidth) survives a
# CJK-aware mailer and any copy-paste out of one, and `%40` / `%2540` survive a
# newsletter that pastes a URL-encoded address into its own body text (the
# double-encoded form is the same shape `sanitize.looks_like_address` already
# has to defeat inside URLs).
#
# DELIBERATELY OUT OF SCOPE, and named so nobody reads the gap as an oversight:
#   * IDN / non-ASCII local parts and domains (`用户@例子.公司`, `joyd@例子.公司`).
#     Allowing non-ASCII on both sides of the separator turns this regex into a
#     matcher for ordinary CJK prose containing an at-sign, and none of the five
#     allowlisted senders is a CJK newsletter. It stays ASCII on both halves.
#   * The spelled-out separators `(at)` / `[at]` / ` at `. The realistic carrier
#     of a subscriber address is a mailer's own "you are subscribed as X"
#     footer, which is machine-written and always uses a literal at-sign. A
#     human writing `(at)` is obfuscating on purpose and is not the threat.
_ADDRESS_SEPARATOR = r"(?:@|＠|%2540|%40)"
_ADDRESS_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+-]+" + _ADDRESS_SEPARATOR + r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
ADDRESS_PLACEHOLDER = "[address removed]"

_READ_TIME = re.compile(r"\s*\(\s*\d+\s*minute\s+read\s*\)\s*$", re.I)
_TRAILING_TAG = re.compile(r"\s*\(\s*(sponsor|sponsored|ad)\s*\)\s*$", re.I)
_BARE_URL = re.compile(r"^https?://", re.I)


@dataclass
class Story:
    """One extracted story. `url_raw` never leaves this module."""

    title: str
    url_raw: str
    blurb: str = ""
    url: str = ""  # sanitized publisher URL, empty when none could be recovered


@dataclass
class HitReport:
    """What one message yielded, so the lane can report a real hit rate."""

    stories_found: int = 0
    links_sanitized: int = 0
    links_dropped: int = 0


@dataclass
class ParseResult:
    stories: list[Story] = field(default_factory=list)
    report: HitReport = field(default_factory=HitReport)


# --------------------------------------------------------------------------
# MIME
# --------------------------------------------------------------------------

def html_body(msg: Message) -> str:
    """The first readable text/html part, decoded, wherever it is nested.

    Covers the MIME realities the design doc names: multipart/alternative with
    both parts, HTML-only, base64 or quoted-printable transfer encoding, and
    multipart nested inside multipart. `walk()` handles the nesting; attachments
    are skipped so a forwarded .html file cannot become the newsletter.
    """
    return _first_part(msg, "text/html")


def text_body(msg: Message) -> str:
    """The plain-text alternative. Used only to tell an empty mail from a broken one."""
    return _first_part(msg, "text/plain")


def _first_part(msg: Message, content_type: str) -> str:
    for part in msg.walk():
        if part.get_content_type() != content_type:
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")
    return ""


def sender_address(msg: Message) -> str:
    """The From address, lowercased. Public sending address, not a subscriber."""
    from email.utils import parseaddr

    return (parseaddr(str(msg.get("From") or ""))[1] or "").strip().lower()


# --------------------------------------------------------------------------
# who actually sent it
# --------------------------------------------------------------------------

# `dkim=pass header.d=example.com` inside an Authentication-Results header.
# The two halves are matched separately because a real header interleaves
# several methods (spf, dkim, dmarc) with their own parameters, in any order.
_AUTH_DKIM = re.compile(r"\bdkim\s*=\s*(?P<verdict>[a-z]+)", re.I)
# `header.d` is the signing domain directly. `header.i` is the AUID, which is
# written either as `@example.com` or as `user@example.com`; the optional
# local-part group is what stops the second form capturing `user` as a domain.
# (Round 2, R2-S4: the old combined `header.(d|i)` regex did exactly that. It
# failed closed, so nothing was exploitable, but half of it did not work.)
_AUTH_HEADER_D = re.compile(r"\bheader\.d\s*=\s*@?(?P<domain>[A-Za-z0-9.\-]+)", re.I)
_AUTH_HEADER_I = re.compile(
    r"\bheader\.i\s*=\s*(?:[^@\s;]*@)?@?(?P<domain>[A-Za-z0-9.\-]+)", re.I
)
# Comments in a header are RFC 5322 `(...)` runs. Gmail writes one right after
# the authserv-id on some verdicts, so the id has to be read past them.
_AUTH_COMMENT = re.compile(r"\([^()]*\)")

# WHOSE verdict counts. RFC 8601 §2.2: the first token of an
# Authentication-Results header is the authserv-id, the identity of the ADMD
# that performed the check and wrote the header. A receiving server strips only
# the headers bearing its OWN id (§5) and passes foreign ones through, because
# relayed mail legitimately carries them. So "is there a dkim=pass anywhere in
# the header set" is not a check at all: the sender writes their own header,
# Gmail forwards it, and the forgery reads as a pass.
#
# This is the default, not the rule. The rule is `authserv_id`, which flows in
# from the lane's config (`newsletter.authserv_id` in sources.yaml), because a
# mailbox that is not Gmail has a different receiving server and changing that
# must not mean editing this file.
DEFAULT_AUTHSERV_ID = "mx.google.com"

AUTH_PASS = "pass"  # DKIM passed and signed a domain the adapter allows
AUTH_FAIL = "fail"  # the trusted server's header does not authorise this sender
AUTH_MISSING = "missing"  # no Authentication-Results header from the trusted id


def authserv_id(header: str) -> str:
    """The authserv-id of one Authentication-Results header, lowercased.

    Everything before the first `;`, with comments and any trailing version
    number removed and surrounding quotes stripped. Untrusted input: a header
    that is empty, malformed, or has no id at all yields the empty string,
    which matches no configured id and therefore counts for nothing.
    """
    head = _AUTH_COMMENT.sub(" ", str(header or "").split(";", 1)[0])
    tokens = head.strip().split()
    if not tokens:
        return ""
    return tokens[0].strip('"').strip().lower()


def trusted_auth_header(msg: Message, *, trusted_id: str = DEFAULT_AUTHSERV_ID) -> str | None:
    """The TOPMOST Authentication-Results header written by the trusted server.

    Topmost, not "any matching one", and that is the half of the fix that
    defeats the self-supplied header claiming to be `mx.google.com`: the
    receiving server prepends its own header, so its verdict is always above
    anything that arrived with the message. `get_all` preserves header order,
    so the first match here is the topmost one in the message.

    None means the trusted server wrote nothing, which is AUTH_MISSING and is
    a refusal, not an absence of evidence to be shrugged at.
    """
    want = (trusted_id or "").strip().strip('"').lower()
    if not want:
        return None
    for raw in msg.get_all("Authentication-Results") or []:
        if authserv_id(raw) == want:
            return str(raw)
    return None


def dkim_results(
    msg: Message, *, trusted_id: str = DEFAULT_AUTHSERV_ID
) -> list[tuple[str, str]]:
    """Every `(verdict, signing domain)` pair the TRUSTED server wrote.

    Scoped to one header on purpose. Reading every Authentication-Results
    header in the message is what made this control forgeable in round 2: an
    attacker sends their own `Authentication-Results: mx.evil.example;
    dkim=pass header.d=tldrnewsletter.com`, Gmail passes the foreign header
    through untouched, and a scan of the whole set finds the attacker's pass.
    Malformed values become empty strings rather than exceptions: this is
    untrusted input.
    """
    header = trusted_auth_header(msg, trusted_id=trusted_id)
    if header is None:
        return []
    out: list[tuple[str, str]] = []
    # One header can carry several methods. Split on `;` so a `dkim=pass`
    # in one clause is not paired with a `header.d` from another.
    for clause in header.split(";"):
        verdict = _AUTH_DKIM.search(clause)
        if not verdict:
            continue
        domain = _AUTH_HEADER_D.search(clause) or _AUTH_HEADER_I.search(clause)
        out.append((
            verdict.group("verdict").lower(),
            (domain.group("domain").lower().strip(".") if domain else ""),
        ))
    return out


def authentication(
    msg: Message, adapter: "Adapter", *, trusted_id: str = DEFAULT_AUTHSERV_ID
) -> str:
    """AUTH_PASS, AUTH_FAIL or AUTH_MISSING for this message and adapter.

    The gap this closes: Gmail's `from:` search operator matches the From
    HEADER, which anyone can write. Without this check, someone who learns the
    newsletter account's address could put arbitrary headlines and links on a
    public page under a trusted newsletter's name. No XSS needed; the content
    itself is the payload.

    Two conditions, both required, and the first one is the one round 2 was
    missing: the verdict must come from the TOPMOST header the trusted
    receiving server wrote (`trusted_id`), and within that header a `dkim=pass`
    must sign a domain the adapter allows. A header from any other authserv-id
    is ignored entirely, however convincing its contents.

    **Fail-closed, including on a missing header.** Gmail stamps
    Authentication-Results on delivery, so every real message should carry one,
    and the first live run confirmed it on 23 real messages. The missing case
    keeps its OWN counter rather than being folded into the failures: if mail
    turns up with no header from the trusted server, the lane's status line
    says so instead of the run silently reading as clean.
    """
    if trusted_auth_header(msg, trusted_id=trusted_id) is None:
        return AUTH_MISSING
    for verdict, domain in dkim_results(msg, trusted_id=trusted_id):
        if verdict == "pass" and domain and adapter.allows_domain(domain):
            return AUTH_PASS
    # The trusted server spoke and did not authorise this sender. A header that
    # carries no DKIM clause at all lands here too: present, and not a pass.
    return AUTH_FAIL


# --------------------------------------------------------------------------
# HTML -> blocks
# --------------------------------------------------------------------------

@dataclass
class _Link:
    href: str
    text: str
    emphasized: bool


@dataclass
class _Heading:
    """A heading that contains no link of its own.

    Milk Road needs this and nothing else does. Its stories are `<h1>` headings
    followed by prose, with no anchor anywhere near them: real mail was checked
    and the ONLY long anchors in a Milk Road issue are the sponsor button, "Read
    full disclaimer" and "Sponsor Milk Road". An anchor-driven extractor reads
    that issue as three advertisements and no news.

    A heading that wraps an anchor is NOT one of these. The anchor is emitted as
    a `_Link` as usual and the heading closes with an empty buffer, so the four
    senders that link their headlines are untouched.
    """

    text: str


class _BlockReader(HTMLParser):
    """Reduce a newsletter to an ordered stream of links and text runs.

    Nothing else survives: no tags, no attributes other than `href`, no
    scripts. Whatever the sender put in the document, what leaves here is a
    list of strings.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Link | _Heading | str] = []
        self._skip_depth = 0
        self._emphasis_depth = 0
        self._anchor: _Link | None = None
        self._buffer: list[str] = []

    # -- helpers
    def _flush_text(self, *, as_heading: bool = False) -> None:
        text = " ".join(self._buffer).strip()
        self._buffer = []
        if text:
            self.blocks.append(_Heading(text=text) if as_heading else text)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _EMPHASIS_TAGS:
            self._emphasis_depth += 1
            # Both shapes are common in the wild: <strong><a>..</a></strong>
            # and <a><strong>..</strong></a>. Emphasis opening INSIDE an open
            # anchor counts too, or half the headlines read as plain links.
            if self._anchor is not None:
                self._anchor.emphasized = True
        if tag == "a":
            href = ""
            for name, value in attrs:
                if name.lower() == "href" and value:
                    href = value.strip()
                    break
            self._flush_text()
            self._anchor = _Link(href=href, text="", emphasized=self._emphasis_depth > 0)
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._buffer.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self._skip_depth:
            self._buffer.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _EMPHASIS_TAGS:
            self._emphasis_depth = max(0, self._emphasis_depth - 1)
        if tag == "a" and self._anchor is not None:
            anchor = self._anchor
            anchor.text = " ".join(self._buffer).strip()
            self._buffer = []
            self._anchor = None
            self.blocks.append(anchor)
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush_text(as_heading=True)
        elif tag in ("p", "div", "tr", "li", "table"):
            self._flush_text()

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        self._buffer.append(data)

    def close(self):
        super().close()
        if self._anchor is not None:
            self._anchor.text = " ".join(self._buffer).strip()
            self.blocks.append(self._anchor)
            self._anchor = None
            self._buffer = []
        self._flush_text()


def read_blocks(html: str) -> list[_Link | _Heading | str]:
    reader = _BlockReader()
    try:
        reader.feed(html or "")
        reader.close()
    except Exception:
        # A malformed newsletter yields whatever was parsed before it broke.
        # It is untrusted input; it does not get to raise.
        pass
    return reader.blocks


# --------------------------------------------------------------------------
# blocks -> stories
# --------------------------------------------------------------------------

def is_boilerplate(text: str) -> bool:
    folded = (text or "").casefold()
    return any(word in folded for word in BOILERPLATE)


def is_promo_headline(title: str, *, shouting_is_promo: bool = True) -> bool:
    """A call to action or an advertising banner wearing a headline's clothes.

    Two shapes, both taken from real mail rather than imagined. A leading
    imperative ("Join The Rundown Tech") is an invitation, not news, and every
    issue of all three beehiiv senders ends with a run of them.

    The second shape is shouting, and it is why the caller gets a switch. An
    emphasized ANCHOR in block capitals is a sponsor's button: "FREE SEMINAR ON
    BLOCKCHAIN & PRIVATE MARKETS" is an ad, and it was one of only three things
    Milk Road's adapter could find. A HEADING in block capitals is a different
    thing entirely, because Milk Road writes its own headlines that way. So the
    rule is applied where it discriminates and switched off where it would
    delete the sender's actual news.
    """
    folded = (title or "").casefold().lstrip(" >»-–—")
    if any(folded.startswith(prefix) for prefix in CTA_PREFIXES):
        return True
    if not shouting_is_promo:
        return False
    letters = [c for c in (title or "") if c.isalpha()]
    return len(letters) >= 3 and not any(c.islower() for c in letters)


def redact_addresses(text: str) -> str:
    """Take every email address out of prose bound for a public page."""
    return _ADDRESS_IN_TEXT.sub(ADDRESS_PLACEHOLDER, text or "")


def _clean_headline(raw: str) -> str:
    title = clean_title(raw)
    title = _READ_TIME.sub("", title)
    title = _TRAILING_TAG.sub("", title)
    return redact_addresses(title.strip(" –—-:·|").strip())


def extract_stories(
    html: str,
    *,
    require_emphasis: bool = True,
    min_title: int = MIN_TITLE_CHARS,
    max_title: int = MAX_TITLE_CHARS,
    headings_start_stories: bool = False,
) -> list[Story]:
    """Anchor + following text, filtered down to things that look like stories.

    One shape fits four of the five senders because four of them write the same
    shape: a link that is the headline, followed by the newsletter's own
    sentence or two about it. The tuning knobs are which of them bold their
    headlines and how short a headline is allowed to be.

    `headings_start_stories` is the fifth. Milk Road writes `<h1>` headlines
    with no link on them at all, so for that sender a heading opens a story the
    same way an anchor does, and the story ships with no URL. That is a normal
    outcome here: `sanitize` refuses far more links than it keeps, and the
    renderer already handles a story with nothing to link to. It is OFF by
    default because the other four use headings as section labels ("Big Tech &
    Startups"), and turning it on for them would publish the table of contents.
    """
    blocks = read_blocks(html)
    stories: list[Story] = []
    seen_hrefs: set[str] = set()
    current: Story | None = None
    from_heading = False
    blurb_parts: list[str] = []
    blurb_closed = False

    def close_current() -> None:
        nonlocal current, from_heading, blurb_parts, blurb_closed
        if current is not None:
            blurb = " ".join(part for part in blurb_parts if part).strip()
            # Leading punctuation is the separator the sender used between the
            # headline link and its sentence ("Title - the blurb"), not content.
            blurb = clean_title(blurb).lstrip(" -–—:·|").strip()
            current.blurb = redact_addresses(blurb)[:MAX_BLURB_CHARS].strip()
            # A heading with no prose under it and no link on it ("RATE TODAY'S
            # EDITION") is a section label, and it is indistinguishable from a
            # story until the block after it turns out to be another heading.
            # An anchor story is kept either way: it still carries a URL.
            if not (from_heading and not current.blurb):
                stories.append(current)
        current = None
        from_heading = False
        blurb_parts = []
        blurb_closed = False

    for block in blocks:
        if isinstance(block, _Heading) and headings_start_stories:
            title = _clean_headline(block.text)
            if (
                min_title <= len(title) <= max_title
                and not is_boilerplate(block.text)
                # Milk Road's headlines are in block capitals, so shouting is
                # this sender's voice and cannot be the tell for an ad here.
                and not is_promo_headline(title, shouting_is_promo=False)
            ):
                close_current()
                current = Story(title=title, url_raw="")
                from_heading = True
                continue
            # A heading that does not qualify is a section label; it ends the
            # blurb of whatever story it follows rather than joining it.
            blurb_closed = current is not None
            continue

        if isinstance(block, (str, _Heading)):
            text = block if isinstance(block, str) else block.text
            if current is None or blurb_closed or is_boilerplate(text):
                continue
            # A SHORT text run after the blurb has started is the next section
            # heading ("Research & Engineering"), not more of this blurb.
            if blurb_parts and len(text) < SECTION_HEADING_CHARS:
                blurb_closed = True
                continue
            blurb_parts.append(text)
            continue

        title = _clean_headline(block.text)
        qualifies = (
            bool(block.href)
            and not _BARE_URL.match(title)
            and min_title <= len(title) <= max_title
            and not is_boilerplate(block.text)
            and not is_promo_headline(title)
            and (block.emphasized or not require_emphasis)
        )
        if not qualifies:
            # An in-blurb link ("their announcement") is not a new story and
            # its text is still part of the current blurb.
            if current is not None and not blurb_closed and block.text and not is_boilerplate(block.text):
                blurb_parts.append(clean_title(block.text))
            continue

        close_current()
        if block.href in seen_hrefs:
            continue
        seen_hrefs.add(block.href)
        current = Story(title=title, url_raw=block.href)
        from_heading = False

    close_current()
    return stories


# --------------------------------------------------------------------------
# the other copy of the same message
# --------------------------------------------------------------------------

# `[label](https://destination)` in the text/plain alternative. The label is
# bounded so a stray `[` in prose cannot make the match run to the end of a
# 17KB body.
_MD_LINK = re.compile(r"\[(?P<label>[^\]\[\n]{1,300}?)\]\(\s*(?P<url>https?://[^)\s]+)\s*\)")
_MD_MARKS = re.compile(r"[*_`~]+")
# Two labels only count as the same story when the match key is long enough to
# be a headline rather than "read more".
MIN_MATCH_KEY = 20


def _match_key(text: str) -> str:
    """Fold a headline down to what survives HTML-vs-markdown rendering."""
    return re.sub(r"[^a-z0-9]+", " ", _MD_MARKS.sub("", text or "").lower()).strip()


def plain_text_destinations(msg: Message) -> dict[str, str]:
    """`headline -> destination`, read from the message's OWN plain-text part.

    Why this exists: beehiiv's HTML hrefs are `link.mail.beehiiv.com/ss/c/u001.<blob>/...`
    and the blob is encrypted, not encoded. Real mail was measured: the blob is
    240 to 411 characters of base64url, and decoding it yields ~40% printable
    bytes with no URL anywhere inside. There is nothing in that href to recover,
    which is why the first live run dropped 100% of these links.

    The destination is nonetheless already in the message, in the OTHER half of
    the same `multipart/alternative`: beehiiv renders the plain-text copy as
    markdown, and its `[label](url)` links carry the real publisher URL rather
    than the tracked one. Reading it is offline decoding of mail we already
    have. No request is made, so the tracker still learns nothing, which is the
    whole point of the static-only rule in `sanitize`.

    A label that appears twice with DIFFERENT destinations is dropped rather
    than guessed at: pointing a headline at the wrong article is a worse
    failure than shipping it linkless.
    """
    table: dict[str, str] = {}
    ambiguous: set[str] = set()
    for match in _MD_LINK.finditer(text_body(msg)):
        key = _match_key(match.group("label"))
        if not key:
            continue
        url = match.group("url")
        if key in table and table[key] != url:
            ambiguous.add(key)
        table.setdefault(key, url)
    for key in ambiguous:
        table.pop(key, None)
    return table


def recover_destinations(msg: Message, stories: list[Story]) -> list[Story]:
    """Swap in a plain-text destination for any link the sanitizer cannot use.

    Conservative on purpose, in two ways. It only fires when the HTML href is
    unrecoverable, so a sender that already ships a usable link keeps it. And
    the swapped-in URL still goes through `sanitize()` afterwards like every
    other link, so this widens what can be RECOVERED without widening what is
    allowed to be PUBLISHED.
    """
    table = plain_text_destinations(msg)
    if not table:
        return stories
    for story in stories:
        if sanitize(story.url_raw):
            continue
        key = _match_key(story.title)
        found = table.get(key)
        if found is None and len(key) >= MIN_MATCH_KEY:
            # The HTML headline and the markdown label are the same sentence
            # cut at different lengths ("...(4 minute read)", a trailing emoji),
            # so a containment match on a key this long is the same story.
            for label, url in table.items():
                if len(label) >= MIN_MATCH_KEY and (label.startswith(key) or key.startswith(label)):
                    found = url
                    break
        if found:
            story.url_raw = found
    return stories


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------

def _milkroad_stories(msg: Message) -> list[Story]:
    """Milk Road: `<h1>` headlines with no link, followed by prose.

    Same beehiiv chassis as the other two, and a different editorial shape on
    top of it. Real mail was checked: a Milk Road issue contains no anchor that
    is a headline, so the shared beehiiv parse returns the sponsor button and
    the disclaimer link and nothing else. These stories ship without URLs
    because Milk Road writes its own copy and does not link out per story.
    """
    stories = extract_stories(
        html_body(msg), require_emphasis=True, min_title=15, headings_start_stories=True,
    )
    return recover_destinations(msg, stories)


def _beehiiv_stories(msg: Message) -> list[Story]:
    """Shared by the two beehiiv senders that link their headlines.

    The links come from the plain-text half of the message, because the HTML
    half does not contain them in any recoverable form. See
    `plain_text_destinations`.
    """
    stories = extract_stories(html_body(msg), require_emphasis=True, min_title=15)
    return recover_destinations(msg, stories)


def _tldr_stories(msg: Message) -> list[Story]:
    """TLDR: bold headline link, "(N minute read)" suffix, blurb paragraph."""
    return extract_stories(html_body(msg), require_emphasis=True, min_title=12)


def _bensbites_stories(msg: Message) -> list[Story]:
    """Ben's Bites: a list of plain links, each with a short trailing blurb."""
    return extract_stories(html_body(msg), require_emphasis=False, min_title=18)


@dataclass(frozen=True)
class Adapter:
    """One allowlisted sender. `senders` are PUBLIC sending addresses."""

    id: str
    name: str
    senders: tuple[str, ...]
    parse: Callable[[Message], list[Story]]

    def allows_domain(self, domain: str) -> bool:
        """Is this domain the adapter's, or a subdomain of it?

        Suffix matching at a DOT BOUNDARY, which is the only kind that is safe.
        The live inbox needs it: The Neuron sends from
        `newsletter.theneurondaily.com` and Milk Road from `mail.milkroad.com`,
        neither of which is the bare allowlisted domain. `evilmilkroad.com`
        must not match `milkroad.com`, and the `"." +` is what enforces that.
        """
        domain = (domain or "").strip().lower().strip(".")
        if not domain:
            return False
        for entry in self.senders:
            entry = entry.lower()
            if "@" in entry:
                entry = entry.rpartition("@")[2]
            if domain == entry or domain.endswith("." + entry):
                return True
        return False

    def matches(self, address: str) -> bool:
        """Does this From address belong to this adapter?

        An exact address entry matches only itself; a bare domain entry matches
        that domain and its subdomains.
        """
        address = (address or "").strip().lower()
        if not address or "@" not in address:
            return False
        domain = address.rpartition("@")[2]
        for entry in self.senders:
            entry = entry.lower()
            if "@" in entry:
                if address == entry:
                    return True
                continue
            if domain == entry or domain.endswith("." + entry):
                return True
        return False

    def extract(self, msg: Message) -> ParseResult:
        """Parse, then sanitize every link. The only public entry point."""
        report = HitReport()
        kept: list[Story] = []
        for story in self.parse(msg):
            report.stories_found += 1
            clean = sanitize(story.url_raw) if story.url_raw else None
            if clean:
                story.url = clean
                report.links_sanitized += 1
            else:
                story.url = ""
                report.links_dropped += 1
            kept.append(story)
        return ParseResult(stories=kept, report=report)


# Which of these are LIVE, re-measured against the mailbox on 2026-08-28 with
# four real issues per sender. FOUR are live, not three: `therundown` was
# previously recorded as having no mail, and it does, from `daily.therundown.ai`
# rather than the `mail.therundown.ai` that had been guessed at. The four live
# sending hosts are `tldrnewsletter.com`, `daily.therundown.ai`,
# `newsletter.theneurondaily.com` and `mail.milkroad.com`: three of the four are
# subdomains, which is why suffix matching at a dot boundary is load-bearing
# rather than defensive.
#
# `bensbites` is the one adapter with no real message behind it. Its fixture is
# still a hand-written reconstruction and its format stays grade C until a real
# issue has been through this parser, which is exactly the grade TLDR carried
# while it was silently returning nothing.
ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        id="tldr",
        name="TLDR",
        senders=("tldrnewsletter.com", "tldr.tech", "dan@tldrnewsletter.com"),
        parse=_tldr_stories,
    ),
    Adapter(
        id="therundown",
        name="The Rundown AI",
        # `daily.therundown.ai` is the live sending host, confirmed 2026-08-28.
        # The bare domain already covers it via suffix matching; it is named
        # here so the Gmail `from:` query says the host that actually sends.
        senders=("rundown.ai", "daily.therundown.ai", "mail.therundown.ai", "therundown.ai"),
        parse=_beehiiv_stories,
    ),
    Adapter(
        id="bensbites",
        name="Ben's Bites",
        senders=("bensbites.co", "mail.bensbites.co", "bensbites.com"),
        parse=_bensbites_stories,
    ),
    Adapter(
        id="theneuron",
        name="The Neuron",
        # The bare domain is what `matches()` needs; the subdomain is listed as
        # well because `sender_queries()` turns each entry into a Gmail `from:`
        # term, and naming the live sending host there is not worth guessing at.
        senders=("theneurondaily.com", "newsletter.theneurondaily.com",
                 "mail.theneurondaily.com"),
        parse=_beehiiv_stories,
    ),
    Adapter(
        id="milkroad",
        name="Milk Road",
        senders=("milkroad.com", "mail.milkroad.com"),  # live sender: the subdomain
        parse=_milkroad_stories,
    ),
)

ADAPTER_IDS = tuple(a.id for a in ADAPTERS)


def by_id(adapter_id: str) -> Adapter | None:
    for adapter in ADAPTERS:
        if adapter.id == adapter_id:
            return adapter
    return None


def for_sender(address: str) -> Adapter | None:
    for adapter in ADAPTERS:
        if adapter.matches(address):
            return adapter
    return None


def sender_queries(adapter_ids: list[str] | None = None) -> list[str]:
    """The `from:` terms for the Gmail query, for the enabled adapters only."""
    wanted = ADAPTERS if not adapter_ids else [a for a in ADAPTERS if a.id in set(adapter_ids)]
    out: list[str] = []
    for adapter in wanted:
        for entry in adapter.senders:
            if entry not in out:
                out.append(entry)
    return out
