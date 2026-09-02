"""Declared capabilities for the live source adapters.

ADDITIVE. The two-member ``SourceAdapter`` Protocol in ``base.py`` is not
touched: an adapter that declares nothing still fetches. ``DeclaresCapabilities``
is a separate typing hook so a caller can ask for the declaration without
widening the fetch contract.

Why declare at all: Gate 0c measured an undeclared behavioural split. Every
route is handed ``SourceContext.queries`` and exactly one adapter reads them.
Nothing in the registry said so, so nothing could be checked.

WHAT EACH FLAG MEANS HERE. Written down because a flag that means two things to
two adapters is worse than no flag. These readings are what
``tests/test_source_capabilities.py`` asserts against, so a later change has to
change the definition in the open rather than drift a value quietly.

``supports_poll``
    The adapter is driven by the caller asking it to fetch. Every live adapter
    is.
``supports_push``
    The source can deliver unprompted (webhook, stream). No live adapter has an
    inbound path; all six only issue outbound GETs.
``supports_full_text``
    The adapter emits the article BODY. None do. ``description`` is the
    source's own bounded summary, and the feed adapter explicitly skips article
    bodies it never consumes (``feed.py``, ``enforce_text_limit=False``).
``supports_trend_signal``
    The SOURCE published a popularity value the adapter reads off the wire.
    ``Item.native_rank`` alone does not count: in the feed, JSON Feed, and
    sitemap adapters that value is this adapter's own enumeration index,
    surfaced only when an operator declared the route ``category: trending``.
    An adapter-assigned position is not a signal the publisher sent.
``supports_social_signal``
    The source published an interaction count (votes, comments) the adapter
    reads. Same wire-side test as the trend flag.
``supports_deletion``
    The source tells us an item is gone (a tombstone or a delete feed). No live
    adapter has any notion of a retracted item.
``supports_incremental_checkpoint``
    The adapter resumes from a durable cursor. GREENFIELD everywhere: no live
    adapter sends a conditional GET or reads ``SourceContext.durable_store``.
``consumes_search_queries``
    The adapter reads ``SourceContext.queries``. This is the flag with a
    behavioural test behind it, not just a reviewer's reading.
``languages``
    EMPTY means the adapter imposes no language restriction of its own: the
    route declares its language through ``SourceSpec.language`` and the adapter
    stamps that value onto every item. It does NOT mean "serves no languages".
    No live adapter restricts language, so every declaration here is empty.

``consumes_search_queries`` HAS A BEHAVIOURAL GATE, AND THE GATE HAS A NAMED
BOUNDARY. ``tests/test_source_capabilities.py`` proves exactly two things about
an adapter: that its ``fetch`` READS the queries object it is handed, or that
changing the queries changes the complete normalized result. An adapter that
consumes query semantics through ANY OTHER CHANNEL is outside what the gate can
see: reading them from spec options, from transport-level parameters, by
counting through a different object it was given, or by an EXACT-type check
such as ``type(context.queries) is tuple``, which is False for the tracking
proxy and True for the plain tuple production hands over. Three review rounds each
found one more adapter shape that branches on something the harness does not
reproduce (the queries type, the transport type, the spec id), and the ladder
has no top, because any harness-visible difference is a discriminator.

So the honest scope of a green gate is "no live adapter reads the queries under
production-shaped inputs", never "no adapter can read them". A declaration by
an adapter that consumes query semantics through another channel is enforced by
REVIEW, not by this test. Written here rather than left implicit so a later
round names the boundary instead of adding liar number six.

``plugin_id`` is always the registry key (``type_key``), so a declaration
cannot be attributed to the wrong adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..contracts.source_plugin import SourceCapabilities


#: Version of these declarations, not of the wire formats. Bump when an
#: adapter's declared behaviour changes, so a stored registration row can be
#: compared against what the code now claims.
ADAPTER_PLUGIN_VERSION = "1"


@runtime_checkable
class DeclaresCapabilities(Protocol):
    """An adapter that states what it can do.

    Deliberately separate from ``SourceAdapter``: fetching and declaring are
    different obligations, and the live Protocol stays two members wide.
    """

    type_key: str

    def capabilities(self) -> SourceCapabilities: ...


def declared_capabilities(adapter: object) -> SourceCapabilities | None:
    """Return an adapter's declaration, or ``None`` when it makes none.

    Returning ``None`` rather than a permissive default is the point: an
    undeclared adapter must be visible as undeclared, never inferred.
    """

    declare = getattr(adapter, "capabilities", None)
    if not callable(declare):
        return None
    declaration = declare()
    if not isinstance(declaration, SourceCapabilities):
        return None
    return declaration
