"""Pipeline wiring, and the publish guard that protects a good page."""

from __future__ import annotations

from curator.config import Category, Config
from curator.models import TierResult
from curator.pipeline import build, main
from tests.conftest import make_item


def config(**kw) -> Config:
    base = dict(
        categories=[Category(name="AI", keywords=["AI"])],
        rss=[],
        settings={"max_age_hours": 48},
        ranking={},
        dedup={},
        hackernews={},
        reddit={},
    )
    base.update(kw)
    return Config(**base)


class TestBuild:
    def test_matching_item_reaches_its_topic(self, now):
        results = [TierResult(tier="t", items=[make_item("AI is here")])]
        assert len(build(config(), results, now)["AI"]) == 1

    def test_non_matching_item_is_dropped(self, now):
        results = [TierResult(tier="t", items=[make_item("Gardening tips")])]
        assert build(config(), results, now)["AI"] == []

    def test_stale_item_is_dropped(self, now):
        results = [TierResult(tier="t", items=[make_item("AI is here", hours_ago=200)])]
        assert build(config(), results, now)["AI"] == []

    def test_duplicates_collapse_before_display(self, now):
        results = [
            TierResult(
                tier="t",
                items=[
                    make_item("AI is here", "https://e.com/x", platform="a"),
                    make_item("AI is here", "https://e.com/x", platform="b"),
                ],
            )
        ]
        assert len(build(config(), results, now)["AI"]) == 1

    def test_respects_max_items_per_topic(self, now):
        items = [make_item(f"AI story {i}", f"https://e.com/{i}") for i in range(50)]
        cfg = config(settings={"max_age_hours": 48, "max_items_per_topic": 5})
        assert len(build(cfg, [TierResult(tier="t", items=items)], now)["AI"]) == 5

    def test_a_dead_tier_does_not_lose_the_others(self, now):
        results = [
            TierResult(tier="dead", items=[], ok=False, note="unavailable"),
            TierResult(tier="live", items=[make_item("AI is here")]),
        ]
        assert len(build(config(), results, now)["AI"]) == 1


class TestPublishGuard:
    """The guard must count VISIBLE rows, not fetched items."""

    def _repo(self, tmp_path):
        (tmp_path / "topics.yaml").write_text(
            "topics:\n  - name: AI\n    keywords:\n      - AI\n", encoding="utf-8"
        )
        (tmp_path / "sources.yaml").write_text("rss: []\n", encoding="utf-8")
        return tmp_path

    def test_offline_run_without_override_refuses_to_publish(self, tmp_path):
        # Regression: the old guard passed whenever any tier returned anything,
        # so an irrelevant successful fetch could overwrite a good page with an
        # empty one.
        root = self._repo(tmp_path)
        assert main(["--root", str(root), "--offline", "--out", str(root / "site")]) == 1
        assert not (root / "site" / "index.html").exists()

    def test_allow_empty_overrides_the_guard(self, tmp_path):
        root = self._repo(tmp_path)
        code = main(["--root", str(root), "--offline", "--allow-empty", "--out", str(root / "site")])
        assert code == 0
        assert (root / "site" / "index.html").exists()

    def test_bad_config_exits_two(self, tmp_path):
        (tmp_path / "topics.yaml").write_text("topics:\n  - name: X\n    keywords: AI\n", encoding="utf-8")
        (tmp_path / "sources.yaml").write_text("rss: []\n", encoding="utf-8")
        assert main(["--root", str(tmp_path), "--offline"]) == 2


class TestRound2EmptyTopicsGuard:
    def test_empty_topics_still_cannot_overwrite_a_page(self, tmp_path):
        # The guard used to be conditioned on topics being configured, so
        # `topics: []` walked straight past it and published a blank page.
        (tmp_path / "topics.yaml").write_text("topics: []\n", encoding="utf-8")
        (tmp_path / "sources.yaml").write_text("rss: []\n", encoding="utf-8")
        assert main(["--root", str(tmp_path), "--offline", "--out", str(tmp_path / "site")]) == 1
        assert not (tmp_path / "site" / "index.html").exists()
