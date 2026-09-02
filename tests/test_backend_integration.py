"""Backend source composition and localization boundary tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.config import Category, Config, load_config
from curator.localization import (
    build_localized_view,
    story_id_for_item,
    write_localized_projection,
)
from curator.models import Item, TranslationRecord
from curator.pipeline import build_ranked_language, collect, configured_source_specs
from curator.sources import SafeHttpResponse, SourceValidationError
from curator.translation import TranslationInput


NOW = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc)


def item(title, *, language="en", url="https://example.com/story", image=""):
    return Item(
        title=title,
        url=url,
        canonical_url=url,
        source_id=f"source-{language}",
        source_name=f"Source {language}",
        published_at=NOW,
        language=language,
        image_url=image,
    )


def config(*, rss=(), translation=None):
    return Config(
        categories=[Category(name="AI", id="ai", keywords=["AI"], keywords_by_language={"zh": ["人工智能"]})],
        rss=list(rss),
        settings={
            "max_age_hours": 48,
            "user_agent": "news-curator-integration/3 (+https://example.com/contact)",
        },
        ranking={},
        dedup={},
        hackernews={"enabled": False},
        reddit={"enabled": True, "subreddits": ["must-not-run"]},
        translation=translation or {},
    )


def test_generic_sources_key_accepts_standard_formats_with_policy(tmp_path):
    (tmp_path / "topics.yaml").write_text(
        "categories:\n  - {id: ai, name: AI, keywords: [AI]}\n", encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text(
        """sources:
  - {type: atom, id: atom-one, url: 'https://example.com/atom', request_timeout_seconds: 3}
  - {type: json_feed, id: json-one, url: 'https://example.com/feed.json', max_response_bytes: 4096}
hackernews: {enabled: false}
""",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    specs = configured_source_specs(cfg)

    assert [spec.type for spec in specs] == ["atom", "json_feed"]
    assert specs[0].request_timeout_seconds == 3
    assert specs[1].max_response_bytes == 4096


def test_reddit_config_is_not_collected_or_reported_even_when_enabled():
    result = collect(config())

    assert len(result) == 1
    assert result[0].tier == "sources"
    assert result[0].ok is True
    assert result[0].source_health == []
    assert "reddit" not in result[0].note.lower()


def test_collect_rejects_unsafe_configured_application_identity():
    cfg = config()
    cfg.settings["user_agent"] = "news-curator\r\nInjected: yes"

    with pytest.raises(SourceValidationError, match="user agent is invalid"):
        collect(cfg)


def test_real_config_to_registry_collect_category_and_rank_is_nonempty(tmp_path):
    (tmp_path / "topics.yaml").write_text(
        "categories:\n  - {id: apple, name: Apple, keywords: [Apple]}\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        """settings:
  max_age_hours: 48
sources:
  - type: json_feed
    id: captured-json
    name: Captured JSON Feed
    url: https://example.com/feed.json
    is_aggregator: false
hackernews: {enabled: false}
""",
        encoding="utf-8",
    )
    payload = (
        Path(__file__).parent / "fixtures" / "sources" / "daring-fireball.json"
    ).read_bytes()

    class CapturedTransport:
        def __init__(self):
            self.calls = []

        def get(self, source_id, url, **kwargs):
            self.calls.append((source_id, url, kwargs))
            return SafeHttpResponse(
                200, url, {"content-type": "application/feed+json"}, payload
            )

    cfg = load_config(tmp_path)
    transport = CapturedTransport()
    results = collect(cfg, transport=transport, clock=lambda: NOW)
    ranked = build_ranked_language(cfg, results, NOW, language="en")

    assert [spec.id for spec in configured_source_specs(cfg)] == ["captured-json"]
    assert transport.calls[0][0] == "captured-json"
    assert transport.calls[0][2]["user_agent"] == cfg.user_agent
    assert results[0].ok is True
    assert [row.title for row in ranked["Apple"]] == [
        "★ Thoughts and Observations on Apple’s First Immersive MLB Broadcast, a Yankees 1-0 Win Over the Red Sox",
        "Apple Announces Price Increase for Apple TV and Apple One Subscriptions",
    ]


def test_translation_never_creates_a_category_match():
    original = item("Gardening story")
    cfg = config()
    ranked_en = build_ranked_language(
        cfg,
        [type("Tier", (), {"items": [original]})()],
        NOW,
        language="en",
    )
    content = TranslationInput.from_item(original)
    translation = TranslationRecord(
        story_id=story_id_for_item(original),
        input_digest=content.digest,
        source_language="en",
        target_language="zh",
        title="人工智能新闻",
        description="",
        provider="google",
        model_version="google-nmt-v3",
    )

    localized = build_localized_view(
        target_language="zh",
        native_ranked={"AI": []},
        source_ranked=ranked_en,
        translations=(translation,),
    )

    assert ranked_en["AI"] == []
    assert localized["AI"] == []


def test_native_target_language_wins_over_translation_for_same_story():
    english = item("AI story", language="en")
    chinese = item("人工智能原生报道", language="zh")
    content = TranslationInput.from_item(english)
    record = TranslationRecord(
        story_id=story_id_for_item(english),
        input_digest=content.digest,
        source_language="en",
        target_language="zh",
        title="人工智能翻译报道",
        description="",
        provider="google",
        model_version="google-nmt-v3",
    )

    localized = build_localized_view(
        target_language="zh",
        native_ranked={"AI": [chinese]},
        source_ranked={"AI": [english]},
        translations=(record,),
    )

    assert [row.title for row in localized["AI"]] == ["人工智能原生报道"]
    assert localized["AI"][0].translated is False
    assert localized["AI"][0].translation_available is True
    assert localized["AI"][0].translation_provider == "google"
    assert localized["AI"][0].translation_source_language == "en"


def test_projection_is_json_safe_and_carries_original_identity(tmp_path):
    original = item("AI <script> story", image="https://example.com/image.jpg")
    localized = build_localized_view(
        target_language="en",
        native_ranked={"AI": [original]},
        source_ranked={"AI": []},
        translations=(),
    )
    path = tmp_path / "data/news-en.json"

    write_localized_projection(
        language="en",
        categories=config().categories,
        ranked=localized,
        path=path,
        generated_at=NOW,
    )

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    projected = payload["categories"][0]["items"][0]
    assert "<script>" not in raw
    assert projected["story_id"] == story_id_for_item(original)
    assert projected["translated"] is False
    assert projected["published_at"] == NOW.isoformat()
    assert "day_bucket" not in projected
