"""Locale overlays must not change the original model input or story identity."""
from curator.recommendation.service import RankingService

def test_card_uses_display_overlay_without_relabeling_source_language():
    row = {"story_id":"story:" + "1"*64, "title":"原始标题", "summary":"原文",
           "display_title":"Translated title", "display_summary":"Translated summary",
           "source_name":"Publisher", "published_at":"2026-09-15T00:00:00Z",
           "canonical_url":"https://example.org/story", "source_id":"public", "language":"zh"}
    card=RankingService._card(row,{})
    assert card["title"] == row["display_title"]
    assert card["summary"] == row["display_summary"]
    assert card["language"] == "zh"
    assert row["title"] == "原始标题"

def test_public_bindings_include_frozen_display_language():
    assert RankingService._public_bindings({"display_language":"zh"}) == {"display_language":"zh"}
