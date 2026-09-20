"""The coverage writer: the deduper's evidence, turned into ingest rows.

Without this the hot pool is always empty, because the corpus keeps one row per
canonical story and the list of outlets that carried it was dropped on the floor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from curator.config import Category
from curator.models import Item
from curator.retained_corpus import coverage_ingest_rows, retain

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
CATEGORIES = (Category(name="World", id="world", keywords=["rules"]),)


# Coverage comes from the deduper's EXACT pass (the same canonical URL carried
# by more than one route), never from its fuzzy title pass. That boundary is
# deliberate and load-bearing: curator/dedup.py refuses to let a guessed merge
# inflate a public numeric claim, and "N outlets are carrying this" is exactly
# such a claim. test_a_fuzzy_merge_never_counts_as_coverage pins it.
def item(source_id, *, title, url, hours=1, aggregator=False, echo_eligible=True, language="en"):
    return Item(title=title, url=url, canonical_url=url, source_id=source_id,
                source_name=source_id.title(), published_at=NOW - timedelta(hours=hours),
                language=language, description="Seven new rules.", is_aggregator=aggregator,
                echo_eligible=echo_eligible)


def rows_for(items):
    retained = retain(items, categories=CATEGORIES, observed_at=NOW)
    independent = {source for source in ("reuters", "cnn", "cnbeta")}
    return retained, coverage_ingest_rows(retained, independent_source_ids=independent)


def test_one_coverage_row_per_distinct_publisher():
    # Three outlets on one event. The deduper merges them into one canonical
    # story and records each outlet as a coverage mention.
    retained, rows = rows_for([
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/1"),
        item("cnn", title="Ministry issues seven new rules", url="https://a.test/1"),
        item("cnbeta", title="Ministry issues seven new rules", url="https://a.test/1"),
    ])
    assert len(retained) == 1
    assert {row["publisher_id"] for row in rows} == {"reuters", "cnn", "cnbeta"}
    assert all(row["is_independent"] for row in rows)
    assert all(row["story_id"] == retained[0].story_id for row in rows)


def test_a_same_publisher_echo_is_one_row_not_two():
    retained, rows = rows_for([
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/1"),
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/2", hours=2),
    ])
    publishers = [row["publisher_id"] for row in rows]
    assert publishers == ["reuters"], publishers


def test_an_aggregator_route_is_recorded_but_never_counts_as_independent():
    retained, rows = rows_for([
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/1"),
        item("buzzing", title="Ministry issues seven new rules", url="https://a.test/1",
             aggregator=True),
        item("google-36kr", title="Ministry issues seven new rules", url="https://a.test/1",
             echo_eligible=False),
    ])
    by_publisher = {row["publisher_id"]: row for row in rows}
    assert set(by_publisher) == {"reuters", "buzzing", "google-36kr"}
    assert by_publisher["reuters"]["is_independent"] is True
    # Recorded, so the evidence is not lost, but not corroboration.
    assert by_publisher["buzzing"]["is_independent"] is False
    assert by_publisher["google-36kr"]["is_independent"] is False


def test_first_seen_is_the_earliest_sighting():
    retained, rows = rows_for([
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/1", hours=1),
        item("cnn", title="Ministry issues seven new rules", url="https://a.test/1", hours=5),
    ])
    seen = {row["publisher_id"]: row["first_seen_at"] for row in rows}
    assert seen["cnn"] < seen["reuters"], "the earlier sighting must be the recorded one"


def test_a_fuzzy_merge_never_counts_as_coverage():
    """Two DIFFERENT URLs merged on title similarity are a judgement, not a
    citation. curator/dedup.py already refuses to let a guess inflate the public
    "N sources" claim, and hot is the same claim, so it inherits that rule."""
    retained, rows = rows_for([
        item("reuters", title="Ministry issues seven new rules", url="https://a.test/1"),
        item("cnn", title="Ministry issues seven new rules", url="https://b.test/1"),
    ])
    assert len(retained) == 1, "the fuzzy pass still merges them into one story"
    assert [row["publisher_id"] for row in rows] == ["reuters"]


def test_an_unknown_route_is_not_independent():
    """Conservative by construction: a route we cannot find in config is not
    corroboration. Guessing the other way is how a firehose becomes "hot"."""
    retained = retain([item("mystery", title="Ministry issues seven new rules",
                            url="https://a.test/1")], categories=CATEGORIES, observed_at=NOW)
    rows = coverage_ingest_rows(retained, independent_source_ids=set())
    assert rows and rows[0]["is_independent"] is False


def test_every_row_carries_exactly_the_fields_the_rpc_accepts():
    _, rows = rows_for([item("reuters", title="Ministry issues seven new rules",
                             url="https://a.test/1")])
    assert rows and all(set(row) == {"story_id", "publisher_id", "is_independent", "first_seen_at"}
                        for row in rows)
