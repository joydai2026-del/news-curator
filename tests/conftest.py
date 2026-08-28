from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.models import Item
from curator.normalize import canonical_url

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_item(
    title: str,
    url: str = "https://example.com/a",
    *,
    source_id: str = "example",
    source_name: str = "Example",
    platform: str | None = None,
    hours_ago: float = 1.0,
    weight: float = 1.0,
    score: int | None = None,
    aggregator: bool = False,
) -> Item:
    return Item(
        title=title,
        url=url,
        canonical_url=canonical_url(url) or url,
        source_id=source_id,
        source_name=source_name,
        platform=platform or source_id,
        published_at=NOW - timedelta(hours=hours_ago),
        source_weight=weight,
        score=score,
        is_aggregator=aggregator,
    )


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """Enforce "no network" rather than asserting it in a README line.

    Several tests claim a code path makes no request. Without this they would
    still pass while quietly doing a DNS lookup, which is how "231 tests, no
    network" becomes false without anyone noticing. Tests that deliberately
    exercise host resolution opt out with @pytest.mark.allow_socket.
    """
    if request.node.get_closest_marker("allow_socket"):
        return

    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("this test touched the network; the suite must stay offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
