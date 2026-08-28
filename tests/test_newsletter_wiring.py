"""The seam between the fetch job's artifact and the build.

Everything here is the orchestration contract: the artifact round-trips, the
Newsletters tab exists exactly when the lane is lit, the lane's cap does not
leak into category caps, and reconstruction stamps the privacy-critical fields
whatever the artifact claims.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.config import Category, Config
from curator.models import Item, TierResult
from curator.newsletter.__main__ import serialize
from curator.newsletter.lane import LaneResult
from curator.pipeline import NEWSLETTER_CATEGORY_NAME, build, load_newsletter_artifact

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def config(**newsletter) -> Config:
    return Config(
        categories=[Category(name="AI", keywords=["AI"])],
        rss=[],
        settings={"max_items_per_topic": 2},
        ranking={},
        dedup={},
        hackernews={},
        reddit={},
        newsletter=newsletter,
    )


def artifact(tmp_path: Path, **overrides) -> Path:
    payload = {
        "version": 1,
        "ok": True,
        "dark": False,
        "reason": "ok",
        "note": "newsletter mailbox read",
        "unmatched_messages": 0,
        "watermark": NOW.isoformat(),
        "hashes": ["a" * 64],
        "status": {"tldr": {"name": "TLDR", "seen": 2, "extracted": 3,
                            "dropped_links": 1, "published": 3, "state": "ok"}},
        "items": [{
            "title": "A story about AI agents",
            "url": "https://example.com/story",
            "canonical_url": "https://example.com/story",
            "source_id": "newsletter:tldr",
            "source_name": "TLDR",
            "platform": "newsletter:tldr",
            "published_at": (NOW - timedelta(hours=2)).isoformat(),
            "description": "The newsletter's own blurb.",
            "newsletter_sender": "TLDR",
        }],
    }
    payload.update(overrides)
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestArtifactLoading:
    def test_items_carry_the_privacy_critical_fields(self, tmp_path):
        items, tier, meta = load_newsletter_artifact(artifact(tmp_path))
        (item,) = items
        assert item.is_newsletter is True
        assert item.image_url == ""
        assert item.is_aggregator is True
        assert "newsletters" in item.native_categories
        assert tier.tier == "newsletters"
        assert meta["dark"] is False and meta["watermark"] == NOW.isoformat()

    def test_an_image_url_in_the_artifact_is_refused(self, tmp_path):
        path = artifact(tmp_path)
        raw = json.loads(path.read_text())
        raw["items"][0]["image_url"] = "https://tracker.example/pixel.png"
        path.write_text(json.dumps(raw))
        items, _, _ = load_newsletter_artifact(path)
        assert items[0].image_url == ""

    def test_problems_reach_the_health_note_but_clean_hit_rates_do_not(self, tmp_path):
        # A clean run must not read as degraded, so its note is empty.
        _, clean, _ = load_newsletter_artifact(artifact(tmp_path))
        assert clean.note == "" and clean.degraded is False
        # Problems (a pending adapter, unmatched senders) do reach the page.
        _, tier, _ = load_newsletter_artifact(artifact(
            tmp_path, unmatched_messages=4,
            status={"tldr": {"name": "TLDR", "seen": 2, "extracted": 0,
                             "dropped_links": 0, "published": 0, "state": "pending"}},
        ))
        assert "pending adapters: tldr" in tier.note
        assert "4 messages from senders without an adapter" in tier.note
        assert tier.degraded is True

    def test_a_dark_artifact_keeps_the_lane_dark(self, tmp_path):
        items, tier, meta = load_newsletter_artifact(
            artifact(tmp_path, dark=True, ok=False, items=[], note="Gmail refresh token revoked")
        )
        assert items == [] and tier.ok is False and meta["dark"] is True
        assert "revoked" in tier.note

    def test_an_undated_item_is_dropped_not_crashed(self, tmp_path):
        path = artifact(tmp_path)
        raw = json.loads(path.read_text())
        raw["items"][0]["published_at"] = "not a date"
        path.write_text(json.dumps(raw))
        items, _, _ = load_newsletter_artifact(path)
        assert items == []


class TestBuildWithTheLane:
    def newsletter_item(self, title="Quiet newsletter story", hours=1) -> Item:
        slug = title.replace(" ", "-")
        return Item(
            title=title,
            url=f"https://example.org/{slug}",
            canonical_url=f"https://example.org/{slug}",
            source_id="newsletter:tldr",
            source_name="TLDR",
            published_at=NOW - timedelta(hours=hours),
            is_newsletter=True,
            is_aggregator=True,
            newsletter_sender="TLDR",
            native_categories={"newsletters"},
        )

    def test_tab_present_when_lane_lit_even_if_quiet(self):
        ranked = build(config(), [TierResult(tier="newsletters", items=[])], NOW, newsletter_on=True)
        assert NEWSLETTER_CATEGORY_NAME in ranked

    def test_no_tab_when_lane_dark(self):
        ranked = build(config(), [], NOW, newsletter_on=False)
        assert NEWSLETTER_CATEGORY_NAME not in ranked

    def test_lane_cap_is_independent_of_category_caps(self):
        items = [self.newsletter_item(title=f"story number {n}") for n in range(5)]
        ranked = build(
            config(max_items=3),
            [TierResult(tier="newsletters", items=items)],
            NOW,
            newsletter_on=True,
        )
        assert len(ranked[NEWSLETTER_CATEGORY_NAME]) == 3  # the lane's own cap
        # and the category cap (2) was untouched by the lane's (3)

    def test_keyword_match_cross_tags_one_story(self):
        item = self.newsletter_item(title="AI eats the newsroom")
        ranked = build(
            config(),
            [TierResult(tier="newsletters", items=[item])],
            NOW,
            newsletter_on=True,
        )
        assert any(i.canonical_url == item.canonical_url for i in ranked["AI"])
        assert any(i.canonical_url == item.canonical_url for i in ranked[NEWSLETTER_CATEGORY_NAME])


class TestNewsletterCardMarker:
    def test_newsletter_cards_carry_the_marker_and_no_image(self):
        from curator.render import render_html

        item = Item(
            title="Marked story",
            url="https://example.com/marked",
            canonical_url="https://example.com/marked",
            source_id="newsletter:tldr",
            source_name="TLDR",
            published_at=NOW - timedelta(hours=1),
            is_newsletter=True,
            newsletter_sender="TLDR",
            image_url="",
        )
        html = render_html({NEWSLETTER_CATEGORY_NAME: [item]}, [], NOW)
        card = html[html.index('<article class="card"'):]
        card = card[: card.index(">") + 1]
        assert 'data-newsletter=""' in card
        assert "data-image" not in card

    def test_ordinary_cards_do_not_carry_the_marker(self):
        from curator.render import render_html

        item = Item(
            title="Ordinary story",
            url="https://example.com/plain",
            canonical_url="https://example.com/plain",
            source_id="verge",
            source_name="The Verge",
            published_at=NOW - timedelta(hours=1),
        )
        html = render_html({"AI": [item]}, [], NOW)
        assert "data-newsletter" not in html


class TestSerializeRoundTrip:
    def test_lane_result_survives_the_artifact_boundary(self, tmp_path):
        record = {
            "title": "Round trip",
            "url": "https://example.com/rt",
            "canonical_url": "https://example.com/rt",
            "source_id": "newsletter:tldr",
            "source_name": "TLDR",
            "platform": "newsletter:tldr",
            "published_at": NOW,
            "description": "blurb",
            "is_newsletter": True,
            "newsletter_sender": "TLDR",
            "image_url": "",
        }
        result = LaneResult(items=[record], ok=True, dark=False, watermark=NOW, hashes=["b" * 64])
        path = tmp_path / "artifact.json"
        path.write_text(json.dumps(serialize(result)), encoding="utf-8")
        items, tier, meta = load_newsletter_artifact(path)
        assert items[0].title == "Round trip"
        assert items[0].description == "blurb"
        assert meta["hashes"] == ["b" * 64]
