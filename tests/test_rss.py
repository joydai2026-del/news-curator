"""The RSS fetcher's pure parts: what it takes out of one feed entry.

`entry_summary` is tested against plain dicts because that is what a feedparser
entry behaves like for the two calls this function makes, and because the whole
point is that the function is pure: no network, no parser, no fixture file.
"""

from __future__ import annotations

from curator.fetchers.rss import MAX_DESCRIPTION_CHARS, entry_summary


class TestEntrySummary:
    def test_a_plain_summary_comes_through(self):
        assert entry_summary({"summary": "A sentence about the story."}) == (
            "A sentence about the story."
        )

    def test_description_is_used_when_there_is_no_summary(self):
        assert entry_summary({"description": "The other field."}) == "The other field."

    def test_summary_wins_over_description(self):
        # Atom keeps them distinct: `summary` is the human sentence, `content`
        # is the article. We want the sentence.
        entry = {"summary": "The sentence.", "description": "Something else."}
        assert entry_summary(entry) == "The sentence."

    def test_nothing_at_all_is_an_empty_string(self):
        # HN and Reddit items have no summary, and a card without one is fine.
        assert entry_summary({}) == ""

    def test_markup_is_stripped(self):
        assert entry_summary({"summary": "<p>Real <b>news</b></p>"}) == "Real news"

    def test_comparison_operators_survive(self):
        # The same ordering bug `clean_title` exists to prevent: unescaping
        # before stripping tags turns this into "2 1", because "< 3 >" then
        # looks like markup. Reusing that function is why this passes.
        assert entry_summary({"summary": "2 &lt; 3 &gt; 1"}) == "2 < 3 > 1"

    def test_entities_are_decoded_exactly_once(self):
        assert entry_summary({"summary": "AI &amp;amp; the law"}) == "AI &amp; the law"

    def test_whitespace_collapses(self):
        assert entry_summary({"summary": "  a\n\n\tb  "}) == "a b"

    def test_publisher_punctuation_is_preserved(self):
        assert entry_summary({"summary": "It’s here — finally"}) == "It’s here — finally"

    def test_a_script_tag_does_not_survive_as_markup(self):
        assert "<script" not in entry_summary({"summary": "<script>alert(1)</script>hi"})


class TestSummaryTruncation:
    def test_a_long_summary_is_capped(self):
        # Some feeds put the whole article in `description`. That is a static
        # page, not a mirror.
        text = entry_summary({"summary": "word " * 500})
        assert len(text) <= MAX_DESCRIPTION_CHARS + 1  # the ellipsis

    def test_truncation_cuts_at_a_word_boundary(self):
        text = entry_summary({"summary": "alpha " * 400})
        assert text.endswith("…")
        assert "alph…" not in text, "a clamped card must not imply a half-written word"

    def test_a_short_summary_is_untouched(self):
        assert entry_summary({"summary": "Short."}) == "Short."

    def test_the_cap_is_a_dial(self):
        assert entry_summary({"summary": "one two three four five"}, limit=10) == "one two…"
