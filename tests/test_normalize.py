"""Normalization: display fidelity and link safety.

Both bugs pinned here were found in adversarial review and were real.
"""

from __future__ import annotations

import pytest

from curator.normalize import canonical_url, clean_title, fold_text, safe_url


class TestCleanTitle:
    def test_entities_are_decoded(self):
        assert clean_title("AI &amp; the law") == "AI & the law"

    def test_comparison_operators_survive(self):
        # Regression: unescaping before stripping tags turned this into "2 1",
        # because "< 3 >" then looked like markup.
        assert clean_title("2 &lt; 3 &gt; 1") == "2 < 3 > 1"

    def test_real_markup_is_removed(self):
        assert clean_title("<b>Big</b> news") == "Big news"

    def test_publisher_punctuation_is_preserved(self):
        # The displayed headline must be faithful. Smart quotes and em dashes
        # are the publisher's, not ours to rewrite.
        assert clean_title("It’s here — finally") == "It’s here — finally"

    def test_whitespace_collapses(self):
        assert clean_title("  a\n\tb  ") == "a b"

    def test_empty(self):
        assert clean_title("") == ""


class TestFoldText:
    def test_smart_quotes_folded_for_matching(self):
        assert fold_text("It’s") == "It's"

    def test_em_dash_folded(self):
        assert fold_text("a — b") == "a - b"


class TestSafeUrl:
    @pytest.mark.parametrize(
        "bad",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "/relative/path",
            "not a url",
            "",
        ],
    )
    def test_unsafe_schemes_rejected(self, bad):
        # Regression: HTML-escaping an href does not neutralize javascript:.
        # A compromised feed must not be able to choose what we link to.
        assert safe_url(bad) is None
        assert canonical_url(bad) is None

    @pytest.mark.parametrize("good", ["https://example.com/a", "http://example.com"])
    def test_http_schemes_accepted(self, good):
        assert safe_url(good) == good


class TestCanonicalUrl:
    def test_strips_tracking_params(self):
        assert (
            canonical_url("https://example.com/a?utm_source=x&utm_medium=y&id=7")
            == "https://example.com/a?id=7"
        )

    def test_strips_fragment_and_www_and_trailing_slash(self):
        assert canonical_url("https://www.example.com/a/#top") == "https://example.com/a"

    def test_preserves_meaningful_params(self):
        assert canonical_url("https://example.com/p?page=2") == "https://example.com/p?page=2"

    def test_variants_collapse_to_one_identity(self):
        a = canonical_url("https://www.example.com/story?utm_campaign=z")
        b = canonical_url("http://example.com/story/#section")
        assert a.split("://", 1)[1] == b.split("://", 1)[1]
