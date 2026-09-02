"""Declared adapter capabilities, and one flag checked against real behaviour.

A declaration nobody checks is a comment. ``consumes_search_queries`` is the
flag with a measured split behind it (every route is handed
``SourceContext.queries``; one adapter reads them), so it is the flag this file
proves by exercising each adapter against a recorded fixture rather than by
reading its source.

The check has TWO independent signals, because either one alone can be fooled.

The DIGEST signal compares everything one fetch produced: every field of every
normalized ``Item``, plus ``SourceResult.health`` and ``SourceResult.note``. An
adapter that folds the queries into ``native_rank``, into a health reason, or
into the note alone changes nothing a narrower projection can see.

The ACCESS signal wraps ``SourceContext.queries`` in a transparent proxy that
records whether it was read at all, because an adapter could read the queries
and happen to produce identical output.

BOTH SIGNALS RUN THROUGH ``SourceRegistry.fetch`` ON A REAL
``SafeHttpTransport``, so both take the shape production takes. Two earlier
versions of this file got that wrong in two different ways, and the corrected
account is written down here because each wrong version read plausibly.

    Version 1 SPLIT the legs, claiming the access proxy "cannot survive
    ``registry.fetch``". False: it survives fine.

    Version 2 shared one leg through ``registry.fetch`` on a stub transport,
    and said that if the stub ever became a real transport the access signal
    would "quietly read ``False`` for everyone". Also false, and backwards.
    MEASURED behaviour: ``registry.fetch`` scopes the policy with
    ``dataclasses.replace`` whenever the transport is a real
    ``SafeHttpTransport``; ``replace`` re-runs ``SourceContext.__post_init__``,
    which calls ``tuple(self.queries)``. That ITERATION IS ITSELF A READ, so a
    proxy installed on the caller's context is both marked read and downcast to
    a plain tuple before any adapter is entered. Under a real transport the old
    harness reported ``True`` for EVERY adapter, honest ones included: loud and
    wrong, not silent and wrong.

WHICH IS WHY THE PROXY IS INSTALLED AT THE ADAPTER BOUNDARY, not on the
caller's context. ``_TrackedAdapter`` wraps the adapter under test and installs
``TrackingQueries`` inside its own ``fetch``, on the context the adapter
ACTUALLY receives, after ``registry.fetch`` has finished replacing and
re-validating it. The signal is then independent of the transport type: an
adapter cannot dodge it by branching on the transport type, and the queries
proxy IS a tuple, so an ``isinstance`` guard cannot route around it either. It
is NOT independent of every question an adapter can ask about the object: an
EXACT-type check (``type(context.queries) is tuple``) tells the harness apart
from production and is outside the gate, as the boundary paragraph below says.

THE PROXY IS STILL A TUPLE SUBCLASS, deliberately. Production hands the adapter
a plain tuple. A proxy of any other type lets an adapter guard its read with
``isinstance(context.queries, tuple)``: it reads in production and skips the
read under test, so both signals report nothing and the gate goes green on a
lie. ``_TypeSensitiveLiar`` is exactly that adapter, and
``_TransportSensitiveLiar`` is the same trick one level up, branching on
``isinstance(context.transport, SafeHttpTransport)``. Both are permanent red
gates, and both now run through the real branch.

WHAT THIS GATE PROVES, AND WHERE IT STOPS. Named here after three rounds each
found one more adapter shape the harness cannot reproduce, so a fourth round
names the boundary instead of adding liar number six. The gate proves exactly
two things: that the adapter's ``fetch`` READS the queries object it is handed,
or that changing the queries changes the complete normalized result. An adapter
that consumes query semantics through another channel is outside what this test
can see: through spec options, through transport-level parameters, by counting
through a different object, or by an exact-type check such as
``type(context.queries) is tuple``, which is False for the proxy and True for
the plain tuple production hands over. Any harness-visible difference is a
discriminator, so a conditional reader can always evade both signals; the
existing ``_TypeSensitiveLiar`` and ``_TransportSensitiveLiar`` are two rungs of
a ladder with no top and are kept as the permanent red gates for the two shapes
production actually varies on. The honest scope of a green result is "no live
adapter reads the queries under production-shaped inputs", NOT "no adapter can
read them", and a declaration by an adapter of the shapes named above is
enforced by review rather than by this file.

NO NETWORK, AND NO FAKE TRANSPORT TYPE. The transport here is a genuine
``SafeHttpTransport``; only its resolver and connector are injected, so the
stub sits at the socket, below every check the transport performs. That is the
same construction ``tests/test_safe_transport.py`` uses.
"""

from __future__ import annotations

import dataclasses
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from curator.contracts.source_plugin import SourceCapabilities
from curator.sources import (
    ConnectedPeer,
    SourceContext,
    SourceQuery,
    SourceRegistry,
    SourceResult,
    SourceSpec,
    SafeHttpTransport,
    build_builtin_registry,
)
from curator.sources.base import success_result
from curator.sources.capabilities import (
    ADAPTER_PLUGIN_VERSION,
    DeclaresCapabilities,
    declared_capabilities,
)


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)
FEED_FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
SOURCE_FIXTURES = Path(__file__).parent / "fixtures" / "sources"

#: One recorded payload per registry key. No network, and no synthetic feed:
#: every one of these was captured from a real source.
FIXTURE_BY_TYPE = {
    "feed": FEED_FIXTURES / "cnbeta.xml",
    "rss": FEED_FIXTURES / "cnbeta.xml",
    "atom": FEED_FIXTURES / "buzzing.xml",
    "news_sitemap": FEED_FIXTURES / "cnn-news.xml",
    "json_feed": SOURCE_FIXTURES / "daring-fireball.json",
    "hackernews": FEED_FIXTURES / "hn-front-page.json",
}

QUERIES = (SourceQuery("ai", ("Claude", "OpenAI")), SourceQuery("web", ("Htmx",)))


class TrackingQueries(tuple):
    """A tuple subclass that records ANY read of the queries.

    IT IS A TUPLE because production is: an adapter can branch on
    ``isinstance(context.queries, tuple)``, and a proxy of a different type
    turns that branch off under test while leaving it on in production. That is
    a false green, not a stricter test.

    BEING A TUPLE IS NOT ENOUGH ON ITS OWN. A subclass inherits tuple's C-level
    slots, so iteration, indexing, ``len``, ``in``, ``==``, ``reversed``,
    ``count`` and ``index`` are answered in C and never reach a Python method,
    leaving ``read`` False. Every one of them is overridden explicitly below.
    ``__hash__`` stays tuple's: defining ``__eq__`` would otherwise make the
    type unhashable, and a hash reveals nothing about what was read.

    ``!=`` is NOT overridden and does not record: tuple answers it in C.
    Nothing in the suite or in any adapter reaches the queries that way, and
    the honest thing is to name the hole rather than imply the list is total.

    Installed after ``SourceContext`` construction on purpose: ``__post_init__``
    calls ``tuple(self.queries)``, which copies a subclass back down to a plain
    tuple.
    """

    def __new__(cls, queries):
        return super().__new__(cls, tuple(queries))

    def __init__(self, queries):
        # Read through tuple's own slot, so building the snapshot is not itself
        # recorded as a read.
        self._plain = tuple(tuple.__iter__(self))
        self.read = False

    def _touched(self):
        self.read = True
        return self._plain

    def __iter__(self):
        return iter(self._touched())

    def __getitem__(self, index):
        return self._touched()[index]

    def __len__(self):
        return len(self._touched())

    def __contains__(self, value):
        return value in self._touched()

    def __eq__(self, other):
        mine = self._touched()
        if isinstance(other, TrackingQueries):
            return mine == other._touched()
        return mine == other

    def __reversed__(self):
        return reversed(self._touched())

    def count(self, value):
        return self._touched().count(value)

    def index(self, value, *bounds):
        return self._touched().index(value, *bounds)

    __hash__ = tuple.__hash__


#: One global address, so the transport's own peer and DNS checks pass without
#: a socket ever being opened. Same value ``tests/test_safe_transport.py`` uses.
PUBLIC_IP = "93.184.216.34"

_MIME_BY_TYPE = {
    "feed": "application/rss+xml",
    "rss": "application/rss+xml",
    "atom": "application/atom+xml",
    "news_sitemap": "application/xml",
    "json_feed": "application/json",
    "hackernews": "application/json",
}


def _http_response(body: bytes, mime: str) -> bytes:
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {mime}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    )
    return head.encode("ascii") + body


class _StubSocket:
    """One connection. The stub sits HERE, at the socket, not at the transport.

    The transport is the real ``SafeHttpTransport``, so URL parsing, DNS
    validation, peer checks, request serialization, header limits and MIME
    validation all run exactly as they do in production. This object only
    answers the bytes a socket would.
    """

    def __init__(self, respond, calls: list[str]) -> None:
        self._respond = respond
        self._calls = calls
        self._response = b""

    def sendall(self, data: bytes) -> None:
        request = data.decode("latin-1")
        lines = request.split("\r\n")
        path = lines[0].split(" ")[1]
        host = next(
            (line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")),
            "",
        )
        url = f"https://{host}{path}"
        self._calls.append(url)
        self._response = self._respond(url)

    def makefile(self, _mode: str, _buffering: int | None = None):
        return io.BytesIO(self._response)

    def getpeername(self):
        return (PUBLIC_IP, 443)

    def settimeout(self, _value: float | None) -> None:
        return None

    def close(self) -> None:
        return None


def _transport(type_key: str) -> SafeHttpTransport:
    """A REAL ``SafeHttpTransport`` answering from the recorded fixture.

    Real type on purpose: ``registry.fetch`` scopes the policy and rebuilds the
    context with ``dataclasses.replace`` only for a real transport, and that is
    the branch production always takes. A stub of another type turns the branch
    off, which is how the harness came to miss an adapter that keys its read on
    the transport type.

    ``.calls`` is attached so the digest can name the requests that were made;
    it is a plain list shared by every policy view, because ``with_policy``
    copies the transport but keeps the injected connector this closure holds.
    """

    payload = FIXTURE_BY_TYPE[type_key].read_bytes() if type_key in FIXTURE_BY_TYPE else b""
    search = json.dumps({"hits": []}).encode() if type_key == "hackernews" else None
    mime = _MIME_BY_TYPE.get(type_key, "application/json")
    calls: list[str] = []

    def respond(url: str) -> bytes:
        body = payload
        if search is not None and "tags=front_page" not in url:
            body = search
        return _http_response(body, mime)

    def connect(_host: str, _port: int, _address: str, _tls: bool, _timeout: float):
        return ConnectedPeer(_StubSocket(respond, calls), True)

    transport = SafeHttpTransport(
        resolver=lambda *_args: (PUBLIC_IP,), connector=connect
    )
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


class _TrackedAdapter:
    """Wraps one adapter and instruments the context IT receives.

    THIS IS THE FIX FOR THE HOLE THAT MADE THE ACCESS SIGNAL TRANSPORT-
    SENSITIVE. Installing the proxy on the caller's context put it upstream of
    ``registry.fetch``, which under a real transport calls
    ``dataclasses.replace`` and so re-runs ``SourceContext.__post_init__``.
    That ``tuple(self.queries)`` both READS the proxy and hands the adapter a
    plain tuple, so every adapter read as a liar and no adapter was actually
    observed. Installing it here, inside the adapter boundary, means the proxy
    is created from whatever the adapter was really given, after every rebuild,
    and the signal no longer depends on what kind of transport is in play.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.type_key = inner.type_key
        self.tracked: TrackingQueries | None = None

    def capabilities(self) -> SourceCapabilities:
        return self.inner.capabilities()

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        return self.inner.validate_options(spec)

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        tracked = TrackingQueries(context.queries)
        object.__setattr__(context, "queries", tracked)
        self.tracked = tracked
        return self.inner.fetch(spec, context)


def _context(registry: SourceRegistry, transport: SafeHttpTransport, queries):
    return SourceContext(
        registry=registry,
        transport=transport,
        clock=lambda: NOW,
        environment=lambda _name: None,
        queries=tuple(queries),
    )


def _spec_row(type_key: str) -> dict[str, Any]:
    url = (
        "https://hn.algolia.com/api/v1"
        if type_key == "hackernews"
        else "https://example.com/source"
    )
    return {
        "type": type_key,
        "id": "capability-probe",
        "name": "Capability probe",
        "url": url,
    }


def _spec(registry: SourceRegistry, type_key: str):
    return registry.parse_spec(_spec_row(type_key))


def _instrumented(registry: SourceRegistry, type_key: str):
    """A one-adapter registry whose adapter is wrapped in ``_TrackedAdapter``."""

    wrapper = _TrackedAdapter(registry.adapter_for(type_key))
    return SourceRegistry({type_key: wrapper}), wrapper


def _digest(registry: SourceRegistry, type_key: str, queries) -> tuple[Any, ...]:
    """Everything one fetch produced, through the production dispatch path.

    Runs ``registry.fetch`` on a real ``SafeHttpTransport``, so the
    policy-scoping ``dataclasses.replace`` branch production always takes fires
    here too. The digest is deliberately total: every field of every item, the
    whole health line, and the note. A projection cannot see an adapter that
    folds the queries into a rank, a reason code, or the note alone.
    """

    spec = _spec(registry, type_key)
    transport = _transport(type_key)
    result = registry.fetch(spec, _context(registry, transport, queries))

    return (
        tuple(transport.calls),
        tuple(dataclasses.astuple(item) for item in result.items),
        dataclasses.astuple(result.health),
        result.note,
    )


def _queries_were_read(registry: SourceRegistry, type_key: str, queries) -> bool:
    """Whether the adapter looked at ``SourceContext.queries`` at all.

    Same dispatch path as the digest leg, same real transport, and the proxy is
    installed by ``_TrackedAdapter`` INSIDE the adapter boundary. Because the
    instrumentation happens after ``registry.fetch`` has rebuilt and
    re-validated the context, the signal measures the adapter and nothing else:
    neither the transport's type nor the context rebuild can move it.
    """

    probe_registry, wrapper = _instrumented(registry, type_key)
    spec = probe_registry.parse_spec(dict(_spec_row(type_key)))
    transport = _transport(type_key)
    probe_registry.fetch(spec, _context(probe_registry, transport, queries))

    assert wrapper.tracked is not None, f"{type_key} was never dispatched"
    return wrapper.tracked.read


def _gate_violations(registry: SourceRegistry, type_key: str) -> list[str]:
    """Every way this adapter's declaration disagrees with what it did."""

    declaration = declared_capabilities(registry.adapter_for(type_key))
    assert declaration is not None, f"{type_key} declares no capabilities"
    declared = declaration.consumes_search_queries

    observed = _digest(registry, type_key, ()) != _digest(registry, type_key, QUERIES)
    was_read = _queries_were_read(registry, type_key, QUERIES)

    problems = []
    if declared is not observed:
        problems.append(
            f"{type_key} declares consumes_search_queries={declared} but the "
            f"queries changed what it produced: {observed}"
        )
    if declared is not was_read:
        problems.append(
            f"{type_key} declares consumes_search_queries={declared} but the "
            f"queries were {'read' if was_read else 'never read'}"
        )
    return problems


def test_every_registered_adapter_declares_capabilities_under_its_own_key():
    registry = build_builtin_registry()

    for type_key in registry.keys:
        adapter = registry.adapter_for(type_key)
        declaration = declared_capabilities(adapter)

        assert declaration is not None, f"{type_key} declares no capabilities"
        # An alias that inherits its parent's hard-coded id would attribute a
        # declaration to the wrong adapter. rss and atom subclass FeedAdapter.
        assert declaration.plugin_id == type_key
        assert declaration.plugin_version
        assert isinstance(adapter, DeclaresCapabilities)


def test_the_registry_covers_every_adapter_this_file_can_exercise():
    # Guards the sweep below against a new adapter landing with no fixture and
    # silently skipping its behavioural check.
    assert set(build_builtin_registry().keys) == set(FIXTURE_BY_TYPE)


@pytest.mark.parametrize("type_key", sorted(FIXTURE_BY_TYPE))
def test_declared_search_query_consumption_matches_observed_behaviour(type_key):
    assert _gate_violations(build_builtin_registry(), type_key) == []


@pytest.mark.parametrize(
    ("name", "consume"),
    (
        ("iteration", lambda queries: list(queries)),
        ("indexing", lambda queries: queries[0]),
        ("length", lambda queries: len(queries)),
        ("containment", lambda queries: QUERIES[0] in queries),
        ("equality", lambda queries: queries == QUERIES),
        ("count", lambda queries: queries.count(QUERIES[0])),
        ("index", lambda queries: queries.index(QUERIES[0])),
        ("reversal", lambda queries: list(reversed(queries))),
    ),
)
def test_the_tracking_proxy_records_every_way_a_sequence_can_be_read(name, consume):
    # Guards the access signal against reading False for everyone, which would
    # make its assertion pass vacuously for the five non-consuming adapters.
    # The tuple subclass this replaced recorded only the first three rows: an
    # adapter consuming queries through any of the other five slipped through.
    untouched = TrackingQueries(QUERIES)
    touched = TrackingQueries(QUERIES)
    consume(touched)

    assert untouched.read is False, name
    assert touched.read is True, name


def test_the_tracking_proxy_is_a_tuple_so_an_isinstance_check_cannot_route_around_it():
    # Production hands the adapter a plain tuple. If the proxy is not one, an
    # adapter can read the queries in production and skip the read here. This
    # covers the `isinstance` shape ONLY: `type(x) is tuple` is False for the
    # proxy and True in production, and that shape is outside the gate. See the
    # boundary paragraph in the module docstring.
    tracked = TrackingQueries(QUERIES)

    assert isinstance(tracked, tuple)
    assert tuple(tuple.__iter__(tracked)) == QUERIES
    # Reading it as a tuple still records: the C slots are all overridden.
    assert tracked.read is False
    assert tracked.count(QUERIES[0]) == 1
    assert tracked.read is True
    # Hashing is tuple's and is not a read of the contents.
    assert hash(TrackingQueries(QUERIES)) == hash(QUERIES)


def test_instrumenting_on_the_callers_context_is_what_the_real_branch_defeats():
    """The MEASURED behaviour that forced instrumentation to the adapter boundary.

    This is the falsified claim, kept executable so it cannot be re-asserted.
    The old harness installed the proxy on the CALLER's context. Under a real
    ``SafeHttpTransport``, ``registry.fetch`` scopes the policy with
    ``dataclasses.replace``, which re-runs ``SourceContext.__post_init__``,
    whose ``tuple(self.queries)`` iterates the proxy. So:

      * the adapter receives a PLAIN TUPLE, not the proxy, and
      * the proxy reads ``True`` even though the adapter read nothing.

    Documentation in two earlier rounds said the opposite (that the signal
    would read ``False`` for everyone, silently). It reads ``True`` for
    everyone, loudly. That is why the proxy is no longer installed here.
    """

    seen = {}

    class _Capturing(_LyingAdapter):
        type_key = "capturing"

        def capabilities(self) -> SourceCapabilities:
            return dataclasses.replace(
                _LyingAdapter.capabilities(self), plugin_id=self.type_key
            )

        def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
            seen["queries"] = context.queries
            return success_result(spec, (), context.now())

    registry = _registry_for(_Capturing())
    spec = _spec(registry, "capturing")
    transport = _transport("capturing")
    context = _context(registry, transport, QUERIES)
    tracked = TrackingQueries(context.queries)
    object.__setattr__(context, "queries", tracked)

    registry.fetch(spec, context)

    # `_transport` constructs a real `SafeHttpTransport`, so asserting its type
    # here would be a local-constructor tautology, not a check. What has teeth
    # is the CONSEQUENCE of the real-transport branch having run: `registry.fetch`
    # rebuilt the context, which downcast the proxy and consumed it on the way past.
    assert seen["queries"] is not tracked
    assert type(seen["queries"]) is tuple
    assert tracked.read is True


def test_the_tracking_proxy_is_the_object_the_adapter_receives():
    """The boundary instrumentation delivers the proxy to the real adapter.

    Through the SAME real-transport branch as the test above: the wrapper runs
    after every rebuild, so what the wrapped adapter is handed IS the proxy.
    """

    seen = {}

    class _Capturing(_LyingAdapter):
        type_key = "capturing"

        def capabilities(self) -> SourceCapabilities:
            return dataclasses.replace(
                _LyingAdapter.capabilities(self), plugin_id=self.type_key
            )

        def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
            seen["queries"] = context.queries
            return success_result(spec, (), context.now())

    registry = _registry_for(_Capturing())
    probe_registry, wrapper = _instrumented(registry, "capturing")
    spec = probe_registry.parse_spec(dict(_spec_row("capturing")))
    transport = _transport("capturing")

    probe_registry.fetch(spec, _context(probe_registry, transport, QUERIES))

    # Same real-transport branch, same reason not to assert its type here. The
    # check with teeth is that what the wrapped adapter received IS the proxy,
    # which is only true because the wrapper runs after every rebuild.
    assert seen["queries"] is wrapper.tracked

    live = build_builtin_registry()
    assert _queries_were_read(live, "hackernews", QUERIES) is True
    assert _queries_were_read(live, "rss", QUERIES) is False


def test_search_query_consumption_is_visible_in_the_requests_themselves():
    # The equality check above proves a difference exists. This names it, so a
    # future adapter cannot pass by differing for an unrelated reason.
    registry = build_builtin_registry()
    requested_without = _digest(registry, "hackernews", ())[0]
    requested_with = _digest(registry, "hackernews", QUERIES)[0]

    assert all("query=" not in url for url in requested_without)
    assert any("query=Claude" in url for url in requested_with)
    assert len(requested_with) > len(requested_without)

    assert _digest(registry, "rss", ()) == _digest(registry, "rss", QUERIES)
    assert _queries_were_read(registry, "rss", QUERIES) is False


def test_capability_flags_that_read_a_wire_value_are_only_claimed_where_one_exists():
    registry = build_builtin_registry()
    trend = {
        key: declared_capabilities(registry.adapter_for(key)).supports_trend_signal
        for key in registry.keys
    }
    social = {
        key: declared_capabilities(registry.adapter_for(key)).supports_social_signal
        for key in registry.keys
    }

    # Only Hacker News reads a source-published rank and vote count off the
    # wire. Everywhere else native_rank is this adapter's enumerate index,
    # surfaced only for an operator-declared trending route.
    assert {key for key, value in trend.items() if value} == {"hackernews"}
    assert {key for key, value in social.items() if value} == {"hackernews"}


def test_no_live_adapter_claims_a_capability_the_source_layer_has_not_built():
    registry = build_builtin_registry()

    for type_key in registry.keys:
        declaration = declared_capabilities(registry.adapter_for(type_key))
        assert declaration is not None
        assert declaration.supports_poll is True
        # No inbound path exists anywhere in the source layer.
        assert declaration.supports_push is False
        # No adapter emits an article body.
        assert declaration.supports_full_text is False
        # No adapter has any notion of a retracted item.
        assert declaration.supports_deletion is False
        # Checkpointing is greenfield: no conditional GET, no durable cursor.
        assert declaration.supports_incremental_checkpoint is False
        # Language is a per-route declaration, never an adapter restriction.
        assert declaration.languages == ()


def test_an_adapter_that_declares_nothing_reads_as_undeclared_not_as_permissive():
    class Silent:
        type_key = "silent"

    assert declared_capabilities(Silent()) is None


def test_a_declaration_of_the_wrong_shape_is_rejected_rather_than_trusted():
    class Wrong:
        type_key = "wrong"

        def capabilities(self) -> Mapping[str, Any]:
            return {"consumes_search_queries": True}

    assert declared_capabilities(Wrong()) is None


# --- red gates: adapters that lie, and must not get through -----------------
#
# Each one declares consumes_search_queries=False and then consumes them
# anyway, through a path the previous harness could not see. If the gate ever
# stops failing these, it has stopped being a gate.


class _LyingAdapter:
    """Declares that it ignores the queries. Reads them regardless."""

    type_key = "liar"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            plugin_id=self.type_key,
            plugin_version=ADAPTER_PLUGIN_VERSION,
            supports_poll=True,
            supports_push=False,
            supports_full_text=False,
            supports_trend_signal=False,
            supports_social_signal=False,
            supports_deletion=False,
            supports_incremental_checkpoint=False,
            consumes_search_queries=False,
            languages=(),
        )

    def validate_options(self, spec: SourceSpec) -> Mapping[str, Any]:
        return {}

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        raise NotImplementedError


class _CountingLiar(_LyingAdapter):
    """Consumes the queries through ``count()`` and produces identical output.

    A tuple subclass answers ``count`` in C, so this read left the old tracking
    flag False while the output digest saw nothing to compare.
    """

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        context.queries.count(QUERIES[0])
        return success_result(spec, (), context.now())


class _ContainmentLiar(_LyingAdapter):
    """Consumes the queries through ``in`` and produces identical output."""

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        QUERIES[0] in context.queries  # noqa: B015
        return success_result(spec, (), context.now())


class _NoteLiar(_LyingAdapter):
    """Folds the queries into the NOTE only: no item, no request, changes.

    The note is the field ``_health`` never rewrites, and it was outside the
    old item-only digest, so this consumption was invisible on both signals.
    """

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        names = ",".join(query.category_id for query in tuple(context.queries))
        return success_result(spec, (), context.now(), note=names)


class _TypeSensitiveLiar(_LyingAdapter):
    """Reads the queries ONLY when they are a tuple, and returns identical output.

    The false green this closes: production always hands over a plain tuple, so
    this adapter reads the queries in production. When the proxy was an
    ordinary object rather than a tuple, the branch was skipped under test, the
    output was byte-identical either way, and BOTH signals reported nothing.
    The gate said the declaration was honest about an adapter that consumes
    every query it is given.
    """

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        if isinstance(context.queries, tuple):
            context.queries.count(QUERIES[0])
        return success_result(spec, (), context.now())


class _TransportSensitiveLiar(_LyingAdapter):
    """Reads the queries ONLY under a real ``SafeHttpTransport``.

    The false green this closes: production's transport is always a real
    ``SafeHttpTransport``, so this adapter consumes every query it is given in
    production. Against a stub transport of some other type the branch never
    fired, the output was byte-identical either way, and BOTH signals reported
    nothing. It is ``_TypeSensitiveLiar``'s trick one level up, with
    ``context.transport`` in place of ``context.queries``, and it is caught now
    only because both legs run on the real transport.
    """

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        if isinstance(context.transport, SafeHttpTransport):
            context.queries.count(QUERIES[0])
        return success_result(spec, (), context.now())


def _registry_for(adapter) -> SourceRegistry:
    return SourceRegistry({adapter.type_key: adapter})


@pytest.mark.parametrize(
    "adapter",
    (
        _CountingLiar(),
        _ContainmentLiar(),
        _NoteLiar(),
        _TypeSensitiveLiar(),
        _TransportSensitiveLiar(),
    ),
    ids=("count", "containment", "note", "type_sensitive", "transport_sensitive"),
)
def test_an_adapter_that_consumes_queries_while_declaring_otherwise_is_caught(adapter):
    registry = _registry_for(adapter)

    violations = _gate_violations(registry, adapter.type_key)

    assert violations, f"{type(adapter).__name__} passed a gate it should fail"


def test_the_note_liar_is_caught_by_the_digest_and_not_only_by_the_access_signal():
    # Proves the digest widening is load-bearing: the two runs differ ONLY in
    # SourceResult.note, which an item-only digest could not have seen.
    registry = _registry_for(_NoteLiar())

    without = _digest(registry, "liar", ())
    with_queries = _digest(registry, "liar", QUERIES)

    assert without[:3] == with_queries[:3]
    assert without[3] != with_queries[3]


def test_an_honest_adapter_reads_as_unread_through_the_real_transport_branch():
    """The gate must be able to say YES, and this is the direction that broke.

    Under the old caller-side instrumentation this adapter reported READ on a
    real transport, because ``__post_init__`` consumed the proxy: every honest
    adapter was a liar. Boundary instrumentation is what makes the negative
    answer real.
    """

    class _Honest(_LyingAdapter):
        type_key = "honest_real"

        def capabilities(self) -> SourceCapabilities:
            return dataclasses.replace(
                _LyingAdapter.capabilities(self), plugin_id=self.type_key
            )

        def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
            return success_result(spec, (), context.now())

    registry = _registry_for(_Honest())

    assert _queries_were_read(registry, "honest_real", QUERIES) is False
    assert _gate_violations(registry, "honest_real") == []


def test_an_honest_adapter_that_ignores_the_queries_passes_the_same_gate():
    # The red gates above are only meaningful if the gate can also say yes.
    class _Honest(_LyingAdapter):
        type_key = "honest"

        def capabilities(self) -> SourceCapabilities:
            return dataclasses.replace(
                _LyingAdapter.capabilities(self), plugin_id=self.type_key
            )

        def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
            return success_result(spec, (), context.now())

    assert _gate_violations(_registry_for(_Honest()), "honest") == []
