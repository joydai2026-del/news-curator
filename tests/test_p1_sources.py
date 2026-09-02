"""P1 backend contracts for multilingual source coverage and freshness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from curator.config import Category, ConfigError, RssSource, load_config
from curator.dedup import dedupe
from curator.fetchers import hn, rss
from curator.filter import assign_categories
from curator.models import Item
from curator.pipeline import build_language_view
from curator.rank import keyword_score, rank_items
from tests.conftest import NOW, make_item


FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def _source(**overrides) -> RssSource:
    values = {
        "id": "cnn-news",
        "name": "CNN",
        "url": "https://www.cnn.com/sitemap/news.xml",
        "max_age_hours": 6,
    }
    values.update(overrides)
    return RssSource(**values)


class TestSourceContract:
    def test_legacy_source_defaults_are_additive(self):
        source = _source(max_age_hours=None)
        assert source.type == "rss"
        assert source.language == "en"
        assert source.echo_eligible is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [("type", "html"), ("language", "auto"), ("max_age_hours", 0), ("echo_eligible", "no")],
    )
    def test_invalid_source_values_are_rejected(self, tmp_path, field, value):
        (tmp_path / "topics.yaml").write_text("topics: []\n", encoding="utf-8")
        (tmp_path / "sources.yaml").write_text(
            "rss:\n  - id: bad\n    url: https://example.com/feed\n"
            f"    {field}: {value!r}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(tmp_path)


class TestCapturedNewsSitemaps:
    def test_cnn_fixture_uses_news_date_not_lastmod(self):
        source = _source(type="news_sitemap", language="en")
        items = rss.parse_document((FIXTURES / "cnn-news.xml").read_bytes(), source)
        assert items[0].title == (
            "Death toll rises and thousands still missing in Nepal and China after catastrophic floods"
        )
        assert items[0].published_at == datetime(2026, 8, 29, 5, 13, 16, 172000, tzinfo=timezone.utc)
        assert items[0].image_url.startswith("https://media.cnn.com/")
        assert items[0].language == "en"

    def test_fox_fixture_normalizes_offset_to_utc(self):
        source = _source(id="fox-news", name="Fox News", type="news_sitemap")
        (item,) = rss.parse_document((FIXTURES / "fox-news.xml").read_bytes(), source)
        assert item.published_at == datetime(2026, 8, 29, 19, 58, 13, tzinfo=timezone.utc)

    @pytest.mark.parametrize("declaration", [b"<!DOCTYPE urlset>", b"<!ENTITY injected 'x'>"])
    def test_dtd_and_entity_declarations_are_rejected(self, declaration):
        payload = b"<?xml version='1.0'?>" + declaration + b"<urlset/>"
        with pytest.raises(ValueError, match="DTD|entity"):
            rss.parse_document(payload, _source(type="news_sitemap"))

    def test_lastmod_is_never_a_publication_fallback(self):
        payload = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://www.cnn.com/2026/08/29/world/live-news/nepal-china-flood</loc><lastmod>2026-08-29T20:25:26.601000+00:00</lastmod></url>
        </urlset>"""
        assert rss.parse_document(payload, _source(type="news_sitemap")) == []


class TestCapturedDwRdf:
    def test_item_dc_date_wins_over_channel_generation_date(self):
        source = _source(id="dw-zh", name="DW Chinese", language="zh", max_age_hours=12)
        items = rss.parse_document((FIXTURES / "dw-zh.rdf").read_bytes(), source)
        assert [item.published_at for item in items] == [
            datetime(2026, 8, 29, 12, 52, tzinfo=timezone.utc),
            datetime(2025, 8, 20, 13, 21, tzinfo=timezone.utc),
        ]
        assert all(item.language == "zh" for item in items)

        cutoff = datetime(2026, 8, 29, 20, 30, tzinfo=timezone.utc) - timedelta(hours=48)
        view = build_language_view(items, "zh", cutoff=cutoff)
        assert [item.title for item in view] == ["中国军备迅速扩张：仅仅是为了自卫吗？"]


class TestLanguageAndBuckets:
    def test_exact_and_fuzzy_dedup_do_not_cross_languages(self):
        url = "https://www.theguardian.com/us-news/2026/aug/29/trump-dhs-1509-summons-records-journalists-nonprofits"
        en = make_item("DHS is using obscure law to snoop on journalists, non-profits, unions", url)
        zh = make_item("美国国土安全部正利用一条鲜为人知的法律对记者、非营利组织和工会进行监视", url)
        zh.language = "zh"
        assert len(dedupe([en, zh])) == 2

        zh.canonical_url = "https://i.buzzing.cc/hn/posts/2026/35/en_hn_2026_08_29__49492219/"
        assert len(dedupe([en, zh])) == 2

    def test_build_language_view_partitions_before_dedup_and_assignment(self):
        url = "https://www.theguardian.com/us-news/2026/aug/29/trump-dhs-1509-summons-records-journalists-nonprofits"
        en = make_item("DHS is using obscure law to snoop on journalists, non-profits, unions", url)
        zh = make_item("美国国土安全部正利用一条鲜为人知的法律对记者、非营利组织和工会进行监视", url)
        zh.language = "zh"
        assert len(build_language_view([en, zh], "en")) == 1
        assert len(build_language_view([en, zh], "zh")) == 1

    def test_today_yesterday_and_older_are_pure(self):
        base = Item(
            title="Beer bandits swipe PBR truck hauling 50,000 cans in California, leaving brand pleading for help",
            url="https://www.foxnews.com/us/beer-bandits-swipe-pbr-truck-hauling-50000-cans-california-leaving-brand-pleading-help",
            canonical_url="https://foxnews.com/us/beer-bandits-swipe-pbr-truck-hauling-50000-cans-california-leaving-brand-pleading-help",
            source_id="fox-news",
            source_name="Fox News",
            published_at=datetime(2026, 8, 29, 1, tzinfo=timezone.utc),
        )
        now = datetime(2026, 8, 29, 23, tzinfo=timezone.utc)
        assert base.day_bucket(now) == "today"
        base.published_at -= timedelta(days=1)
        assert base.day_bucket(now) == "yesterday"
        base.published_at -= timedelta(days=1)
        assert base.day_bucket(now) == "older"

    def test_cbs_and_yahoo_real_titles_use_normal_us_assignment(self):
        cfg = load_config(Path(__file__).resolve().parent.parent)
        rows = json.loads((FIXTURES / "category-routing.json").read_text())["items"]
        categories = {category.id: category for category in cfg.categories}

        for row in rows:
            if row["source_id"] not in {"cbs-news", "yahoo-news"}:
                continue
            item = make_item(
                row["title"],
                row["url"],
                source_id=row["source_id"],
                source_name=row["source_id"],
            )
            item.language = row["language"]
            buckets = assign_categories([item], cfg.categories)
            us_rows = buckets[categories["us-news"].name]
            if "us-news" in row.get("expected", []):
                assert [story.title for story in us_rows] == [row["title"]]
            if "us-news" in row.get("excluded", []):
                assert us_rows == []

    @pytest.mark.parametrize(
        ("source_id", "fixture", "category_id", "matched"),
        [
            ("cnbeta", "cnbeta.xml", "ai", "人工智能"),
            ("solidot", "solidot.xml", "ai", "生成式 AI"),
            ("google-36kr", "google-36kr.xml", "business", "融资"),
        ],
    )
    def test_captured_chinese_feeds_route_and_score_by_zh_terms(
        self,
        source_id,
        fixture,
        category_id,
        matched,
    ):
        cfg = load_config(Path(__file__).resolve().parent.parent)
        source = next(source for source in cfg.rss if source.id == source_id)
        category = next(category for category in cfg.categories if category.id == category_id)
        (item,) = rss.parse_document((FIXTURES / fixture).read_bytes(), source)

        (routed,) = assign_categories([item], [category])[category.name]

        assert routed.language == "zh"
        assert routed.matched_keywords == [matched]
        assert keyword_score(routed, category, lead_chars=40, lead_bonus=0.25) > 0

    def test_keyword_score_rejects_terms_from_the_wrong_language(self):
        category = Category(
            id="ai",
            name="AI",
            keywords=["AI"],
            keywords_by_language={"zh": ["人工智能"]},
        )
        item = make_item("生成式人工智能服务开放")
        item.matched_keywords = ["人工智能"]
        assert keyword_score(item, category, lead_chars=40, lead_bonus=0.25) == 0


class TestFreshnessAndEchoEligibility:
    def test_staleness_uses_newest_usable_item_before_global_filter(self):
        source = _source(type="news_sitemap", max_age_hours=6)
        items = rss.parse_document((FIXTURES / "cnn-news.xml").read_bytes(), source)
        health = rss.source_health(source, items, items[0].published_at + timedelta(hours=100))
        assert health.status == "stale"
        assert health.usable_items == 1
        assert health.age_hours == pytest.approx(100)

    def test_google_news_item_is_kept_but_never_counts_as_corroboration(self):
        url = "https://news.google.com/rss/articles/CBMiYEFVX3lxTE16R0RWdkZUUEt0Rzd2ajNDbHdvbUFzM010eUNBUDFvWXRXWnN4MG94Z2szM1Jydm04Z3RTc0htZU92QlBWQUtCWlFXVXl6VmlCYWR5Wl9OdmNVM2ZpNUtDdA?oc=5"
        title = "Xinque Technology has completed a seed round financing of tens of millions of RMB. - 36氪"
        direct = make_item(title, url, platform="publisher")
        google = make_item(title, url, platform="google-news")
        google.echo_eligible = False
        google.echo_platforms.clear()
        survivor = dedupe([direct, google])[0]
        assert survivor.echo_platforms == {"publisher"}

    def test_google_news_limitation_is_explicit_in_health(self):
        source = _source(
            id="google-36kr",
            url="https://news.google.com/rss/search?q=site%3A36kr.com",
            language="zh",
            echo_eligible=False,
        )
        (item,) = rss.parse_document((FIXTURES / "google-36kr.xml").read_bytes(), source)
        health = rss.source_health(source, [item], item.published_at + timedelta(hours=1))
        assert health.status == "link_resolution_degraded"
        assert health.echo_eligible is False
        assert health.reason_code == "google_news_url_retained_non_corroborating"

    def test_buzzing_is_trending_but_not_corroboration(self):
        source = _source(
            id="buzzing",
            name="buzzing.cc",
            url="https://www.buzzing.cc/feed.xml",
            language="zh",
            category="trending",
            is_aggregator=True,
            echo_eligible=False,
        )
        (item,) = rss.parse_document((FIXTURES / "buzzing.xml").read_bytes(), source)
        assert item.native_categories == {"trending"}
        assert item.native_rank == 0
        assert item.echo_platforms == set()


class TestHnFrontPageIsAdditive:
    def test_front_page_is_one_additional_request_and_preserves_native_rank(self, monkeypatch):
        captured = []
        payload = __import__("json").loads((FIXTURES / "hn-front-page.json").read_text())

        def fake_query(endpoint, params, cfg):
            captured.append((endpoint, params))
            return payload["hits"] if params.get("tags") == "front_page" else []

        monkeypatch.setattr(hn, "_query", fake_query)
        cfg = load_config(Path(__file__).resolve().parent.parent)
        result = hn.fetch(cfg, [])
        front = [item for item in result.items if "trending" in item.native_categories]
        assert len([params for _, params in captured if params.get("tags") == "front_page"]) == 1
        assert [item.native_rank for item in front] == [0, 1]
        assert front[0].score == 786 and front[0].language == "en"

    def test_legacy_topic_queries_still_run_after_front_page(self, monkeypatch):
        captured = []

        def fake_query(endpoint, params, cfg):
            captured.append((endpoint, params))
            return []

        monkeypatch.setattr(hn, "_query", fake_query)
        cfg = load_config(Path(__file__).resolve().parent.parent)
        hn.fetch(cfg, [Category(name="Web", keywords=["Htmx"], hn_queries=["Htmx"])])
        assert sum(params.get("tags") == "front_page" for _, params in captured) == 1
        assert any(params.get("query") == "Htmx" and params.get("tags") == "story" for _, params in captured)

    def test_trending_keeps_captured_front_page_order_over_recency(self):
        payload = __import__("json").loads((FIXTURES / "hn-front-page.json").read_text())
        items = [
            hn._to_item(hit, 1.0, native_category="trending", native_rank=rank)
            for rank, hit in enumerate(payload["hits"])
        ]
        front = [item for item in items if item is not None]
        topic = Category(id="trending", name="Trending")
        now = max(item.published_at for item in front) + timedelta(hours=1)

        ranked = rank_items(front, topic, now, {})

        assert [item.native_rank for item in ranked] == [0, 1]
        assert [item.title for item in ranked] == [
            "Htmx 4.0",
            "Boot a Virtual iPhone via Apple's Virtualization.framework",
        ]

    def test_exact_merge_keeps_captured_front_page_rank(self):
        payload = __import__("json").loads((FIXTURES / "hn-front-page.json").read_text())
        ranked = hn._to_item(
            payload["hits"][0],
            1.0,
            native_category="trending",
            native_rank=0,
        )
        publisher = hn._to_item(payload["hits"][0], 2.0)
        assert ranked is not None and publisher is not None
        publisher.source_id = "htmx"
        publisher.source_name = "htmx"
        publisher.platform = "htmx"
        publisher.is_aggregator = False
        publisher.native_categories = {"trending"}
        publisher.native_rank = 3

        (survivor,) = dedupe([ranked, publisher])

        assert survivor.source_id == "htmx"
        assert survivor.native_categories == {"trending"}
        assert survivor.native_rank == 0

    def test_different_url_fuzzy_merge_keeps_best_trending_rank(self):
        ranked = make_item(
            "Htmx 4.0 released with faster navigation",
            "https://news.ycombinator.com/item?id=49490592",
            source_id="hn-front",
            source_name="Hacker News",
            platform="hackernews",
            aggregator=True,
            weight=1.0,
        )
        ranked.native_categories = {"trending"}
        ranked.native_rank = 0
        publisher = make_item(
            "Htmx 4.0 released with faster navigation",
            "https://htmx.org/posts/2026-08-29-htmx-4/",
            source_id="htmx",
            source_name="htmx",
            platform="htmx",
            weight=2.0,
        )
        publisher.native_categories = {"trending"}
        publisher.native_rank = 4
        second = make_item("Another trending story", "https://example.com/second")
        second.native_categories = {"trending"}
        second.native_rank = 1

        merged = dedupe([ranked, publisher, second])
        topic = Category(id="trending", name="Trending", sources=[_source(category="trending")])
        ordered = rank_items(merged, topic, NOW, {})

        assert ordered[0].source_id == "htmx"
        assert ordered[0].native_rank == 0
        assert ordered[1].native_rank == 1


class TestProbeCliContract:
    def test_google_degraded_json_ok_matches_zero_exit(self, monkeypatch, capsys):
        from scripts import probe_sources

        cfg = load_config(Path(__file__).resolve().parent.parent)
        source = next(source for source in cfg.rss if source.id == "google-36kr")
        (item,) = rss.parse_document((FIXTURES / "google-36kr.xml").read_bytes(), source)
        health = rss.source_health(source, [item], item.published_at + timedelta(hours=1))
        row = probe_sources._health_row(health)
        assert "elapsed_s" not in row
        captured = {}

        def fake_probe(_cfg, specs, **_kwargs):
            captured["ids"] = [spec.id for spec in specs]
            return [row]

        monkeypatch.setattr(probe_sources, "probe_specs", fake_probe)

        exit_code = probe_sources.main(
            ["--root", str(Path(__file__).resolve().parent.parent), "--json", "--ids", "google-36kr"]
        )
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert payload["sources"][0]["status"] == "link_resolution_degraded"
        assert payload["sources"][0]["ok"] is True
        assert payload["sources"][0]["fresh"] is False
        assert captured["ids"] == ["google-36kr"]

    def test_trending_accepts_only_native_rows(self):
        cfg = load_config(Path(__file__).resolve().parent.parent)
        topic = next(category for category in cfg.categories if category.id == "trending")
        payload = __import__("json").loads((FIXTURES / "hn-front-page.json").read_text())
        ordinary = hn._to_item(payload["hits"][0], 1.0)
        assert ordinary is not None

        assert topic.keywords == []
        assert topic.hn_queries == []
        assert assign_categories([ordinary], [topic])["Trending"] == []
