from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from curator.config import Category
from curator.models import Item
from dataclasses import replace
from curator.retained_corpus import candidate_response, candidates, public_ingest_rows, retain


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def item(title: str, url: str, *, language: str = "en") -> Item:
    return Item(title=title, description="publisher source", url=url, canonical_url=url,
                source_id="fixture", source_name="Fixture", published_at=NOW, language=language)


def test_retained_corpus_keeps_items_outside_published_page_and_filters_category():
    category = Category(id="us-news", name="US News", keywords=["Congress"])
    rows = retain([item("Congress passes bill", "https://example.com/a"), item("Other story", "https://example.com/b")], categories=[category], observed_at=NOW)
    assert len(rows) == 2
    assert [row.item.title for row in candidates(rows, category_id="us-news")] == ["Congress passes bill"]


def test_retained_corpus_preserves_native_category_when_no_keyword_matches():
    category = Category(id="us-news", name="US News", keywords=["Congress"])
    native = item("Regional weather update", "https://example.com/native")
    native.native_categories.add("us-news")
    rows = retain([native], categories=[category], observed_at=NOW)
    assert rows[0].category_ids == frozenset({"us-news"})


def test_retained_corpus_cjk_search_uses_real_existing_corpus_title():
    fixture = Path(__file__).parent / "fixtures" / "feeds" / "cnbeta.xml"
    title = ElementTree.parse(fixture).findtext("./channel/item/title")
    assert title
    rows = retain([item(title, "https://example.com/zh", language="zh")], categories=[], observed_at=NOW)
    assert [row.item.title for row in candidates(rows, query="生成式人工智能")] == [title]


def test_candidate_response_is_versioned_and_not_an_m1_card_shape():
    rows = retain([item("Congress passes bill", "https://example.com/a")], categories=[], observed_at=NOW)
    response = candidate_response(rows)
    assert response["schema_version"] == 1
    assert response["candidates"][0]["source_observed_at"] == NOW.isoformat()


def test_public_ingest_rows_exclude_newsletters():
    story = item("Newsletter", "https://example.com/news")
    story.is_newsletter = True
    rows = retain([story], categories=[], observed_at=NOW)
    try:
        public_ingest_rows(rows, allowed_source_ids={"fixture"})
    except ValueError as exc:
        assert "newsletter" in str(exc)
    else:
        raise AssertionError("newsletter must be rejected")


def test_public_ingest_rows_reject_unknown_source():
    rows = retain([item("Unknown", "https://example.com/unknown")], categories=[], observed_at=NOW)
    try:
        public_ingest_rows(rows, allowed_source_ids=set())
    except ValueError:
        pass
    else:
        raise AssertionError("unknown source must be rejected")


def test_public_ingest_rows_preserve_strict_aggregator_attribution():
    publisher = item("Publisher", "https://example.com/publisher")
    aggregator = item("Aggregator", "https://example.com/aggregator")
    aggregator.is_aggregator = True
    rows = retain([publisher, aggregator], categories=[], observed_at=NOW)
    payload = public_ingest_rows(rows, allowed_source_ids={"fixture"})
    assert {row["source_is_aggregator"] for row in payload} == {False, True}
    publisher.is_aggregator = "false"  # type: ignore[assignment]
    try:
        public_ingest_rows(retain([publisher], categories=[], observed_at=NOW), allowed_source_ids={"fixture"})
    except ValueError as exc:
        assert "boolean" in str(exc)
    else:
        raise AssertionError("non-boolean aggregator attribution must be rejected")


def test_corpus_coalesces_language_variants_without_changing_m1_deduper():
    # Reuse the captured Guardian headline pair from the M1 source regression.
    from curator.dedup import dedupe
    url = "https://www.theguardian.com/us-news/2026/aug/29/trump-dhs-1509-summons-records-journalists-nonprofits"
    english = item("DHS is using obscure law to snoop on journalists, non-profits, unions", url)
    chinese = item("美国国土安全部正利用一条鲜为人知的法律对记者、非营利组织和工会进行监视", url, language="zh")
    english.native_categories.add("us-news")
    chinese.native_categories.add("world")
    assert len(dedupe([english, chinese])) == 2
    rows = retain([english, chinese], categories=[
        Category(id="us-news", name="US News", keywords=[]),
        Category(id="world", name="World", keywords=[]),
    ], observed_at=NOW)
    assert len(rows) == 1
    assert rows[0].category_ids == frozenset({"us-news", "world"})


def test_retain_claims_only_certain_cross_language_matches():
    """The heuristic is gone. Only an identical title or URL is claimed here.

    Two rows sharing a canonical URL already coalesce into one retained row
    upstream (story_id is derived from that URL), so the case that actually
    reaches the pre-filter is a Chinese-language outlet running the English
    headline verbatim.
    """
    en = Item(title="Nvidia beats on earnings", url="https://e.com/1", canonical_url="https://e.com/1",
              source_id="fixture", source_name="Fixture", published_at=NOW, language="en",
              description="Revenue of 46 billion dollars in 2026.")
    zh = Item(title="Nvidia beats on earnings", url="https://e.cn/1", canonical_url="https://e.cn/1",
              source_id="fixture", source_name="Fixture", published_at=NOW, language="zh",
              description="2026 年营收 467 亿美元。")
    unrelated = Item(title="某地铁线路延长 3 公里", url="https://e.cn/2", canonical_url="https://e.cn/2",
                     source_id="fixture", source_name="Fixture", published_at=NOW, language="zh",
                     description="该工程于 2026 年完工。")
    rows = retain([en, zh, unrelated], categories=[], observed_at=NOW)
    grouped = [row for row in rows if row.event_group_id]
    assert len(grouped) == 2 and len({row.event_group_id for row in grouped}) == 1
    assert next(row for row in rows if row.item.canonical_url == "https://e.cn/2").event_group_id is None


def test_ingest_rows_carry_the_overlay_only_when_present():
    from curator.retained_corpus import apply_translations
    from curator.translation.ingest import TranslationOverlay, TRANSLATED
    lone = Item(title="独家：某部门发布七项新规", url="https://e.cn/2", canonical_url="https://e.cn/2",
                source_id="fixture", source_name="Fixture", published_at=NOW, language="zh",
                description="该通知自 2026 年起执行。")
    rows = retain([lone], categories=[], observed_at=NOW)
    story_id = rows[0].story_id
    plain = public_ingest_rows(rows, allowed_source_ids={"fixture"})
    assert "title_translations" not in plain[0] and "event_group_id" not in plain[0]
    translated = apply_translations(rows, {story_id: TranslationOverlay(
        {"en": "Exclusive: 7 new rules"}, {}, TRANSLATED)})
    payload = public_ingest_rows(translated, allowed_source_ids={"fixture"})
    assert payload[0]["title_translations"] == {"en": "Exclusive: 7 new rules"}
