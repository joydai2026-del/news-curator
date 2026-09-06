"""Grounded summary enrichment for visible stories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from curator.summaries import (
    SummaryCache,
    compose_summary,
    enrich,
    parse_article_text,
    summary_is_usable,
)
from curator.sources import SafeHttpResponse
from tests.conftest import make_item


NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
USABLE_SUMMARY = (
    "The first sentence provides the central fact and enough concrete context for the reader. "
    "The second sentence explains why the development matters to the people involved. "
    "The third sentence adds the next expected step and a useful timeline."
)


class TestArticleText:
    def test_prefers_declared_description_and_article_paragraphs(self):
        markup = """
        <html><head><meta property="og:description" content="A concise publisher overview."></head>
        <body><nav><p>Subscribe now for alerts and offers.</p></nav><article>
        <p>The first reporting paragraph explains what happened and names the people involved.</p>
        <p>The second reporting paragraph adds the consequences and the relevant timeline.</p>
        </article></body></html>
        """
        meta, paragraphs = parse_article_text(markup)
        assert meta == ["A concise publisher overview."]
        assert paragraphs == [
            "The first reporting paragraph explains what happened and names the people involved.",
            "The second reporting paragraph adds the consequences and the relevant timeline.",
        ]
        assert "Subscribe" not in " ".join(paragraphs)

    def test_script_style_and_cookie_copy_are_not_summary_material(self):
        markup = """
        <html><body><main><script>secret()</script><style>.x{}</style>
        <p>We use cookies to improve your experience and personalize advertising.</p>
        <p>The company released the system after a six-month pilot across three production teams.</p>
        <p>Customers can use the system today, while a wider rollout is planned for next quarter.</p>
        </main></body></html>
        """
        _meta, paragraphs = parse_article_text(markup)
        assert len(paragraphs) == 2
        assert "secret" not in " ".join(paragraphs)
        assert "cookies" not in " ".join(paragraphs).casefold()

    def test_article_fetch_requests_the_story_language(self):
        class RecordingTransport:
            def __init__(self):
                self.kwargs = {}

            def get(self, _source_id, url, **kwargs):
                self.kwargs = kwargs
                body = (
                    "<body><article><p>第一句说明新闻发生了什么，并提供读者理解事件所需的具体背景信息。</p>"
                    "<p>第二句解释这项进展为什么重要，以及它会影响哪些相关人员和行业。</p>"
                    "<p>第三句补充下一步行动、预计时间，以及目前仍需要持续观察的问题。</p></article></body>"
                ).encode()
                return SafeHttpResponse(200, url, {"content-type": "text/html"}, body)

        transport = RecordingTransport()
        from curator.summaries import fetch_summary

        summary, outcome = fetch_summary(
            "https://publisher.example/story",
            existing="",
            user_agent="test",
            timeout=5,
            max_bytes=4096,
            minimum_characters=70,
            minimum_sentences=3,
            target_characters=70,
            maximum_characters=240,
            language="zh",
            transport=transport,  # type: ignore[arg-type]
        )
        assert outcome == "ok" and summary
        assert transport.kwargs["headers"]["Accept-Language"].startswith("zh-CN")


class TestComposeSummary:
    def test_combines_distinct_grounded_sentences_to_the_target(self):
        text = compose_summary(
            "The launch completed successfully after several years of development.",
            ["The rocket reached orbit from Norway and carried multiple commercial payloads."],
            [
                "The mission makes the company the first European commercial operator to reach orbit.",
                "Executives said another flight is planned after teams review this mission's data.",
            ],
            minimum_characters=180,
            minimum_sentences=3,
            target_characters=260,
            maximum_characters=420,
        )
        assert summary_is_usable(text, minimum_characters=180, minimum_sentences=3)
        assert len(text) <= 420
        assert text.count("launch completed") == 1

    def test_repeated_metadata_is_not_duplicated(self):
        sentence = "The company released the system after a six-month pilot across three production teams."
        text = compose_summary(
            sentence,
            [sentence],
            [
                "Customers can use the system today, while a wider rollout is planned for next quarter.",
                "The company says pricing will be announced before the wider rollout begins.",
            ],
            minimum_characters=150,
            minimum_sentences=3,
            target_characters=220,
            maximum_characters=360,
        )
        assert text.count(sentence) == 1

    def test_cjk_sentences_without_spaces_meet_the_quality_gate(self):
        text = (
            "第一句说明新闻发生了什么，并提供读者理解事件所需的具体背景。"
            "第二句解释这项进展为什么重要，以及它会影响哪些相关人员。"
            "第三句补充下一步行动、预计时间，以及目前仍需要观察的问题。"
        )
        assert summary_is_usable(text, minimum_characters=70, minimum_sentences=3)

    def test_cjk_publisher_text_is_preserved_during_composition(self):
        text = compose_summary(
            "",
            ["第一句说明新闻发生了什么，并提供读者理解事件所需的具体背景信息。"],
            [
                "第二句解释这项进展为什么重要，以及它会影响哪些相关人员和行业。",
                "第三句补充下一步行动、预计时间，以及目前仍需要持续观察的问题。",
            ],
            minimum_characters=70,
            minimum_sentences=3,
            target_characters=70,
            maximum_characters=240,
        )
        assert text.startswith("第一句")
        assert "第二句" in text and "第三句" in text
        assert summary_is_usable(text, minimum_characters=70, minimum_sentences=3)


class TestSummaryCache:
    def test_valid_summary_round_trips(self, tmp_path):
        path = tmp_path / "summary_cache.json"
        text = (
            "The first sentence provides the central fact and enough concrete context for the reader. "
            "The second sentence explains why the development matters to the people involved. "
            "The third sentence adds the next expected step and a useful timeline."
        )
        cache = SummaryCache(path)
        cache.put("k", text, "ok", NOW)
        assert cache.save() is True
        hit, got = SummaryCache.load(path).get(
            "k", NOW, retry_error_after_hours=24, minimum_characters=180, minimum_sentences=3
        )
        assert hit is True and got == text

    def test_hand_edited_short_cache_value_is_rejected(self):
        cache = SummaryCache(None)
        cache.entries["k"] = {
            "summary": "Too short.", "outcome": "ok",
            "checked_at": NOW.isoformat(), "seen_at": NOW.isoformat(),
        }
        assert cache.get(
            "k", NOW, retry_error_after_hours=24, minimum_characters=180, minimum_sentences=3
        ) == (False, "")

    def test_clean_negative_answer_is_retried_after_the_configured_window(self):
        cache = SummaryCache(None)
        cache.put("k", None, "none", NOW)
        assert cache.get("k", NOW, retry_error_after_hours=6)[0] is True
        assert cache.get("k", NOW + timedelta(hours=7), retry_error_after_hours=6)[0] is False


class TestEnrich:
    def test_disabled_policy_does_not_clear_existing_short_text(self):
        item = make_item("Story", description="A short publisher sentence.")
        stats = enrich([item], SummaryCache(None), NOW, user_agent="t", config={
            "enabled": False, "minimum_characters": 180, "minimum_sentences": 3,
        })
        assert item.description == "A short publisher sentence."
        assert stats["unusable"] == 0

    def test_long_feed_summary_needs_no_fetch(self, monkeypatch):
        from curator import summaries

        def boom(*_a, **_k):
            raise AssertionError("network fetch should not run")

        monkeypatch.setattr(summaries, "fetch_summary", boom)
        item = make_item("Story", description=USABLE_SUMMARY)
        stats = enrich([item], SummaryCache(None), NOW, user_agent="t", config={
            "enabled": True, "minimum_characters": 180, "minimum_sentences": 3,
        })
        assert stats["from_feed"] == 1 and stats["fetched"] == 0

    def test_short_summary_is_enriched_for_every_category_copy(self, monkeypatch):
        from curator import summaries

        summary = (
            "The first sentence supplies the central fact from the publisher's article. "
            "The second sentence adds concrete background needed to understand the development. "
            "The third sentence explains the immediate next step and its expected timing."
        )
        monkeypatch.setattr(summaries, "fetch_summary", lambda *_a, **_k: (summary, "ok"))
        a = make_item("Story", "https://e.example/a", description="Brief note.")
        b = make_item("Story", "https://e.example/a", description="Brief note.")
        stats = enrich([a, b], SummaryCache(None), NOW, user_agent="t", config={
            "enabled": True, "minimum_characters": 180, "minimum_sentences": 3,
        })
        assert stats["fetched"] == 1
        assert a.description == b.description == summary

    def test_same_url_in_two_languages_never_shares_a_summary(self, monkeypatch):
        from curator import summaries

        english = USABLE_SUMMARY
        chinese = (
            "第一句说明新闻发生了什么，并提供读者理解事件所需的具体背景信息。"
            "第二句解释这项进展为什么重要，以及它会影响哪些相关人员和行业。"
            "第三句补充下一步行动、预计时间，以及目前仍需要持续观察的问题。"
        )
        calls = []

        def fetched(*_args, language="en", **_kwargs):
            calls.append(language)
            return (chinese if language == "zh" else english), "ok"

        monkeypatch.setattr(summaries, "fetch_summary", fetched)
        en = make_item("Story", "https://e.example/bilingual", description="Brief.")
        zh = make_item("报道", "https://e.example/bilingual", description="简讯。")
        zh.language = "zh"
        stats = enrich([en, zh], SummaryCache(None), NOW, user_agent="t", config={
            "enabled": True, "minimum_characters": 70, "minimum_sentences": 3,
        })
        assert sorted(calls) == ["en", "zh"]
        assert en.description == english
        assert zh.description == chinese
        assert stats["fetched"] == 2

    def test_newsletter_url_is_never_fetched(self, monkeypatch):
        from curator import summaries

        def boom(*_a, **_k):
            raise AssertionError("newsletter URL reached the network")

        monkeypatch.setattr(summaries, "fetch_summary", boom)
        item = make_item("Newsletter", "https://sender.example/r/private", description="Short.")
        item.is_newsletter = True
        stats = enrich([item], SummaryCache(None), NOW, user_agent="t", config={"enabled": True})
        assert stats["newsletter_skipped"] == 1 and item.description == ""

    def test_unenrichable_story_is_cleared_so_renderer_drops_it(self, monkeypatch):
        from curator import summaries

        monkeypatch.setattr(summaries, "fetch_summary", lambda *_a, **_k: (None, "none"))
        item = make_item("Story", description="Short.")
        stats = enrich([item], SummaryCache(None), NOW, user_agent="t", config={"enabled": True})
        assert stats["unusable"] == 1 and item.description == ""

    def test_retryable_error_ages_out(self):
        cache = SummaryCache(None)
        cache.put("k", None, "error", NOW)
        assert cache.get("k", NOW, retry_error_after_hours=24)[0] is True
        assert cache.get("k", NOW + timedelta(hours=25), retry_error_after_hours=24)[0] is False
