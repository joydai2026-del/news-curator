"""The allowlist: five named newsletters, one small adapter each.

The honest contract from the design doc: **generic newsletter parsing does not
work**. Every sender lays its stories out differently, and a parser that tries
to be universal produces a page of navigation links and unsubscribe footers. So
v1 names five senders, gives each an adapter, and REPORTS what each one
extracted. A sender in the allowlist whose adapter found nothing is listed as a
pending adapter, never silently dropped, because "we shipped and the section is
empty" is the failure this rule exists to catch.

**The five share one extractor on purpose.** Three of them (The Rundown, The
Neuron, Milk Road) are built on beehiiv and emit the same block structure, and
the remaining two differ from it in small, nameable ways (TLDR bolds a headline
link and follows it with a paragraph and a "(4 minute read)" suffix; Ben's
Bites writes a list of links with short trailing blurbs). Writing five separate
parsers would imply five kinds of knowledge we do not have. What we have is one
extractor with per-sender tuning, plus a `senders` allowlist, and each adapter
says which it is.

**HTML is data, never instructions.** These messages come from outside. They
are parsed with `html.parser`, reduced to anchors and text, and nothing inside
them is executed, followed, or obeyed. Nothing extracted is rendered as HTML by
this codebase; the renderer escapes it.

**Links go through the sanitizer, always.** `extract()` is the only public
entry point and it sanitizes every link before a story leaves this module.
`Story.url_raw` exists so the sanitizer has something to work on and must never
reach a page, a log, or the state file.

Boilerplate (unsubscribe, view in browser, sponsor, referral, social icons) is
dropped with simple heuristics: a word list, an anchor-text length floor, and a
requirement that a headline link be emphasized where the sender emphasizes its
headlines. These are heuristics against a synthetic fixture, so the measured
hit rate is a per-run OUTPUT, not a promise made here.
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
)

# Tags whose content is not readable text.
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "meta", "link"})
# Tags that mean "this anchor is a headline" for senders that emphasize theirs.
_EMPHASIS_TAGS = frozenset({"h1", "h2", "h3", "h4", "strong", "b"})

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
_AUTH_DOMAIN = re.compile(r"\bheader\.(?:d|i)\s*=\s*@?(?P<domain>[A-Za-z0-9.\-]+)", re.I)

AUTH_PASS = "pass"  # DKIM passed and signed a domain the adapter allows
AUTH_FAIL = "fail"  # a header is present and it does not authorise this sender
AUTH_MISSING = "missing"  # no Authentication-Results header at all


def dkim_results(msg: Message) -> list[tuple[str, str]]:
    """Every `(verdict, signing domain)` pair in the Authentication-Results set.

    A message can carry several of these headers; the mail server writes them
    on delivery and a sender cannot forge the one the RECEIVING server wrote,
    which is the whole reason this is worth reading. Malformed values become
    empty strings rather than exceptions: this is untrusted input.
    """
    out: list[tuple[str, str]] = []
    for raw in msg.get_all("Authentication-Results") or []:
        text = str(raw or "")
        # One header can carry several methods. Split on `;` so a `dkim=pass`
        # in one clause is not paired with a `header.d` from another.
        for clause in text.split(";"):
            verdict = _AUTH_DKIM.search(clause)
            if not verdict:
                continue
            domain = _AUTH_DOMAIN.search(clause)
            out.append((
                verdict.group("verdict").lower(),
                (domain.group("domain").lower().strip(".") if domain else ""),
            ))
    return out


def authentication(msg: Message, adapter: "Adapter") -> str:
    """AUTH_PASS, AUTH_FAIL or AUTH_MISSING for this message and adapter.

    The gap this closes: Gmail's `from:` search operator matches the From
    HEADER, which anyone can write. Without this check, someone who learns the
    newsletter account's address could put arbitrary headlines and links on a
    public page under a trusted newsletter's name. No XSS needed; the content
    itself is the payload.

    **Fail-closed, including on a missing header, and graded C until a live
    run says otherwise.** Gmail stamps Authentication-Results on delivery, so
    every real message should carry one, and the fixtures assert the shape.
    But no message from the real mailbox has been through this code yet. The
    missing case therefore gets its OWN counter rather than being folded into
    the failures: if real mail turns up without the header, the lane's status
    line says so on the first run instead of silently reading as empty.
    """
    results = dkim_results(msg)
    if not results:
        return AUTH_MISSING
    for verdict, domain in results:
        if verdict == "pass" and domain and adapter.allows_domain(domain):
            return AUTH_PASS
    return AUTH_FAIL


# --------------------------------------------------------------------------
# HTML -> blocks
# --------------------------------------------------------------------------

@dataclass
class _Link:
    href: str
    text: str
    emphasized: bool


class _BlockReader(HTMLParser):
    """Reduce a newsletter to an ordered stream of links and text runs.

    Nothing else survives: no tags, no attributes other than `href`, no
    scripts. Whatever the sender put in the document, what leaves here is a
    list of strings.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Link | str] = []
        self._skip_depth = 0
        self._emphasis_depth = 0
        self._anchor: _Link | None = None
        self._buffer: list[str] = []

    # -- helpers
    def _flush_text(self) -> None:
        text = " ".join(self._buffer).strip()
        self._buffer = []
        if text:
            self.blocks.append(text)

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
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"):
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


def read_blocks(html: str) -> list[_Link | str]:
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


def _clean_headline(raw: str) -> str:
    title = clean_title(raw)
    title = _READ_TIME.sub("", title)
    title = _TRAILING_TAG.sub("", title)
    return title.strip(" –—-:·|").strip()


def extract_stories(
    html: str,
    *,
    require_emphasis: bool = True,
    min_title: int = MIN_TITLE_CHARS,
    max_title: int = MAX_TITLE_CHARS,
) -> list[Story]:
    """Anchor + following text, filtered down to things that look like stories.

    One shape fits all five senders because all five write the same shape: a
    link that is the headline, followed by the newsletter's own sentence or two
    about it. The tuning knobs are which of them bold their headlines and how
    short a headline is allowed to be.
    """
    blocks = read_blocks(html)
    stories: list[Story] = []
    seen_hrefs: set[str] = set()
    current: Story | None = None
    blurb_parts: list[str] = []
    blurb_closed = False

    def close_current() -> None:
        nonlocal current, blurb_parts, blurb_closed
        if current is not None:
            blurb = " ".join(part for part in blurb_parts if part).strip()
            # Leading punctuation is the separator the sender used between the
            # headline link and its sentence ("Title - the blurb"), not content.
            blurb = clean_title(blurb).lstrip(" -–—:·|").strip()
            current.blurb = blurb[:MAX_BLURB_CHARS].strip()
            stories.append(current)
        current = None
        blurb_parts = []
        blurb_closed = False

    for block in blocks:
        if isinstance(block, str):
            if current is None or blurb_closed or is_boilerplate(block):
                continue
            # A SHORT text run after the blurb has started is the next section
            # heading ("Research & Engineering"), not more of this blurb.
            if blurb_parts and len(block) < SECTION_HEADING_CHARS:
                blurb_closed = True
                continue
            blurb_parts.append(block)
            continue

        title = _clean_headline(block.text)
        qualifies = (
            bool(block.href)
            and not _BARE_URL.match(title)
            and min_title <= len(title) <= max_title
            and not is_boilerplate(block.text)
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

    close_current()
    return stories


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------

def _beehiiv_stories(msg: Message) -> list[Story]:
    """Shared by the three beehiiv-built senders: headlines are emphasized."""
    return extract_stories(html_body(msg), require_emphasis=True, min_title=15)


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


# Which of these are LIVE, as of the mailbox survey on 2026-08-28: tldr,
# theneuron and milkroad had mail in the surveyed week and send from
# `tldrnewsletter.com`, `newsletter.theneurondaily.com` and `mail.milkroad.com`
# respectively, which is why subdomain suffix matching is load-bearing rather
# than defensive. therundown and bensbites stay in the allowlist as adapters
# but had NO mail in that week, so their extraction is tested only against the
# synthetic fixtures: their real-world format is grade C until a real message
# from each has been through this parser.
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
        senders=("rundown.ai", "mail.therundown.ai", "therundown.ai"),
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
        parse=_beehiiv_stories,
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
