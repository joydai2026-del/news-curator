"""Fail-closed network, parser, and configuration policy tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.sources import (
    SafeHttpPolicy,
    SafeHttpResponse,
    SafeHttpTransport,
    SafeTransportError,
    SafeTransportReason,
    SourceContext,
    SourceRegistry,
    SourceValidationError,
    build_builtin_registry,
)
from curator.sources.base import success_result


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "sources"


class RecordingTransport:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, source_id, url, **kwargs):
        self.calls.append((source_id, url, kwargs))
        return SafeHttpResponse(self.status, url, {}, self.payload)


class MimeCheckingTransport(RecordingTransport):
    def __init__(self, payload: bytes, content_type: str) -> None:
        super().__init__(payload)
        self.content_type = content_type

    def get(self, source_id, url, **kwargs):
        self.calls.append((source_id, url, kwargs))
        allowed = tuple(kwargs.get("allowed_mime_types") or ())
        if self.content_type not in allowed:
            raise SafeTransportError(
                source_id, SafeTransportReason.UNSUPPORTED_MIME_TYPE
            )
        return SafeHttpResponse(
            self.status,
            url,
            {"content-type": self.content_type},
            self.payload,
        )


def context(registry, transport):
    return SourceContext(
        registry=registry,
        transport=transport,
        clock=lambda: NOW,
        environment=lambda _name: None,
    )


class PolicyProbeAdapter:
    type_key = "policy_probe"

    def __init__(self):
        self.policy = None

    def validate_options(self, _spec):
        return {}

    def fetch(self, spec, source_context):
        self.policy = source_context.transport.policy
        return success_result(spec, (), source_context.now())


@pytest.mark.parametrize(
    ("source_values", "expected"),
    [
        ((2.0, 128, 1), (2.0, 128, 1)),
        ((20.0, 2048, 8), (10.0, 1024, 4)),
    ],
)
def test_source_network_policy_is_applied_and_clamped_to_global_ceiling(source_values, expected):
    adapter = PolicyProbeAdapter()
    registry = SourceRegistry((adapter,))
    timeout, byte_cap, concurrency = source_values
    spec = registry.parse_spec(
        {
            "type": "policy_probe",
            "id": "probe",
            "name": "Probe",
            "url": "https://example.com/feed",
            "request_timeout_seconds": timeout,
            "max_response_bytes": byte_cap,
            "per_host_concurrency": concurrency,
        }
    )
    transport = SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=10,
            max_wire_bytes=1024,
            max_decoded_bytes=1024,
            per_host_concurrency=4,
        )
    )

    registry.fetch(spec, context(registry, transport))

    assert adapter.policy is not None
    assert (
        adapter.policy.total_timeout_seconds,
        adapter.policy.max_wire_bytes,
        adapter.policy.per_host_concurrency,
    ) == expected
    assert adapter.policy.max_decoded_bytes == expected[1]


def test_registry_rejects_arbitrary_html_scraper_type():
    registry = build_builtin_registry()
    with pytest.raises(SourceValidationError, match="not allowlisted"):
        registry.parse_spec(
            {
                "type": "html",
                "id": "site",
                "name": "Site",
                "url": "https://example.com/news",
            }
        )


@pytest.mark.parametrize("declaration", [b"<!DOCTYPE rss>", b"<!ENTITY payload 'x'>"])
def test_every_xml_adapter_rejects_dtd_and_entity_declarations(declaration):
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
        }
    )
    transport = RecordingTransport(b"<?xml version='1.0'?>" + declaration + b"<rss/>")

    result = registry.fetch(spec, context(registry, transport))

    assert result.health.status == "malformed"
    assert result.health.reason_code == "malformed_xml_dtd_or_entity"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_encoded_xml_cannot_bypass_dtd_rejection(encoding):
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
        }
    )
    document = "<?xml version='1.0'?><!DOCTYPE rss [<!ENTITY x 'boom'>]><rss><channel/></rss>"
    result = registry.fetch(
        spec, context(registry, RecordingTransport(document.encode(encoding)))
    )

    assert result.health.status == "malformed"
    assert result.health.reason_code == "malformed_xml_dtd_or_entity"


def test_adapter_applies_source_response_byte_bound_after_safe_transport():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "json_feed",
            "id": "json",
            "name": "JSON",
            "url": "https://example.com/feed.json",
            "max_response_bytes": 16,
        }
    )
    transport = RecordingTransport(b"{" + b"x" * 64 + b"}")

    result = registry.fetch(spec, context(registry, transport))

    assert len(transport.calls) == 1
    assert result.health.reason_code == "response_too_large"


def test_json_string_and_item_bounds_fail_closed():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "json_feed",
            "id": "json",
            "name": "JSON",
            "url": "https://example.com/feed.json",
            "options": {"max_items": 1, "max_string_chars": 100},
        }
    )
    payload = json.dumps(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "items": [
                {
                    "id": "1",
                    "url": "https://example.com/1",
                    "title": "One",
                    "date_published": "2026-08-29T10:00:00Z",
                },
                {
                    "id": "2",
                    "url": "https://example.com/2",
                    "title": "Two",
                    "date_published": "2026-08-29T11:00:00Z",
                },
            ],
        }
    ).encode()

    result = registry.fetch(spec, context(registry, RecordingTransport(payload)))

    assert result.health.reason_code == "item_limit_exceeded"


def test_feed_ignores_long_unused_content_within_structural_bounds():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": {"max_string_chars": 100},
        }
    )
    payload = (
        "<rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        "<channel><item><title>Usable story</title>"
        "<link>https://example.com/story</link>"
        "<pubDate>Sat, 29 Aug 2026 20:00:00 GMT</pubDate>"
        f"<content:encoded>{'x' * 101}</content:encoded>"
        "</item></channel></rss>"
    ).encode()

    result = registry.fetch(spec, context(registry, RecordingTransport(payload)))

    assert result.health.status == "fresh"
    assert [item.title for item in result.items] == ["Usable story"]


def test_feed_processes_item_cap_without_rejecting_larger_bounded_document():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": {"max_items": 2},
        }
    )
    rows = "".join(
        "<item>"
        f"<title>Story {index}</title>"
        f"<link>https://example.com/{index}</link>"
        "<pubDate>Sat, 29 Aug 2026 20:00:00 GMT</pubDate>"
        "</item>"
        for index in range(3)
    )
    payload = f"<rss version='2.0'><channel>{rows}</channel></rss>".encode()

    result = registry.fetch(spec, context(registry, RecordingTransport(payload)))

    assert result.health.status == "fresh"
    assert [item.title for item in result.items] == ["Story 0", "Story 1"]


def test_feed_rejects_html_mime_by_default():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
        }
    )
    payload = (
        b"<rss><channel><item><title>Story</title>"
        b"<link>https://example.com/story</link>"
        b"<pubDate>Sat, 29 Aug 2026 20:00:00 GMT</pubDate>"
        b"</item></channel></rss>"
    )

    result = registry.fetch(
        spec, context(registry, MimeCheckingTransport(payload, "text/html"))
    )

    assert result.items == ()
    assert result.health.reason_code == "unsupported_mime_type"


def test_feed_accepts_html_mime_only_with_source_opt_in():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": {"allow_mislabeled_html_mime": True},
        }
    )
    payload = (
        b"<rss><channel><item><title>Story</title>"
        b"<link>https://example.com/story</link>"
        b"<pubDate>Sat, 29 Aug 2026 20:00:00 GMT</pubDate>"
        b"</item></channel></rss>"
    )

    result = registry.fetch(
        spec, context(registry, MimeCheckingTransport(payload, "text/html"))
    )

    assert [item.title for item in result.items] == ["Story"]
    assert result.health.status == "fresh"


def test_feed_mislabeled_html_opt_in_requires_boolean():
    registry = build_builtin_registry()

    with pytest.raises(SourceValidationError, match="must be a boolean"):
        registry.parse_spec(
            {
                "type": "rss",
                "id": "feed",
                "name": "Feed",
                "url": "https://example.com/feed.xml",
                "options": {"allow_mislabeled_html_mime": "true"},
            }
        )


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        (b"<html><body>ordinary page</body></html>", "malformed_feed"),
        (b"<rss><channel><item>", "malformed_xml"),
    ],
    ids=("ordinary-html", "malformed-xml"),
)
def test_feed_html_mime_opt_in_still_requires_a_valid_feed(payload, reason_code):
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": {"allow_mislabeled_html_mime": True},
        }
    )

    result = registry.fetch(
        spec, context(registry, MimeCheckingTransport(payload, "text/html"))
    )

    assert result.items == ()
    assert result.health.status == "malformed"
    assert result.health.reason_code == reason_code


def test_feed_structural_node_bound_still_rejects_oversized_document():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": {"max_xml_nodes": 10},
        }
    )
    payload = (
        "<rss><channel>"
        + "".join(f"<unused>{index}</unused>" for index in range(9))
        + "</channel></rss>"
    ).encode()

    result = registry.fetch(spec, context(registry, RecordingTransport(payload)))

    assert result.health.reason_code == "xml_node_limit_exceeded"


@pytest.mark.parametrize(
    ("suffix", "options"),
    [
        (b"<unused/>" * 50, {"max_xml_nodes": 10}),
        (b"<unused a='" + b"x" * 101 + b"'/>", {"max_string_chars": 100}),
        (b"<x>" * 20 + b"</x>" * 20, {"max_xml_depth": 6}),
    ],
    ids=("node-limit", "attribute-limit", "depth-limit"),
)
def test_malformed_feed_cannot_bypass_bounds_after_early_parse_error(
    suffix, options
):
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "options": options,
        }
    )
    item = (
        b"<item><title>Story</title><link>https://example.com/story</link>"
        b"<pubDate>Sat, 29 Aug 2026 20:00:00 GMT</pubDate></item>"
    )
    payload = (
        b"<rss><channel><broken>&oops</broken>"
        + suffix
        + item
        + b"</channel></rss>"
    )

    result = registry.fetch(spec, context(registry, RecordingTransport(payload)))

    assert result.items == ()
    assert result.health.status == "malformed"
    assert result.health.reason_code == "malformed_xml"


def test_adapter_owned_options_reject_unknown_fields():
    registry = build_builtin_registry()
    with pytest.raises(SourceValidationError, match="unknown rss options"):
        registry.parse_spec(
            {
                "type": "rss",
                "id": "feed",
                "name": "Feed",
                "url": "https://example.com/feed.xml",
                "options": {"css_selector": "article"},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", "yes"),
        ("echo_eligible", "no"),
        ("aggregator", "false"),
        ("is_aggregator", "true"),
        ("request_timeout_seconds", 0),
        ("max_response_bytes", 0),
        ("per_host_concurrency", 0),
        ("url", "file:///etc/passwd"),
    ],
)
def test_common_source_policy_is_validated(field, value):
    registry = build_builtin_registry()
    row = {
        "type": "rss",
        "id": "feed",
        "name": "Feed",
        "url": "https://example.com/feed.xml",
    }
    row[field] = value
    with pytest.raises(SourceValidationError):
        registry.parse_spec(row)


def test_json_feed_uses_safe_transport_mime_allowlist():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "json_feed",
            "id": "json",
            "name": "JSON",
            "url": "https://example.com/feed.json",
        }
    )
    transport = RecordingTransport((FIXTURES / "daring-fireball.json").read_bytes())

    registry.fetch(spec, context(registry, transport))

    assert transport.calls[0][2]["allowed_mime_types"] == (
        "application/json",
        "application/feed+json",
    )


def test_disabled_source_returns_health_without_network_access():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "disabled",
            "name": "Disabled",
            "url": "https://example.com/feed.xml",
            "enabled": False,
        }
    )
    transport = RecordingTransport(b"must not be read")

    result = registry.fetch(spec, context(registry, transport))

    assert transport.calls == []
    assert result.health.status == "disabled"
    assert result.health.reason_code == "disabled_by_config"
