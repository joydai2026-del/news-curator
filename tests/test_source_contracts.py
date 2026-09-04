"""Stable source contract and captured-fixture behavior."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from curator.sources import (
    SafeHttpResponse,
    SourceContext,
    SourceQuery,
    SourceRegistry,
    SourceResult,
    SourceSpec,
    SourceValidationError,
    build_builtin_registry,
    collect_sources,
)
from curator.sources.base import success_result


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)
FEED_FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
SOURCE_FIXTURES = Path(__file__).parent / "fixtures" / "sources"


class FakeTransport:
    def __init__(
        self, responses: Mapping[str, bytes] | None = None, default: bytes = b""
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.user_agents: list[str | None] = []

    def get(self, source_id: str, url: str, **kwargs: Any) -> SafeHttpResponse:
        mimes = tuple(kwargs.get("allowed_mime_types") or ())
        self.calls.append((source_id, url, mimes))
        self.user_agents.append(kwargs.get("user_agent"))
        body = next(
            (payload for needle, payload in self.responses.items() if needle in url),
            self.default,
        )
        return SafeHttpResponse(200, url, {}, body)


def make_context(
    registry: SourceRegistry,
    transport: FakeTransport,
    *,
    queries=(),
    user_agent: str | None = None,
) -> SourceContext:
    return SourceContext(
        registry=registry,
        transport=transport,  # type: ignore[arg-type]
        clock=lambda: NOW,
        environment=lambda _name: None,
        user_agent=user_agent,
        queries=tuple(queries),
    )


def test_source_spec_is_discriminated_and_adapter_options_are_validated():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "json_feed",
            "id": "daring-fireball",
            "name": "Daring Fireball",
            "url": "https://daringfireball.net/feeds/json",
            "language": "en",
            "max_age_hours": 48,
            "options": {"max_items": 25},
        }
    )

    assert spec.type == "json_feed"
    assert spec.options["max_items"] == 25
    assert spec.options["max_json_depth"] == 32


def test_rss_adapter_preserves_real_captured_p1_fields():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "cnbeta",
            "name": "cnBeta",
            "url": "https://www.cnbeta.com.tw/backend.php",
            "language": "zh",
            "category": "ai",
            "weight": 0.95,
        }
    )
    transport = FakeTransport(default=(FEED_FIXTURES / "cnbeta.xml").read_bytes())

    result = registry.fetch(spec, make_context(registry, transport))

    assert len(result.items) == 1
    assert "人工智能" in result.items[0].title
    assert result.items[0].language == "zh"
    assert result.items[0].native_categories == {"ai"}
    assert result.health.source_type == "rss"
    assert "application/rss+xml" in transport.calls[0][2]


def test_google_news_feed_remains_non_corroborating_and_explicitly_degraded():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "google-36kr",
            "name": "36kr via Google News",
            "url": "https://news.google.com/rss/search?q=site%3A36kr.com",
            "language": "zh",
            "aggregator": True,
            "platform": "google-news",
            "echo_eligible": False,
        }
    )
    transport = FakeTransport(default=(FEED_FIXTURES / "google-36kr.xml").read_bytes())

    result = registry.fetch(spec, make_context(registry, transport))

    assert result.items[0].echo_platforms == set()
    assert result.health.status == "link_resolution_degraded"
    assert result.health.reason_code == "google_news_url_retained_non_corroborating"


def test_news_sitemap_uses_news_publication_date_and_declared_image():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "news_sitemap",
            "id": "cnn-news",
            "name": "CNN",
            "url": "https://www.cnn.com/sitemap/news.xml",
            "max_age_hours": 6,
        }
    )
    transport = FakeTransport(default=(FEED_FIXTURES / "cnn-news.xml").read_bytes())

    result = registry.fetch(spec, make_context(registry, transport))

    assert result.items[0].published_at == datetime(
        2026, 8, 29, 5, 13, 16, 172000, tzinfo=timezone.utc
    )
    assert result.items[0].image_url.startswith("https://media.cnn.com/")
    assert result.health.status == "stale"


def test_json_feed_11_adapter_parses_sanitized_real_capture():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "json_feed",
            "id": "daring-fireball",
            "name": "Daring Fireball",
            "url": "https://daringfireball.net/feeds/json",
        }
    )
    transport = FakeTransport(
        default=(SOURCE_FIXTURES / "daring-fireball.json").read_bytes()
    )

    result = registry.fetch(spec, make_context(registry, transport))

    assert [item.title for item in result.items] == [
        "★ Thoughts and Observations on Apple’s First Immersive MLB Broadcast, a Yankees 1-0 Win Over the Red Sox",
        "Apple Announces Price Increase for Apple TV and Apple One Subscriptions",
    ]
    assert result.items[0].published_at == datetime(
        2026, 8, 29, 17, 53, 34, tzinfo=timezone.utc
    )
    assert result.health.status == "fresh"
    assert transport.calls[0][2] == ("application/json", "application/feed+json")


def test_hackernews_keeps_typed_round_robin_queries_and_request_cap():
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": "hackernews",
            "id": "hackernews",
            "name": "Hacker News",
            "url": "https://hn.algolia.com/api/v1",
            "weight": 0.95,
            "aggregator": True,
            "options": {"max_requests": 2, "include_by_date": True},
        }
    )
    front = (FEED_FIXTURES / "hn-front-page.json").read_bytes()
    empty = json.dumps({"hits": []}).encode()
    transport = FakeTransport({"tags=front_page": front}, default=empty)
    queries = (
        SourceQuery("ai", ("Claude", "OpenAI")),
        SourceQuery("web", ("Htmx",)),
    )

    result = registry.fetch(spec, make_context(registry, transport, queries=queries))

    assert len(transport.calls) == 3
    topic_urls = [url for _, url, _ in transport.calls[1:]]
    assert "query=Claude" in topic_urls[0]
    assert "query=Claude" in topic_urls[1]
    assert "/search_by_date?" in topic_urls[1]
    assert all("query=Htmx" not in url for url in topic_urls)
    assert "query_cap%" not in "".join(topic_urls)
    assert "query_cap:2" in result.note
    # The leading segment is the partial marker the cross-run health fold reads:
    # it is the only part of a degraded run that survives base.py rewriting the
    # status and reason_code when the run is also stale.
    assert result.note.split(";")[0] == "partial"
    front_items = [
        item for item in result.items if item.native_categories == {"trending"}
    ]
    assert [item.native_rank for item in front_items] == [0, 1]


@pytest.mark.parametrize(
    ("source_id", "fixture", "expected_type", "language", "category", "title", "echo"),
    [
        (
            "fox-news",
            "fox-news.xml",
            "news_sitemap",
            "en",
            "",
            "Beer bandits swipe PBR truck hauling 50,000 cans in California, leaving brand pleading for help",
            True,
        ),
        (
            "dw-zh",
            "dw-zh.rdf",
            "rss",
            "zh",
            "world",
            "中国军备迅速扩张：仅仅是为了自卫吗？",
            True,
        ),
        (
            "solidot",
            "solidot.xml",
            "rss",
            "zh",
            "",
            "Debian 项目将允许以负责任的方式使用生成式 AI",
            True,
        ),
        (
            "buzzing",
            "buzzing.xml",
            "atom",
            "zh",
            "trending",
            "美国国土安全部正利用一条鲜为人知的法律对记者、非营利组织和工会进行监视",
            False,
        ),
    ],
)
def test_production_config_registry_fixture_sweep(
    source_id, fixture, expected_type, language, category, title, echo
):
    from curator.config import load_config
    from curator.pipeline import configured_source_specs

    cfg = load_config(Path(__file__).resolve().parent.parent)
    registry = build_builtin_registry()
    specs = {spec.id: spec for spec in configured_source_specs(cfg, registry)}
    spec = specs[source_id]
    transport = FakeTransport(default=(FEED_FIXTURES / fixture).read_bytes())
    context = make_context(registry, transport)

    (result,) = collect_sources((spec,), context, max_workers=1)

    assert spec.type == expected_type
    assert spec.language == language
    assert spec.category == category
    assert result.items[0].title == title
    assert result.items[0].native_rank == (0 if category == "trending" else None)
    assert result.items[0].language == language
    assert result.items[0].native_categories == ({category} if category else set())
    assert result.items[0].echo_eligible is echo
    assert result.health.source_type == expected_type
    expected_policy = {
        "fox-news": (6.0, 1.0, False),
        "dw-zh": (12.0, 1.0, False),
        "solidot": (6.0, 1.0, False),
        "buzzing": (6.0, 0.9, True),
    }
    assert (spec.max_age_hours, spec.weight, spec.is_aggregator) == expected_policy[source_id]


@pytest.mark.parametrize(
    ("source_type", "fixture"),
    (
        ("feed", "cnbeta.xml"),
        ("rss", "cnbeta.xml"),
        ("atom", "cnbeta.xml"),
        ("news_sitemap", "cnn-news.xml"),
        ("json_feed", "daring-fireball.json"),
        ("hackernews", "hn-front-page.json"),
    ),
)
def test_every_builtin_source_request_uses_configured_application_identity(
    source_type, fixture
):
    registry = build_builtin_registry()
    spec = registry.parse_spec(
        {
            "type": source_type,
            "id": "identity-probe",
            "name": "Identity probe",
            "url": (
                "https://hn.algolia.com/api/v1"
                if source_type == "hackernews"
                else "https://example.com/source"
            ),
        }
    )
    fixture_root = FEED_FIXTURES if fixture.endswith((".xml", ".rdf")) else SOURCE_FIXTURES
    fixture_path = fixture_root / fixture
    if not fixture_path.exists():
        fixture_path = FEED_FIXTURES / fixture
    transport = FakeTransport(default=fixture_path.read_bytes())
    user_agent = "news-curator-tests/3 (+https://example.com/contact)"

    registry.fetch(
        spec,
        make_context(registry, transport, user_agent=user_agent),
    )

    assert transport.user_agents
    assert set(transport.user_agents) == {user_agent}


@pytest.mark.parametrize(
    "user_agent",
    ("bad\r\nInjected: yes", "非ASCII", "x" * 257),
)
def test_source_context_rejects_unsafe_configured_application_identity(user_agent):
    registry = build_builtin_registry()

    with pytest.raises(SourceValidationError, match="user agent is invalid"):
        make_context(registry, FakeTransport(), user_agent=user_agent)


class _ControlledAdapter:
    type_key = "controlled"

    def validate_options(self, _spec: SourceSpec) -> Mapping[str, Any]:
        return {}

    def fetch(self, spec: SourceSpec, context: SourceContext) -> SourceResult:
        if spec.id == "slow":
            time.sleep(0.02)
        if spec.id == "broken":
            raise RuntimeError("untrusted failure detail")
        return success_result(spec, (), context.now())


def test_concurrent_collection_preserves_configured_order_and_contains_failures():
    registry = SourceRegistry((_ControlledAdapter(),))
    specs = registry.parse_specs(
        [
            {
                "type": "controlled",
                "id": "slow",
                "name": "Slow",
                "url": "https://slow.example/feed",
            },
            {
                "type": "controlled",
                "id": "broken",
                "name": "Broken",
                "url": "https://bad.example/feed",
            },
            {
                "type": "controlled",
                "id": "fast",
                "name": "Fast",
                "url": "https://fast.example/feed",
            },
        ]
    )
    context = make_context(registry, FakeTransport())

    results = collect_sources(specs, context, max_workers=3)

    assert [result.source_id for result in results] == ["slow", "broken", "fast"]
    assert results[1].health.status == "unavailable"
    assert results[1].health.reason_code == "adapter_failed"
    assert "untrusted" not in results[1].note
