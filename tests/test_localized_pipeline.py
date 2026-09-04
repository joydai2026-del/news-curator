"""Nonempty proof that localization is a one-way presentation overlay."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from curator.config import Category, Config
from curator.localization import build_localized_view, story_id_for_item
from curator.models import Item, TierResult, TranslationRecord
from curator.pipeline import build_ranked_language
from curator.translation import TranslationInput


NOW = datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)


def _cfg() -> Config:
    return Config(
        categories=[Category(
            name="AI",
            id="ai",
            keywords=["AI", "model"],
            keywords_by_language={"zh": ["人工智能", "模型"]},
        )],
        rss=[],
        settings={"max_age_hours": 48},
        ranking={},
        dedup={"title_similarity": 0.9},
        hackernews={"enabled": False},
        reddit={},
    )


def _items() -> list[Item]:
    return [
        Item(
            title="AI lab releases model 7",
            description="Publisher summary one",
            url="https://example.com/one",
            canonical_url="https://example.com/one",
            source_id="publisher-a",
            source_name="Publisher A",
            published_at=NOW,
            language="en",
            source_weight=1.2,
        ),
        Item(
            title="AI research improves model 6",
            description="Publisher summary two",
            url="https://example.com/two",
            canonical_url="https://example.com/two",
            source_id="publisher-b",
            source_name="Publisher B",
            published_at=NOW - timedelta(hours=2),
            language="en",
            source_weight=0.8,
        ),
        Item(
            title="AI lab releases model 7",
            description="Aggregator wording must not win",
            url="https://example.com/one?ref=aggregator",
            canonical_url="https://example.com/one",
            source_id="aggregator",
            source_name="Aggregator",
            published_at=NOW + timedelta(minutes=1),
            language="en",
            is_aggregator=True,
        ),
    ]


def _fingerprint(ranked: dict[str, list[Item]]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            story_id_for_item(item),
            item.title,
            item.description,
            item.url,
            tuple(item.matched_keywords),
            tuple(sorted(item.echo_platforms)),
        )
        for item in ranked["AI"]
    )


def test_nonempty_localization_cannot_change_original_dedupe_category_rank_or_order() -> None:
    original_ranked = build_ranked_language(
        _cfg(), [TierResult("sources", _items(), True)], NOW, language="en"
    )
    before = _fingerprint(copy.deepcopy(original_ranked))
    translations = []
    for index, original in enumerate(original_ranked["AI"], start=1):
        content = TranslationInput.from_item(original)
        translations.append(
            TranslationRecord(
                story_id=story_id_for_item(original),
                input_digest=content.digest,
                source_language="en",
                target_language="zh",
                title=f"人工智能翻译 {index}",
                description=f"翻译摘要 {index}",
                provider="google",
                model_version="google-nmt-v3",
            )
        )

    localized = build_localized_view(
        target_language="zh",
        native_ranked={"AI": []},
        source_ranked=original_ranked,
        translations=translations,
    )

    assert _fingerprint(original_ranked) == before
    assert [row.story_id for row in localized["AI"]] == [row[0] for row in before]
    assert [row.original.title for row in localized["AI"]] == [row[1] for row in before]
    assert all(row.translated and row.translation_available for row in localized["AI"])


def test_nonempty_zh_to_en_localization_cannot_change_original_rank_or_order() -> None:
    chinese = [
        Item(
            title="人工智能模型发布第七版",
            description="来源摘要一",
            url="https://example.cn/one",
            canonical_url="https://example.cn/one",
            source_id="publisher-zh-a",
            source_name="中文来源 A",
            published_at=NOW,
            language="zh",
            source_weight=1.2,
        ),
        Item(
            title="人工智能研究改进模型",
            description="来源摘要二",
            url="https://example.cn/two",
            canonical_url="https://example.cn/two",
            source_id="publisher-zh-b",
            source_name="中文来源 B",
            published_at=NOW - timedelta(hours=2),
            language="zh",
            source_weight=0.8,
        ),
    ]
    original_ranked = build_ranked_language(
        _cfg(), [TierResult("sources", chinese, True)], NOW, language="zh"
    )
    before = _fingerprint(copy.deepcopy(original_ranked))
    translations = []
    for index, original in enumerate(original_ranked["AI"], start=1):
        content = TranslationInput.from_item(original)
        translations.append(
            TranslationRecord(
                story_id=story_id_for_item(original),
                input_digest=content.digest,
                source_language="zh",
                target_language="en",
                title=f"AI translated story {index}",
                description=f"Translated summary {index}",
                provider="google",
                model_version="google-nmt-v3",
            )
        )

    localized = build_localized_view(
        target_language="en",
        native_ranked={"AI": []},
        source_ranked=original_ranked,
        translations=translations,
    )

    assert _fingerprint(original_ranked) == before
    assert [row.story_id for row in localized["AI"]] == [row[0] for row in before]
    assert [row.original.title for row in localized["AI"]] == [row[1] for row in before]
    assert all(row.translated and row.translation_available for row in localized["AI"])
