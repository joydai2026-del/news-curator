"""Route type declarations, checked against the recorded capture for each route.

A route's ``type`` says which adapter parses it. Until now every feed route
inherited the ``rss`` default whatever it actually served, which left the
``atom`` and ``json_feed`` adapters registered and serving nothing.

The bar for changing a route's type is equality, not plausibility: the current
adapter and the proposed one must produce the same normalized items from the
SAME recorded payload. A route with no recorded capture is not retyped.

The equality is measured through ``SourceRegistry.fetch``, which is the live
dispatch path (registry to ``guarded_fetch`` to the adapter's own ``fetch``).
Comparing the module-level parse function instead would compare ``f(x)`` with
``f(x)``: it never receives the adapter or the route type, so it cannot tell the
two adapters apart and a sabotaged ``AtomAdapter.fetch`` would go unnoticed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.sources import (
    SafeHttpResponse,
    SourceContext,
    build_builtin_registry,
)


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


class ReplayTransport:
    """Answers every GET from one recorded payload."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list[str] = []

    def get(self, source_id: str, url: str, **_kwargs: Any) -> SafeHttpResponse:
        self.calls.append(url)
        return SafeHttpResponse(200, url, {}, self.payload)


def spec_for(source_id: str):
    cfg = load_config(REPO_ROOT)
    registry = build_builtin_registry()
    return {spec.id: spec for spec in configured_source_specs(cfg, registry)}[source_id]


def fetched_as(source_id: str, type_key: str, fixture: str):
    """Fetch one route's recorded payload through the adapter ``type_key`` names.

    Goes through ``registry.fetch``, so the MIME allowlist, ``guarded_fetch``,
    the bozo / salvage branch and the health line are all exercised, not just
    the shared parse helper.
    """

    registry = build_builtin_registry()
    live = spec_for(source_id)
    retyped = registry.parse_spec(
        {
            "type": type_key,
            "id": live.id,
            "name": live.name,
            "url": live.url,
            "language": live.language,
            "category": live.category,
            "max_age_hours": live.max_age_hours,
            "weight": live.weight,
            "aggregator": live.is_aggregator,
            "platform": live.platform,
            "echo_eligible": live.echo_eligible,
        }
    )
    context = SourceContext(
        registry=registry,
        transport=ReplayTransport((FEED_FIXTURES / fixture).read_bytes()),  # type: ignore[arg-type]
        clock=lambda: NOW,
        environment=lambda _name: None,
    )
    return registry.fetch(retyped, context)


def test_the_buzzing_route_is_declared_atom_because_its_capture_is_atom():
    import feedparser

    parsed = feedparser.parse((FEED_FIXTURES / "buzzing.xml").read_bytes())

    # The capture is from https://www.buzzing.cc/feed.xml, the route's own URL.
    assert parsed.version == "atom10"
    assert spec_for("buzzing").type == "atom"


def test_retyping_buzzing_to_atom_produced_identical_normalized_items():
    """The equivalence proof behind the config change.

    Both adapters are driven through ``registry.fetch`` on the SAME recorded
    payload, and the COMPLETE normalized item sequences are compared, every
    field of every item. If ``AtomAdapter`` ever stops being a pure relabel of
    ``FeedAdapter``, this goes red.
    """

    as_rss = fetched_as("buzzing", "rss", "buzzing.xml")
    as_atom = fetched_as("buzzing", "atom", "buzzing.xml")

    assert as_rss.items  # an empty fetch would make equality meaningless
    assert as_rss.items == as_atom.items

    # The ONE visible difference, named rather than hidden by the comparison:
    # the health line records which registry key served the route.
    assert (as_rss.health.source_type, as_atom.health.source_type) == ("rss", "atom")
    assert as_rss.health.status == as_atom.health.status


def test_the_equivalence_proof_actually_exercises_the_atom_adapter(monkeypatch):
    """The guard against the guard: sabotage atom, the proof must go red.

    ``AtomAdapter.fetch is FeedAdapter.fetch``, so a comparison that does not
    dispatch through the adapter cannot tell them apart. Patching the subclass
    only (never ``FeedAdapter``) proves the atom side is really being driven.
    """

    from curator.sources.feed import AtomAdapter

    def sabotaged(self, spec, context):
        raise RuntimeError("atom adapter is sabotaged")

    monkeypatch.setattr(AtomAdapter, "fetch", sabotaged)

    as_rss = fetched_as("buzzing", "rss", "buzzing.xml")
    as_atom = fetched_as("buzzing", "atom", "buzzing.xml")

    # guarded_fetch contains the failure, so the difference shows up as an
    # empty item tuple rather than an exception.
    assert as_rss.items
    assert as_atom.items == ()
    assert as_rss.items != as_atom.items


@pytest.mark.parametrize(
    ("source_id", "fixture", "expected_version", "expected_type"),
    (
        ("buzzing", "buzzing.xml", "atom10", "atom"),
        ("cnbeta", "cnbeta.xml", "rss20", "rss"),
        ("solidot", "solidot.xml", "rss20", "rss"),
        ("google-36kr", "google-36kr.xml", "rss20", "rss"),
        # RSS 1.0 / RDF. Served by the same adapter as RSS 2.0, and there is no
        # rdf discriminator to retype it to, so it stays rss.
        ("dw-zh", "dw-zh.rdf", "rss10", "rss"),
    ),
)
def test_every_route_with_a_recorded_capture_is_typed_to_match_that_capture(
    source_id, fixture, expected_version, expected_type
):
    import feedparser

    parsed = feedparser.parse((FEED_FIXTURES / fixture).read_bytes())

    assert parsed.version == expected_version
    assert spec_for(source_id).type == expected_type


def test_the_atom_adapter_is_no_longer_registered_with_zero_routes():
    cfg = load_config(REPO_ROOT)
    types = {spec.type for spec in configured_source_specs(cfg, build_builtin_registry())}

    assert "atom" in types


def test_the_still_unused_adapters_are_named_rather_than_left_undocumented():
    """json_feed and feed serve no route, and this records why.

    ``json_feed``: the only recorded JSON Feed capture
    (tests/fixtures/sources/daring-fireball.json) belongs to no configured
    route, so there is nothing to retype and nothing to prove equivalence
    against. ``feed``: the base class behind the ``rss`` and ``atom``
    configuration aliases; a route typed ``feed`` would say less than either.
    """

    cfg = load_config(REPO_ROOT)
    types = {spec.type for spec in configured_source_specs(cfg, build_builtin_registry())}
    registered = set(build_builtin_registry().keys)

    assert registered - types == {"feed", "json_feed"}
