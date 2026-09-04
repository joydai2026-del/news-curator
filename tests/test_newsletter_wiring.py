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
        assert item.language == "en"

    def test_an_image_url_in_the_artifact_is_refused(self, tmp_path):
        path = artifact(tmp_path)
        raw = json.loads(path.read_text())
        raw["items"][0]["image_url"] = "https://tracker.example/pixel.png"
        path.write_text(json.dumps(raw))
        items, _, _ = load_newsletter_artifact(path)
        assert items[0].image_url == ""

    @pytest.mark.parametrize(
        "unsafe_url",
        [
            "https://publisher.example/story?email=reader%40example.invalid",
            "https://link.mail.beehiiv.com/ss/c/AbCdEf0123456789XyZq",
        ],
    )
    def test_artifact_urls_cross_the_newsletter_privacy_gate_before_item_creation(
        self, tmp_path, unsafe_url
    ):
        path = artifact(tmp_path)
        raw = json.loads(path.read_text())
        raw["items"][0]["url"] = unsafe_url
        raw["items"][0]["canonical_url"] = unsafe_url
        path.write_text(json.dumps(raw))

        items, _, _ = load_newsletter_artifact(path)

        assert items[0].url == ""
        assert items[0].canonical_url.startswith("newsletter:")
        assert "reader" not in items[0].canonical_url
        assert "beehiiv" not in items[0].canonical_url

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


class TestNewsletterUrlsNeverRideTheCluster:
    """Review round 1, M3: the cluster was the one channel a newsletter URL
    could reach a card that carries none of the newsletter guards."""

    def test_fuzzy_merge_refuses_the_newsletter_copy_url(self):
        from curator.dedup import dedupe

        publisher = Item(
            title="Apple ships a quantum chip",
            url="https://publisher.example/story",
            canonical_url="https://publisher.example/story",
            source_id="verge",
            source_name="The Verge",
            published_at=NOW - timedelta(hours=2),
        )
        newsletter = Item(
            title="Apple ships the quantum chip",
            url="https://cleaned.example/story?email=fixture-reader%40example.invalid",
            canonical_url="https://cleaned.example/story",
            source_id="newsletter:tldr",
            source_name="TLDR",
            published_at=NOW - timedelta(hours=1),
            is_newsletter=True,
            is_aggregator=True,
            newsletter_sender="TLDR",
            native_categories={"newsletters"},
        )
        (survivor,) = dedupe([publisher, newsletter])
        assert survivor.source_name == "The Verge"  # publisher wins the merge
        assert survivor.cluster == []  # and the newsletter URL rode nothing
        assert "newsletters" in survivor.native_categories  # cross-tag survives

    def test_publisher_url_may_enter_a_newsletter_cards_cluster(self):
        from curator.dedup import dedupe

        newsletter = Item(
            title="Only the newsletter had it first",
            url="https://cleaned.example/first",
            canonical_url="https://cleaned.example/first",
            source_id="newsletter:tldr",
            source_name="TLDR",
            published_at=NOW - timedelta(hours=4),
            is_newsletter=True,
            is_aggregator=True,
            newsletter_sender="TLDR",
        )
        publisher = Item(
            title="Only the newsletter had it, first",
            url="https://publisher.example/first",
            canonical_url="https://publisher.example/first",
            source_id="verge",
            source_name="The Verge",
            published_at=NOW - timedelta(hours=3),
        )
        (survivor,) = dedupe([newsletter, publisher])
        assert survivor.source_name == "The Verge"
        assert survivor.cluster == []


class TestReservedCategoryId:
    def test_reserved_name_is_rejected(self, tmp_path):
        from curator.config import ConfigError, load_topics

        topics = tmp_path / "topics.yaml"
        topics.write_text("categories:\n  - name: Newsletters\n    keywords: [ai]\n")
        with pytest.raises(ConfigError, match="reserved"):
            load_topics(topics)

    def test_reserved_id_is_rejected(self, tmp_path):
        from curator.config import ConfigError, load_topics

        topics = tmp_path / "topics.yaml"
        topics.write_text("categories:\n  - name: My Topic\n    id: newsletters\n    keywords: [ai]\n")
        with pytest.raises(ConfigError, match="reserved"):
            load_topics(topics)


class TestClusterLinksAreSanitizedAtTheOutputBoundary:
    def render_with_cluster(self, url: str) -> str:
        from curator.render import render_html

        item = Item(
            title="A story with an alternate address",
            url="https://publisher.example/main",
            canonical_url="https://publisher.example/main",
            source_id="verge",
            source_name="The Verge",
            published_at=NOW - timedelta(hours=1),
            cluster=[{"source_name": "Elsewhere", "url": url}],
        )
        return render_html({"AI": [item]}, [], NOW)

    def test_a_tracker_cluster_link_is_dropped(self):
        html = self.render_with_cluster(
            "https://link.mail.beehiiv.com/ss/c/OPAQUEFAKETOKEN123456"
        )
        assert "beehiiv" not in html
        assert "Elsewhere" not in html

    def test_an_email_bearing_cluster_link_is_dropped(self):
        html = self.render_with_cluster(
            "https://publisher.example/other?email=fixture-reader%40example.invalid"
        )
        assert "example.invalid" not in html
        assert "%40" not in html

    def test_a_clean_cluster_link_survives(self):
        html = self.render_with_cluster("https://other.example/coverage")
        assert 'href="https://other.example/coverage"' in html
        assert "Elsewhere" in html


class TestLaneCapIsFairAcrossSenders:
    def records(self, big: int, small: int) -> list[dict]:
        rows = [{"source_id": "newsletter:big", "published_at": NOW - timedelta(minutes=n)}
                for n in range(big)]
        rows += [{"source_id": "newsletter:small", "published_at": NOW - timedelta(hours=1, minutes=n)}
                 for n in range(small)]
        return rows

    def test_round_robin_before_the_cap(self):
        """One prolific sender must not own the tab: live week one had TLDR
        taking 43 of 50 slots and The Rundown publishing zero."""
        from curator.newsletter.lane import fair_cap

        taken = fair_cap(self.records(big=8, small=3), cap=6)
        assert len(taken) == 6
        small = sum(1 for r in taken if r["source_id"] == "newsletter:small")
        assert small == 3  # the small sender keeps every story it had

    def test_under_cap_everything_ships_newest_first(self):
        from curator.newsletter.lane import fair_cap

        taken = fair_cap(self.records(big=2, small=2), cap=50)
        assert len(taken) == 4
        assert [r["published_at"] for r in taken] == sorted(
            (r["published_at"] for r in taken), reverse=True
        )

    def test_cap_zero_ships_nothing(self):
        from curator.newsletter.lane import fair_cap

        assert fair_cap(self.records(big=3, small=1), cap=0) == []


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
