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


def _pair_items():
    from datetime import timedelta
    en = Item(title="Nvidia beats on earnings with 3 Blackwell chips", url="https://e.com/1",
              canonical_url="https://e.com/1", source_id="fixture", source_name="Fixture",
              published_at=NOW, language="en", description="")
    zh = Item(title="英伟达 Nvidia 发布 3 款 Blackwell 芯片 earnings", url="https://e.cn/1",
              canonical_url="https://e.cn/1", source_id="fixture", source_name="Fixture",
              published_at=NOW - timedelta(hours=1), language="zh", description="")
    lone = Item(title="独家：某部门发布 7 项新规", url="https://e.cn/2", canonical_url="https://e.cn/2",
                source_id="fixture", source_name="Fixture", published_at=NOW, language="zh", description="")
    return en, zh, lone


def test_retain_assigns_an_event_group_to_a_cross_language_pair():
    from curator.retained_corpus import language_exclusive_story_ids
    en, zh, lone = _pair_items()
    rows = retain([en, zh, lone], categories=[], observed_at=NOW)
    by_language = {row.item.language + row.item.canonical_url: row for row in rows}
    grouped = [row for row in rows if row.event_group_id]
    assert len(grouped) == 2 and len({row.event_group_id for row in grouped}) == 1
    assert by_language["zhhttps://e.cn/2"].event_group_id is None


def test_language_exclusive_ids_exclude_a_story_an_english_outlet_also_carried():
    from curator.retained_corpus import language_exclusive_story_ids
    en, zh, lone = _pair_items()
    rows = retain([en, zh, lone], categories=[], observed_at=NOW)
    exclusive = language_exclusive_story_ids(rows, display_language="en")
    lone_id = next(row.story_id for row in rows if row.item.canonical_url == "https://e.cn/2")
    paired_zh_id = next(row.story_id for row in rows if row.item.canonical_url == "https://e.cn/1")
    assert lone_id in exclusive and paired_zh_id not in exclusive


def test_ingest_rows_carry_the_overlay_only_when_present():
    from curator.retained_corpus import apply_translations
    from curator.translation.ingest import TranslationOverlay, TRANSLATED
    en, zh, lone = _pair_items()
    rows = retain([lone], categories=[], observed_at=NOW)
    story_id = rows[0].story_id
    plain = public_ingest_rows(rows, allowed_source_ids={"fixture"})
    assert "title_translations" not in plain[0] and "event_group_id" not in plain[0]
    translated = apply_translations(rows, {story_id: TranslationOverlay(
        {"en": "Exclusive: 7 new rules"}, {}, TRANSLATED)})
    payload = public_ingest_rows(translated, allowed_source_ids={"fixture"})
    assert payload[0]["title_translations"] == {"en": "Exclusive: 7 new rules"}


def test_grouping_sees_stories_ingested_by_an_earlier_run():
    """Twelve ingests an hour: the pair almost never lands in one batch."""
    from curator.grouping import GroupingCandidate
    from curator.retained_corpus import language_exclusive_story_ids, regroup_with_corpus
    english = Item(title="Nvidia beats on earnings with 3 Blackwell chips",
                   url="https://e.com/1", canonical_url="https://e.com/1", source_id="fixture",
                   source_name="Fixture", published_at=NOW, language="en",
                   description="Nvidia reported revenue of 46 billion dollars, 56 percent above a year "
                               "ago, and said 7 new sites are live in 2026.")
    chinese = Item(title="英伟达 Nvidia 发布 Blackwell 芯片 earnings 超预期",
                   url="https://e.cn/1", canonical_url="https://e.cn/1", source_id="fixture",
                   source_name="Fixture", published_at=NOW, language="zh",
                   description="英伟达公布季度营收 467 亿美元，同比增长 56%，并称 2026 年将有 3 座新数据中心投入使用。")
    # Run N ingested the English story; run N+1 brings the Chinese one alone.
    earlier = retain([english], categories=[], observed_at=NOW)
    batch = retain([chinese], categories=[], observed_at=NOW)
    assert batch[0].event_group_id is None
    corpus = tuple(GroupingCandidate(story_id=row.story_id, language=row.item.language,
                                     title=row.item.title, summary=row.item.description,
                                     published_at=row.item.published_at,
                                     event_group_id=row.event_group_id) for row in earlier)
    regrouped = regroup_with_corpus(batch, corpus)
    assert regrouped[0].event_group_id is not None
    # And it is therefore NOT language exclusive: an English outlet ran it.
    covered = tuple(GroupingCandidate(story_id=row.story_id, language=row.language, title=row.title,
                                      summary=row.summary, published_at=row.published_at,
                                      event_group_id=regrouped[0].event_group_id) for row in corpus)
    assert language_exclusive_story_ids(regrouped, display_language="en", corpus=covered) == ()


def test_regrouping_never_downgrades_an_id_the_row_already_carries():
    from curator.retained_corpus import regroup_with_corpus
    lone = Item(title="独家：某部门发布 7 项新规", url="https://e.cn/2", canonical_url="https://e.cn/2",
                source_id="fixture", source_name="Fixture", published_at=NOW, language="zh",
                description="该通知自 2026 年起执行。")
    rows = retain([lone], categories=[], observed_at=NOW)
    kept = "group:" + "9" * 32
    rows = (replace(rows[0], event_group_id=kept),)
    assert regroup_with_corpus(rows, ())[0].event_group_id == kept
