"""Rendering: escaping, honest health reporting, and the smoke test."""

from __future__ import annotations

from datetime import timedelta

from curator.models import TierResult
from curator.render import human_age, render_html, render_site
from tests.conftest import make_item


def render(ranked, results=None, now=None, **kw):
    from tests.conftest import NOW

    return render_html(ranked, results or [], now or NOW, **kw)


class TestEscaping:
    def test_script_in_a_headline_is_escaped(self, now):
        html = render({"T": [make_item("<script>alert(1)</script> hi")]}, now=now)
        assert "<script>alert(1)</script> hi" not in html
        assert "&lt;script&gt;" in html

    def test_quotes_in_a_headline_do_not_break_the_attribute(self, now):
        html = render({'T"x': [make_item('He said "hello"')]}, now=now)
        assert "&quot;" in html

    def test_topic_name_is_escaped(self, now):
        html = render({"<b>T</b>": [make_item("a")]}, now=now)
        assert "<b>T</b>" not in html


class TestContent:
    def test_headline_and_link_render(self, now):
        html = render({"T": [make_item("Real headline", "https://example.com/a")]}, now=now)
        assert "Real headline" in html and 'href="https://example.com/a"' in html

    def test_empty_topic_says_so(self, now):
        assert "Nothing matched" in render({"T": []}, now=now)

    def test_no_topics_still_renders(self, now):
        html = render({}, now=now)
        assert "<html" in html and "</html>" in html

    def test_aggregator_rows_are_labeled_via(self, now):
        item = make_item("Submitted story", source_name="Hacker News", aggregator=True)
        assert "via Hacker News" in render({"T": [item]}, now=now)

    def test_publisher_rows_are_not_labeled_via(self, now):
        item = make_item("Published story", source_name="The Verge")
        html = render({"T": [item]}, now=now)
        assert "The Verge" in html and "via The Verge" not in html

    def test_echo_badge_only_with_multiple_platforms(self, now):
        lone = make_item("a")
        assert "sources</span>" not in render({"T": [lone]}, now=now)
        echoed = make_item("b")
        echoed.echo_platforms = {"x", "y"}
        assert "2 sources" in render({"T": [echoed]}, now=now)

    def test_page_makes_no_external_requests(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        for marker in ("<img", "<script src", "fonts.googleapis", "cdn."):
            assert marker not in html

    def test_schedule_wording_is_not_a_promise(self, now):
        # "refreshes hourly" claims something GitHub cron cannot guarantee.
        html = render({"T": [make_item("a")]}, now=now)
        assert "scheduled hourly" in html and "refreshes hourly" not in html


class TestHealthLine:
    def test_healthy_tier_shows_a_count(self, now):
        results = [TierResult(tier="rss", items=[make_item("a")])]
        assert "rss: 1 items" in render({"T": []}, results, now=now)

    def test_partial_failure_is_not_hidden_behind_a_count(self, now):
        # Regression: a tier with items AND a failure used to render as a
        # reassuring "reddit: 10", hiding the degradation entirely.
        results = [
            TierResult(tier="reddit", items=[make_item("a")], ok=False, note="rate-limited after 2/5")
        ]
        html = render({"T": []}, results, now=now)
        assert "degraded" in html and "rate-limited after 2/5" in html

    def test_dead_tier_is_named(self, now):
        results = [TierResult(tier="reddit", items=[], ok=False, note="blocked")]
        assert "reddit: blocked" in render({"T": []}, results, now=now)


class TestStaleness:
    """Staleness is a property of WHEN YOU LOOK, so it is evaluated in the
    reader's browser. A server-side check could never fire: the build always
    renders itself as zero seconds old."""

    def test_build_time_is_embedded_machine_readable(self, now):
        html = render({"T": []}, now=now, built_at=now - timedelta(hours=9))
        assert f'data-built="{(now - timedelta(hours=9)).isoformat()}"' in html

    def test_threshold_is_embedded(self, now):
        assert 'data-after="3"' in render({"T": []}, now=now, built_at=now)

    def test_indicator_starts_hidden(self, now):
        # It must not flash on a fresh page before the script runs.
        html = render({"T": []}, now=now, built_at=now)
        assert 'id="stale"' in html and 'hidden></span>' in html

    def test_client_computes_the_age(self, now):
        html = render({"T": []}, now=now, built_at=now)
        assert "Date.parse(el.dataset.built)" in html and "last build " in html


class TestRepoLink:
    def test_repo_url_is_linked_when_given(self, now):
        html = render({"T": []}, now=now, repo_url="https://github.com/someone/news-curator")
        assert "https://github.com/someone/news-curator" in html

    def test_unsafe_repo_url_is_dropped(self, now):
        html = render({"T": []}, now=now, repo_url="javascript:alert(1)")
        assert "javascript:" not in html

    def test_no_hardcoded_upstream_owner(self, now):
        # A fork must advertise itself, not whoever it was forked from.
        assert "joydai2026-del" not in render({"T": [make_item("a")]}, now=now)


class TestHumanAge:
    def test_minutes(self, now):
        assert human_age(make_item("a", hours_ago=0.5), now) == "30m ago"

    def test_hours(self, now):
        assert human_age(make_item("a", hours_ago=5), now) == "5h ago"

    def test_one_day(self, now):
        assert human_age(make_item("a", hours_ago=25), now) == "1 day ago"

    def test_days(self, now):
        assert human_age(make_item("a", hours_ago=50), now) == "2 days ago"


class TestRenderSite:
    def test_writes_index_and_nojekyll(self, tmp_path, now):
        path = render_site({"T": [make_item("a")]}, [], now, tmp_path)
        assert path.exists() and (tmp_path / ".nojekyll").exists()
        assert "<html" in path.read_text(encoding="utf-8")

    def test_leaves_no_temp_file_behind(self, tmp_path, now):
        render_site({"T": [make_item("a")]}, [], now, tmp_path)
        assert list(tmp_path.glob("*.tmp")) == []


class TestOutputBoundarySafety:
    """Round 2: the renderer revalidates, it does not trust upstream."""

    def test_unsafe_url_row_is_dropped_not_rendered(self, now):
        # An Item built directly (bypassing the fetchers) must still not be
        # able to put a javascript: href on a public page.
        bad = make_item("Innocent looking headline")
        bad.url = "javascript:alert(1)"
        html = render({"T": [bad]}, now=now)
        assert "javascript:" not in html.lower()
        assert "Nothing matched" in html

    def test_good_rows_survive_alongside_a_bad_one(self, now):
        bad = make_item("Bad row", "https://example.com/bad")
        bad.url = "javascript:alert(1)"
        good = make_item("Good row", "https://example.com/good")
        html = render({"T": [bad, good]}, now=now)
        assert "Good row" in html and "Bad row" not in html


class TestUpdatedTimeIsQualified:
    """Round 2: an 'updated' timestamp must not be displayed as a publish time."""

    def test_estimated_time_says_updated(self, now):
        item = make_item("A story", hours_ago=3)
        item.time_is_estimated = True
        assert "updated 3h ago" in render({"T": [item]}, now=now)

    def test_real_publish_time_is_unqualified(self, now):
        html = render({"T": [make_item("A story", hours_ago=3)]}, now=now)
        assert "3h ago" in html and "updated 3h ago" not in html


class TestCname:
    def test_cname_is_copied_into_the_output(self, tmp_path, now):
        from curator.render import render_site

        src = tmp_path / "CNAME"
        src.write_text("news.example.org\n", encoding="utf-8")
        out = tmp_path / "site"
        render_site({"T": [make_item("a")]}, [], now, out, cname_source=src)
        assert (out / "CNAME").read_text(encoding="utf-8").strip() == "news.example.org"

    def test_absent_cname_is_fine(self, tmp_path, now):
        from curator.render import render_site

        out = tmp_path / "site"
        render_site({"T": [make_item("a")]}, [], now, out, cname_source=tmp_path / "CNAME")
        assert not (out / "CNAME").exists()
