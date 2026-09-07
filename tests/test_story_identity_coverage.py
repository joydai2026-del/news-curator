from __future__ import annotations

from datetime import datetime, timezone

from curator.dedup import dedupe
from curator.identity import story_id_for_item
from curator.models import CoverageMention
from curator.newsletter.lane import build_mention
from curator.personalization.ranking import story_key
from tests.conftest import make_item


def test_translation_and_interest_use_one_url_stable_story_identity() -> None:
    first = make_item("Original headline", "https://www.example.com/story?utm_source=mail")
    changed = make_item("Rewritten headline", "https://example.com/story")

    assert story_id_for_item(first) == story_id_for_item(changed)
    assert story_key(first) == story_id_for_item(first)
    assert story_key(changed) == story_id_for_item(first)


def test_story_identity_normalizes_url_before_hashing() -> None:
    tracked = make_item("One", "HTTPS://WWW.Example.com/story/?utm_source=mail#section")
    clean = make_item("Two", "https://example.com/story")
    tracked.canonical_url = tracked.url
    clean.canonical_url = clean.url

    assert story_id_for_item(tracked) == story_id_for_item(clean)


def test_dedup_preserves_named_coverage_and_counts_distinct_sources() -> None:
    publisher = make_item("AI systems ship", "https://example.com/story")
    newsletter = make_item("AI systems ship", "https://example.com/story")
    newsletter.source_id = "newsletter:tldr"
    newsletter.source_name = "TLDR"
    newsletter.platform = "newsletter:tldr"
    newsletter.is_newsletter = True
    newsletter.newsletter_sender = "TLDR"
    newsletter.coverage_mentions = [
        CoverageMention.from_item(newsletter, mentioned_at=datetime(2026, 9, 7, tzinfo=timezone.utc))
    ]

    survivor = dedupe([publisher, newsletter])[0]

    assert {(m.source_kind, m.source_id, m.source_name) for m in survivor.coverage_mentions} == {
        ("outlet", "example", "Example"),
        ("newsletter", "newsletter:tldr", "TLDR"),
    }
    assert survivor.distinct_coverage_source_count == 2


def test_fuzzy_dedup_does_not_copy_named_coverage_from_a_different_url() -> None:
    publisher = make_item(
        "AI systems ship today", "https://publisher.example/story",
        source_id="publisher", source_name="Publisher", weight=2.0,
    )
    other = make_item(
        "AI systems ship today!", "https://other.example/report",
        source_id="other", source_name="Other Outlet",
    )

    survivor = dedupe([publisher, other])[0]

    assert [(m.source_id, m.source_name) for m in survivor.coverage_mentions] == [
        ("publisher", "Publisher")
    ]
    assert survivor.distinct_coverage_source_count == 1


def test_repeated_mentions_from_one_newsletter_are_preserved_but_count_once() -> None:
    item = make_item("AI systems ship", "https://example.com/story")
    item.source_id = "newsletter:tldr"
    item.source_name = "TLDR"
    item.is_newsletter = True
    item.newsletter_sender = "TLDR"
    item.coverage_mentions = [
        CoverageMention.from_item(item, mentioned_at=datetime(2026, 9, 6, tzinfo=timezone.utc)),
        CoverageMention.from_item(item, mentioned_at=datetime(2026, 9, 7, tzinfo=timezone.utc)),
    ]

    assert len(item.coverage_mentions) == 2
    assert item.distinct_coverage_source_count == 1


def test_newsletter_mention_identity_round_trips_through_the_shared_model() -> None:
    item = make_item("AI systems ship", "https://www.example.com/story?utm_source=mail")
    item.source_id = "newsletter:tldr"
    item.source_name = "TLDR"
    item.is_newsletter = True
    item.newsletter_sender = "TLDR"
    item.published_at = datetime(2026, 9, 7, tzinfo=timezone.utc)
    record = {
        "source_id": item.source_id, "newsletter_sender": item.newsletter_sender,
        "published_at": item.published_at, "canonical_url": "https://example.com/story",
        "url": item.url, "title": item.title,
    }

    assert build_mention(record)["mention_id"] == CoverageMention.from_item(item).mention_id
