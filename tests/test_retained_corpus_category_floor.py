"""B8 bug 1: a retained row with no category at all.

Measured on the committed capture before the fix: 50 of 262 rows
(tests/fixtures/m2-retained-public.json) carried `category_ids: []`, led by the
shared-pool general-news desks (fox-news 14, cbs-news 8, cnn-news 7,
yahoo-news 3) and hackernews (9). Those routes declare no category by design
(`sources.yaml` `rss:`, where keywords decide), so a headline carrying none of
the configured terms reached the corpus unlabelled: invisible to every section
filter, and rendered with no category on the card.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from curator.config import Category, load_config
from curator.models import Item
from curator.retained_corpus import retain

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
CATEGORIES = [Category(id="us-news", name="US News", keywords=["Congress"]),
              Category(id="trending", name="Trending")]
FLOORS = {"cnn-news": "us-news"}


def floor_for(source_id: str) -> str:
    return FLOORS.get(source_id, "trending")


def item(title: str, url: str, *, source_id: str = "cnn-news", language: str = "en") -> Item:
    return Item(title=title, description="publisher source", url=url, canonical_url=url,
                source_id=source_id, source_name=source_id, published_at=NOW, language=language)


def test_a_story_that_matches_nothing_keeps_its_routes_floor():
    rows = retain([item("Regional weather update", "https://example.com/a")],
                  categories=CATEGORIES, observed_at=NOW, fallback_category_for=floor_for)
    assert rows[0].category_ids == frozenset({"us-news"})


def test_the_floor_never_overrides_or_joins_a_real_match():
    rows = retain([item("Congress passes bill", "https://example.com/b", source_id="hnfront")],
                  categories=CATEGORIES, observed_at=NOW, fallback_category_for=floor_for)
    assert rows[0].category_ids == frozenset({"us-news"})  # the keyword hit, not "trending"


def test_a_route_with_no_floor_of_its_own_takes_the_global_default():
    rows = retain([item("Regional weather update", "https://example.com/c", source_id="hnfront")],
                  categories=CATEGORIES, observed_at=NOW, fallback_category_for=floor_for)
    assert rows[0].category_ids == frozenset({"trending"})


def test_an_empty_floor_leaves_the_old_behaviour_untouched():
    rows = retain([item("Regional weather update", "https://example.com/d")],
                  categories=CATEGORIES, observed_at=NOW, fallback_category_for=lambda _: "")
    assert rows[0].category_ids == frozenset()
    without = retain([item("Regional weather update", "https://example.com/d")],
                     categories=CATEGORIES, observed_at=NOW)
    assert without[0].category_ids == frozenset()


def test_the_shipped_config_gives_every_configured_route_a_floor():
    config = load_config(ROOT)
    assert config.retained_corpus_fallback_category_id == "trending"
    for source in config.all_feeds:
        floor = config.fallback_category_for(source.id)
        assert floor, source.id
        assert floor in {category.id for category in config.categories}, source.id


def test_replaying_the_real_capture_leaves_no_uncategorised_row():
    # The red measurement, and the green one, over real captured public news.
    capture = json.loads((ROOT / "tests/fixtures/m2-retained-public.json").read_text())
    config = load_config(ROOT)
    rows = [item(row["title"], row["canonical_url"], source_id=row["source_id"],
                 language=row["language"]) for row in capture["rows"]]
    for built, raw in zip(rows, capture["rows"]):
        built.description = raw["summary"]

    before = retain(rows, categories=config.categories, observed_at=NOW)
    assert sum(1 for row in before if not row.category_ids) > 0

    after = retain(rows, categories=config.categories, observed_at=NOW,
                   fallback_category_for=config.fallback_category_for)
    assert [row.story_id for row in after] == [row.story_id for row in before]
    assert not [row.story_id for row in after if not row.category_ids]
