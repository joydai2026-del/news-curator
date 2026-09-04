"""Categories: keywords AND curated feeds, and the two ways to belong to one.

The claim under test is narrow and load-bearing: a feed listed under a category
is an editorial judgement that the publication is single-subject, so its items
join that section WITHOUT a keyword hit, which is the only way a headline like
"Vogtle 4 enters commercial operation" ever reaches the energy section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from curator.config import Category, ConfigError, load_config, load_topics, slugify
from curator.filter import assign_categories, is_native, topic_match
from curator.rank import keyword_score
from tests.conftest import make_item


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


SOURCES = "settings:\n  max_age_hours: 24\nrss: []\n"


class TestCategoryParsing:
    def test_categories_carry_keywords_and_sources(self, tmp_path):
        write(
            tmp_path,
            "topics.yaml",
            "categories:\n"
            "  - id: energy\n"
            "    name: Energy\n"
            "    keywords:\n      - nuclear power\n"
            "    sources:\n"
            "      - {id: wnn, name: World Nuclear News, url: 'https://w.example/rss', weight: 1.2}\n",
        )
        write(tmp_path, "sources.yaml", SOURCES)
        cfg = load_config(tmp_path)

        (category,) = cfg.categories
        assert category.id == "energy"
        assert category.keywords == ["nuclear power"]
        assert len(category.sources) == 1
        assert category.sources[0].weight == 1.2
        # The feed is tagged with its category, which is what the fetcher uses
        # to mark the items it produces.
        assert category.sources[0].category == "energy"

    def test_category_feeds_join_the_fetch_list(self, tmp_path):
        write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - name: Space\n    keywords: [rocket]\n"
            "    sources:\n      - {id: sn, url: 'https://s.example/rss'}\n",
        )
        write(tmp_path, "sources.yaml", "rss:\n  - {id: shared, url: 'https://g.example/rss'}\n")
        cfg = load_config(tmp_path)

        assert [s.id for s in cfg.rss] == ["shared"]  # the shared pool stays shared
        assert sorted(s.id for s in cfg.all_feeds) == ["shared", "sn"]

    def test_id_is_derived_from_the_name_when_omitted(self, tmp_path):
        write(tmp_path, "topics.yaml", "categories:\n  - name: Quantum computing\n    keywords: [qubit]\n")
        write(tmp_path, "sources.yaml", SOURCES)
        assert load_config(tmp_path).categories[0].id == "quantum-computing"

    def test_v1_topics_key_still_loads(self, tmp_path):
        # A fork written against v1 must keep working. It simply gets categories
        # with no native feeds.
        path = write(tmp_path, "topics.yaml", "topics:\n  - name: AI\n    keywords:\n      - AI\n")
        (category,) = load_topics(path)
        assert category.name == "AI" and category.sources == []

    def test_a_category_with_only_sources_is_allowed(self, tmp_path):
        # Legitimate: a section defined purely by which publications feed it.
        path = write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - name: Space\n"
            "    sources:\n      - {id: sn, url: 'https://s.example/rss'}\n",
        )
        assert load_topics(path)[0].keywords == []

    def test_a_category_with_neither_is_rejected(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "categories:\n  - name: Empty\n")
        with pytest.raises(ConfigError, match="no keywords and no sources"):
            load_topics(path)

    def test_scalar_keywords_is_still_a_loud_error(self, tmp_path):
        path = write(tmp_path, "topics.yaml", "categories:\n  - name: X\n    keywords: AI\n")
        with pytest.raises(ConfigError, match="must be a LIST"):
            load_topics(path)

    def test_bad_category_feed_url_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - name: X\n    keywords: [a]\n"
            "    sources:\n      - {id: bad, url: 'javascript:alert(1)'}\n",
        )
        write(tmp_path, "sources.yaml", SOURCES)
        with pytest.raises(ConfigError, match="absolute http"):
            load_config(tmp_path)

    def test_category_feed_rejects_an_unsupported_type_at_load_time(self, tmp_path):
        write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - name: X\n    keywords: [a]\n"
            "    sources:\n      - {type: rrs, id: typo, url: 'https://example.com/feed'}\n",
        )

        with pytest.raises(ConfigError, match="type must be one of"):
            load_topics(tmp_path / "topics.yaml")

    def test_duplicate_category_ids_are_rejected(self, tmp_path):
        path = write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - {name: A B, keywords: [x]}\n  - {name: 'A  B', keywords: [y]}\n",
        )
        with pytest.raises(ConfigError, match="duplicate category ids"):
            load_topics(path)

    def test_a_feed_id_cannot_be_reused_across_files(self, tmp_path):
        # Not cosmetic: `platform` defaults to the id and drives the "N sources"
        # echo badge, so two feeds sharing an id would claim to corroborate
        # each other.
        write(
            tmp_path,
            "topics.yaml",
            "categories:\n  - name: X\n    keywords: [a]\n"
            "    sources:\n      - {id: dup, url: 'https://a.example/rss'}\n",
        )
        write(tmp_path, "sources.yaml", "rss:\n  - {id: dup, url: 'https://b.example/rss'}\n")
        with pytest.raises(ConfigError, match="duplicate feed id 'dup'"):
            load_config(tmp_path)

    def test_hn_queries_fall_back_to_keywords(self, tmp_path):
        path = write(
            tmp_path,
            "topics.yaml",
            "categories:\n"
            "  - name: A\n    keywords: [alpha, beta]\n"
            "  - name: B\n    keywords: [gamma]\n    hn_queries: [g]\n",
        )
        a, b = load_topics(path)
        assert a.search_terms == ["alpha", "beta"]
        assert b.search_terms == ["g"]

    def test_slugify_is_stable(self):
        assert slugify("Energy and nuclear") == "energy-and-nuclear"
        assert slugify("AI / ML!!") == "ai-ml"


class TestNativeMembership:
    def _energy(self):
        return Category(
            name="Energy",
            id="energy",
            keywords=["nuclear power"],
            exclude=["fossil"],
        )

    def test_a_native_feed_belongs_without_a_keyword(self):
        item = make_item("Vogtle 4 enters commercial operation")
        item.native_categories = {"energy"}
        # An empty LIST means "belongs, on the strength of its source".
        assert topic_match(item, self._energy()) == []

    def test_a_non_native_item_without_a_keyword_does_not_belong(self):
        item = make_item("Vogtle 4 enters commercial operation")
        assert topic_match(item, self._energy()) is None

    def test_exclude_still_vetoes_a_native_item(self):
        # A native feed is a strong claim, not an unconditional one.
        item = make_item("A fossil plant closes")
        item.native_categories = {"energy"}
        assert topic_match(item, self._energy()) is None

    def test_a_native_item_that_also_matches_reports_its_keywords(self):
        item = make_item("Nuclear power gets a boost")
        item.native_categories = {"energy"}
        assert topic_match(item, self._energy()) == ["nuclear power"]

    def test_native_membership_does_not_leak_between_categories(self):
        space = Category(name="Space", id="space", keywords=["rocket"])
        item = make_item("Vogtle 4 enters commercial operation")
        item.native_categories = {"energy"}
        assert topic_match(item, space) is None

    def test_assign_carries_native_tags_onto_each_copy(self):
        energy = self._energy()
        item = make_item("Vogtle 4 enters commercial operation")
        item.native_categories = {"energy"}
        (row,) = assign_categories([item], [energy])["Energy"]
        assert is_native(row, energy)
        assert row.matched_keywords == []


class TestNativeRanking:
    def test_a_native_item_with_no_keyword_scores_above_zero(self):
        # Zero would rank a curated nuclear story below a passing mention of
        # "power grid" in a story about something else.
        energy = Category(name="Energy", id="energy", keywords=["nuclear power"])
        item = make_item("Vogtle 4 enters commercial operation")
        item.native_categories = {"energy"}
        score = keyword_score(item, energy, lead_chars=40, lead_bonus=0.25, native_score=0.4)
        assert score == 0.4

    def test_a_real_keyword_hit_still_outranks_a_bare_native_item(self):
        energy = Category(name="Energy", id="energy", keywords=["nuclear power"])
        native = make_item("Vogtle 4 enters commercial operation")
        native.native_categories = {"energy"}
        matched = make_item("Nuclear power output climbs")
        matched.matched_keywords = ["nuclear power"]
        args = dict(lead_chars=40, lead_bonus=0.25, native_score=0.4)
        assert keyword_score(matched, energy, **args) > keyword_score(native, energy, **args)

    def test_the_default_beats_even_a_keyword_hit_with_no_lead_bonus(self):
        # The case the first version of this test missed. A single keyword hit
        # LATE in a headline scores exactly 0.5 with no lead bonus, so a native
        # score of 0.5 would tie and let source weight decide the order.
        energy = Category(name="Energy", id="energy", keywords=["nuclear power"])
        native = make_item("Vogtle 4 enters commercial operation")
        native.native_categories = {"energy"}
        late = make_item("A very long headline about something before nuclear power")
        late.matched_keywords = ["nuclear power"]
        args = dict(lead_chars=10, lead_bonus=0.25)  # no lead bonus for either
        assert keyword_score(late, energy, **args) == 0.5
        assert keyword_score(native, energy, **args) == 0.4
        assert keyword_score(late, energy, **args) > keyword_score(native, energy, **args)

    def test_a_non_native_item_with_no_keyword_scores_zero(self):
        energy = Category(name="Energy", id="energy", keywords=["nuclear power"])
        assert keyword_score(make_item("Unrelated"), energy, lead_chars=40, lead_bonus=0.25) == 0.0


class TestTheShippedConfig:
    """The original six plus P1 general and Trending categories."""

    def _cfg(self):
        return load_config(Path(__file__).resolve().parent.parent)

    def test_original_six_ids_are_unchanged(self):
        cfg = self._cfg()
        original = {"ai", "crypto", "quantum", "energy", "biotech", "space"}
        assert original.issubset({category.id for category in cfg.categories})
        for category in cfg.categories:
            if category.id not in original:
                continue
            assert category.keywords, f"{category.name} has no keywords"
            assert category.sources, f"{category.name} has no curated feeds"

    def test_p1_categories_are_first_class_topics_entries(self):
        cfg = self._cfg()
        assert {"world", "us-news", "business", "trending"}.issubset(
            {category.id for category in cfg.categories}
        )

    def test_required_p1_sources_and_google_limit_are_configured(self):
        cfg = self._cfg()
        by_id = {source.id: source for source in cfg.all_feeds}
        required = {
            "cnn-news", "fox-news", "bbc-world", "guardian-world", "cnbc",
            "cbs-news", "yahoo-news", "cnbeta", "solidot", "rfi-zh", "cna-zh",
            "udn-zh", "dw-zh", "buzzing", "google-36kr", "google-zaobao",
        }
        assert required.issubset(by_id)
        assert by_id["cnn-news"].type == "news_sitemap"
        assert by_id["fox-news"].type == "news_sitemap"
        assert by_id["buzzing"].category == "trending"
        assert by_id["google-36kr"].echo_eligible is False
        assert by_id["google-zaobao"].echo_eligible is False

    def test_general_source_native_mapping_matches_the_category_contract(self):
        cfg = self._cfg()
        by_id = {source.id: source for source in cfg.all_feeds}
        assert by_id["bbc-world"].category == "world"
        assert by_id["guardian-world"].category == "world"
        assert by_id["cnbc"].category == "business"
        assert by_id["cbs-news"].category == ""
        assert by_id["yahoo-news"].category == ""
        shared_ids = {source.id for source in cfg.rss}
        assert {"cnn-news", "fox-news", "cbs-news", "yahoo-news"}.issubset(shared_ids)

    def test_zaobao_google_query_uses_the_live_working_site_and_locale(self):
        from urllib.parse import parse_qs, urlsplit

        cfg = self._cfg()
        source = next(source for source in cfg.all_feeds if source.id == "google-zaobao")
        query = parse_qs(urlsplit(source.url).query)
        assert query["q"] == ["site:zaobao.com.sg when:1d"]
        assert query["gl"] == ["SG"]
        assert query["ceid"] == ["SG:zh-Hans"]

    def test_legacy_cnn_rss_is_absent(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / "sources.yaml").read_text() + (root / "topics.yaml").read_text()
        assert "rss.cnn.com" not in text

    def test_techcrunch_is_present_for_ai(self):
        # An explicit product requirement, so it gets an explicit test rather
        # than being left to whoever edits the YAML next.
        cfg = self._cfg()
        (ai,) = [c for c in cfg.categories if c.id == "ai"]
        assert any("techcrunch.com" in s.url for s in ai.sources)

    def test_every_feed_id_is_unique_and_every_url_is_https(self):
        for source in self._cfg().all_feeds:
            assert source.url.startswith("https://"), source.url

    def test_every_keyword_category_declares_hn_queries(self):
        # Without them a keyword category falls back to all its terms, and many
        # categories of terms blow the Hacker News request cap. Native-only
        # Trending intentionally has no search query beyond HN front_page.
        for category in self._cfg().categories:
            if category.keywords or category.aliases:
                assert category.hn_queries, f"{category.name} has no hn_queries"


class TestDegradedFeedReporting:
    """A source that dies quietly is the failure this reporting exists to catch."""

    def test_a_feed_yielding_nothing_usable_is_reported_as_degraded(self, monkeypatch):
        # The real case: Fierce Biotech returns 200, parses, carries 25 entries,
        # and every one is dropped for an unparseable <pubDate>. Without this the
        # tier reports "ok" and it looks identical to a slow news day.
        from curator import config as cfg_mod
        from curator.fetchers import rss

        cfg = cfg_mod.Config(
            categories=[], rss=[cfg_mod.RssSource(id="dead", name="Dead", url="https://d.example/rss")],
            settings={}, ranking={}, dedup={}, hackernews={}, reddit={},
        )
        monkeypatch.setattr(rss, "_fetch_one", lambda *a, **k: [])
        result = rss.fetch(cfg)
        assert result.ok is False
        assert "nothing usable" in result.note

    def test_a_healthy_feed_is_not_reported_as_degraded(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from curator import config as cfg_mod
        from curator.fetchers import rss
        from tests.conftest import make_item

        cfg = cfg_mod.Config(
            categories=[], rss=[cfg_mod.RssSource(id="live", name="Live", url="https://l.example/rss")],
            settings={}, ranking={}, dedup={}, hackernews={}, reddit={},
        )
        item = make_item("A story")
        item.published_at = datetime.now(timezone.utc) - timedelta(hours=1)
        monkeypatch.setattr(rss, "_fetch_one", lambda *a, **k: [item])
        result = rss.fetch(cfg)
        assert result.ok is True and result.note == ""


class TestFuzzyDedupCannotEmptyACategory:
    """A fuzzy merge must never delete a story out of a section."""

    def test_a_higher_weight_general_copy_does_not_delete_the_native_one(self):
        # The regression that matters, and the exact headline the whole design
        # is justified by. A wire story is carried by the curated energy feed
        # and by a higher-weight general feed at a different URL. The general
        # copy wins the merge, and before the fix the energy tag went with the
        # loser, so the story vanished from Energy entirely.
        from curator.config import Category
        from curator.dedup import dedupe
        from curator.filter import assign_categories

        native = make_item("Vogtle 4 enters commercial operation", "https://wnn.example/1", weight=1.0)
        native.native_categories = {"energy"}
        general = make_item("Vogtle 4 enters commercial operation", "https://reuters.example/2", weight=1.5)

        (survivor,) = dedupe([native, general], threshold=0.9)
        assert survivor.native_categories == {"energy"}

        energy = Category(name="Energy", id="energy", keywords=["nuclear power"])
        assert len(assign_categories([survivor], [energy])["Energy"]) == 1

    def test_a_fuzzy_merge_unions_categories_from_both_rows(self):
        from curator.dedup import dedupe

        a = make_item("Reactor project clears final hurdle", "https://a.example/1")
        a.native_categories = {"energy"}
        b = make_item("Reactor project clears final hurdles", "https://b.example/2")
        b.native_categories = {"space"}
        (survivor,) = dedupe([a, b], threshold=0.85)
        assert survivor.native_categories == {"energy", "space"}

    def test_a_fuzzy_merge_still_does_not_inflate_the_echo_badge(self):
        # The badge makes a public numeric claim, so it stays gated on certainty
        # even though category membership no longer is.
        from curator.dedup import dedupe

        a = make_item("Reactor project clears final hurdle", "https://a.example/1", platform="p1")
        b = make_item("Reactor project clears final hurdles", "https://b.example/2", platform="p2")
        (survivor,) = dedupe([a, b], threshold=0.85)
        assert survivor.echo_platforms == {"p1"}

    def test_exact_url_merge_still_unions_categories(self):
        from curator.dedup import dedupe

        a = make_item("A story", "https://same.example/x")
        a.native_categories = {"energy"}
        b = make_item("A story", "https://same.example/x")
        b.native_categories = {"space"}
        (survivor,) = dedupe([a, b], threshold=0.9)
        assert survivor.native_categories == {"energy", "space"}
