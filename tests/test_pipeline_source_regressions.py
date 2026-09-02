"""Regression coverage for bilingual backend and source-rank boundaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from curator.config import load_config
from curator.localization import story_id_for_item, write_translation_artifact
from curator.models import Item, TierResult, TranslationRecord
from curator.pipeline import main
from curator.source_snapshot import snapshot_config_digest, write_source_snapshot
from curator.translation import TranslationInput


def test_chinese_only_snapshot_writes_both_backend_views_and_a_current_index(
    tmp_path, monkeypatch
):
    (tmp_path / "topics.yaml").write_text(
        """categories:
  - id: ai
    name: AI
    keywords: [AI]
    keywords_by_language:
      zh: [人工智能]
""",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        """settings:
  max_age_hours: 48
  user_agent: news-curator-tests/3
sources: []
hackernews: {enabled: false}
images: {enabled: false}
""",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    original = Item(
        title="人工智能模型发布",
        description="来源摘要",
        url="https://example.cn/ai-model",
        canonical_url="https://example.cn/ai-model",
        source_id="publisher-zh",
        source_name="中文来源",
        published_at=now,
        language="zh",
    )
    content = TranslationInput.from_item(original)
    translation_path = tmp_path / "translations.json"
    write_translation_artifact(
        (
            TranslationRecord(
                story_id=story_id_for_item(original),
                input_digest=content.digest,
                source_language="zh",
                target_language="en",
                title="AI model released",
                description="Publisher summary",
                provider="google",
                model_version="google-nmt-v3",
            ),
        ),
        translation_path,
        generated_at=now,
    )
    cfg = load_config(tmp_path)
    snapshot_path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        (TierResult("sources", [original], True),),
        snapshot_path,
        generated_at=now,
        configuration_digest=snapshot_config_digest(cfg),
    )
    enriched = []

    def fake_enrich(items, *_args, **_kwargs):
        enriched.extend(items)
        for item in items:
            item.image_url = "https://images.example.cn/ai-model.jpg"
        return {
            "total": len(items),
            "from_feed": 0,
            "from_cache": 0,
            "fetched": len(items),
            "no_image": 0,
            "errors": 0,
            "capped": 0,
            "budget_hit": 0,
            "newsletter_skipped": 0,
        }

    monkeypatch.setattr("curator.pipeline.enrich", fake_enrich)
    out = tmp_path / "site"
    out.mkdir()
    existing_html = out / "index.html"
    existing_html.write_text("keep the last non-empty page", encoding="utf-8")

    code = main(
        [
            "--root",
            str(tmp_path),
            "--out",
            str(out),
            "--source-snapshot",
            str(snapshot_path),
            "--translation-artifact",
            str(translation_path),
        ]
    )

    assert code == 0
    assert [item.canonical_url for item in enriched] == [original.canonical_url]
    rendered = existing_html.read_text(encoding="utf-8")
    assert rendered != "keep the last non-empty page"
    assert "人工智能模型发布" in rendered
    en = json.loads((out / "data/news-en.json").read_text(encoding="utf-8"))
    zh = json.loads((out / "data/news-zh.json").read_text(encoding="utf-8"))
    en_item = en["categories"][0]["items"][0]
    zh_item = zh["categories"][0]["items"][0]
    assert en_item["translated"] is True
    assert zh_item["translated"] is False
    assert en_item["image_url"] == zh_item["image_url"]
    assert zh_item["image_url"] == "https://images.example.cn/ai-model.jpg"


def test_malicious_newsletter_urls_cannot_enter_public_projection(
    tmp_path, monkeypatch
):
    (tmp_path / "topics.yaml").write_text(
        "categories:\n  - id: ai\n    name: AI\n    keywords: [AI]\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        "settings: {max_age_hours: 48}\n"
        "sources: []\n"
        "hackernews: {enabled: false}\n"
        "images: {enabled: false}\n",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    cfg = load_config(tmp_path)
    snapshot_path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        (TierResult("sources", [], True),),
        snapshot_path,
        generated_at=now,
        configuration_digest=snapshot_config_digest(cfg),
    )
    newsletter_path = tmp_path / "newsletter.json"
    newsletter_path.write_text(
        json.dumps(
            {
                "dark": False,
                "ok": True,
                "watermark": None,
                "hashes": [],
                "status": {},
                "items": [
                    {
                        "title": "AI newsletter unsafe link",
                        "url": "https://trusted.example@evil.example/story",
                        "canonical_url": "javascript:alert(document.domain)",
                        "source_id": "newsletter:test",
                        "source_name": "Test Newsletter",
                        "published_at": now.isoformat(),
                        "image_url": "https://tracker.example/pixel.gif",
                    },
                    {
                        "title": "AI newsletter canonical fallback",
                        "url": "https://publisher.example/story?utm_source=mail",
                        "canonical_url": "https://publisher.example@evil.example/story",
                        "source_id": "newsletter:test",
                        "source_name": "Test Newsletter",
                        "published_at": now.isoformat(),
                        "image_url": "data:image/gif;base64,tracking",
                    },
                    {
                        "title": "AI newsletter encoded address",
                        "url": "https://publisher.example/story?email=reader%40example.invalid",
                        "canonical_url": "https://publisher.example/story?email=reader%40example.invalid",
                        "source_id": "newsletter:test",
                        "source_name": "Test Newsletter",
                        "published_at": now.isoformat(),
                        "image_url": "https://tracker.example/pixel.gif",
                    },
                    {
                        "title": "AI newsletter opaque tracker",
                        "url": "https://link.mail.beehiiv.com/ss/c/AbCdEf0123456789XyZq",
                        "canonical_url": "https://link.mail.beehiiv.com/ss/c/AbCdEf0123456789XyZq",
                        "source_id": "newsletter:test",
                        "source_name": "Test Newsletter",
                        "published_at": now.isoformat(),
                        "image_url": "https://tracker.example/pixel.gif",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "curator.pipeline.enrich",
        lambda *_args, **_kwargs: {
            "total": 0,
            "from_feed": 0,
            "from_cache": 0,
            "fetched": 0,
            "no_image": 0,
            "errors": 0,
            "capped": 0,
            "budget_hit": 0,
            "newsletter_skipped": 0,
        },
    )
    out = tmp_path / "site"

    assert main(
        [
            "--root",
            str(tmp_path),
            "--out",
            str(out),
            "--source-snapshot",
            str(snapshot_path),
            "--newsletter-artifact",
            str(newsletter_path),
        ]
    ) == 0

    projection = json.loads((out / "data/news-en.json").read_text(encoding="utf-8"))
    newsletter = next(row for row in projection["categories"] if row["id"] == "newsletters")
    items = {item["title"]: item for item in newsletter["items"]}
    assert items["AI newsletter unsafe link"]["url"] == ""
    assert items["AI newsletter unsafe link"]["canonical_url"] == ""
    assert items["AI newsletter canonical fallback"]["url"] == (
        "https://publisher.example/story"
    )
    assert items["AI newsletter canonical fallback"]["canonical_url"] == (
        "https://publisher.example/story"
    )
    assert items["AI newsletter encoded address"]["url"] == ""
    assert items["AI newsletter encoded address"]["canonical_url"] == ""
    assert items["AI newsletter opaque tracker"]["url"] == ""
    assert items["AI newsletter opaque tracker"]["canonical_url"] == ""
    assert all(item["image_url"] == "" for item in items.values())

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "reader%40example.invalid" not in html
    assert "reader@example.invalid" not in html
    assert "link.mail.beehiiv.com" not in html
    assert "https://publisher.example/story" in html


def test_non_trending_feed_position_cannot_replace_hackernews_trending_rank(
    tmp_path
):
    from curator.dedup import dedupe
    from curator.sources import build_builtin_registry
    from curator.sources.feed import parse_feed_document

    registry = build_builtin_registry()
    publisher_spec = registry.parse_spec(
        {
            "type": "rss",
            "id": "publisher",
            "name": "Publisher",
            "url": "https://publisher.example/feed.xml",
            "category": "ai",
        }
    )
    payload = b"""<rss version='2.0'><channel><item>
<title>AI platform release</title>
<link>https://publisher.example/story</link>
<pubDate>Sat, 29 Aug 2026 12:00:00 GMT</pubDate>
</item></channel></rss>"""
    now = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    (publisher,) = parse_feed_document(payload, publisher_spec, now)
    hackernews = Item(
        title="AI platform release",
        url=publisher.url,
        canonical_url=publisher.canonical_url,
        source_id="hackernews",
        source_name="Hacker News",
        platform="hackernews",
        published_at=publisher.published_at,
        is_aggregator=True,
        native_rank=7,
        native_categories={"trending"},
    )
    assert publisher.native_rank is None

    (survivor,) = dedupe([publisher, hackernews])

    assert survivor.source_id == "publisher"
    assert survivor.native_rank == 7
    assert survivor.native_categories == {"ai", "trending"}
