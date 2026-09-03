"""Rendering: escaping, honest health reporting, and the smoke test."""

from __future__ import annotations

from datetime import timedelta

import re

from curator.models import TierResult
from curator.render import human_age, render_html, render_site
from tests.conftest import make_item, make_newsletter_item


def flat(html: str) -> str:
    """Collapse whitespace before matching prose.

    The footer is wrapped for readability in the source, so a phrase can span a
    line break and a literal substring check on the raw output fails for a
    reason that has nothing to do with what the page says.
    """
    return re.sub(r"\s+", " ", html)


def cards(html: str) -> list[str]:
    """Every rendered card, as its own chunk of markup.

    Card-level assertions have to be card-level. Searching the whole page for
    `href=` finds the footer, and searching it for `<img` cannot tell which card
    the image belongs to.
    """
    return re.findall(r"<article class=\"card\".*?</article>", html, re.S)


def card_with(html: str, needle: str) -> str:
    matches = [c for c in cards(html) if needle in c]
    assert len(matches) == 1, f"expected exactly one card containing {needle!r}, got {len(matches)}"
    return matches[0]


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

    def test_the_page_loads_no_third_party_code(self, now):
        # v2 shows the publishers' pictures, so "<img" is no longer a red flag.
        # Third-party CODE still is: no external script, no web font, no CDN.
        html = render({"T": [make_item("a")]}, now=now)
        for marker in ("<script src", "fonts.googleapis", "<link rel=\"stylesheet\""):
            assert marker not in html

    def test_schedule_wording_is_not_a_promise(self, now):
        # "refreshes daily" claims something GitHub cron cannot guarantee.
        html = render({"T": [make_item("a")]}, now=now)
        assert "scheduled daily" in html and "refreshes daily" not in html


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
        assert 'data-after="27"' in render({"T": []}, now=now, built_at=now)

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


class TestPreviewImageData:
    """v2 draws the image. `data-image` stays on the article beside it.

    The deploy workflow counts image coverage with one cheap regex over that
    attribute, and the attribute still says exactly what it always said: the
    address the publisher declared.
    """

    def test_an_image_is_carried_as_a_data_attribute_on_the_article(self, now):
        item = make_item("A story", "https://example.com/a")
        item.image_url = "https://cdn.example/a.jpg"
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert 'data-image="https://cdn.example/a.jpg"' in card
        assert card.index('data-image') < card.index("<img"), "the attribute belongs on the article"

    def test_a_row_without_an_image_carries_no_attribute(self, now):
        # An empty attribute would look like an answer. Absent means absent, so
        # the workflow's coverage count is a count of real images. Asserted on
        # the CARD, because the stylesheet mentions the attribute by name.
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert "data-image" not in card

    def test_the_image_is_now_a_real_img_with_the_privacy_attributes(self, now):
        item = make_item("A story")
        item.image_url = "https://cdn.example/a.jpg"
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert '<img src="https://cdn.example/a.jpg"' in card
        for attribute in ('loading="lazy"', 'decoding="async"', 'referrerpolicy="no-referrer"'):
            assert attribute in card

    def test_the_document_asks_for_no_referrer_too(self, now):
        # Per-image `referrerpolicy` and the document-level meta are not the
        # same defence: the meta covers anything a later change adds.
        assert '<meta name="referrer" content="no-referrer">' in render({"T": []}, now=now)

    def test_a_failed_image_reveals_the_typographic_card(self, now):
        # The fallback is not a separate code path that could rot. It is the
        # layer underneath every image card, so the browser-side failure and
        # the no-image-at-all case land in exactly the same place.
        item = make_item("A story")
        item.image_url = "https://cdn.example/a.jpg"
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert 'class="fb"' in card and "onerror=" in card

    def test_a_story_with_no_image_gets_a_designed_panel_not_a_hole(self, now):
        card = card_with(render({"AI": [make_item("A story")]}, now=now), "A story")
        assert "<img" not in card
        assert 'class="fb" style="--h:' in card, "the panel carries its category accent"
        assert ">AI</span>" in card, "and names the category on it"

    def test_an_unsafe_image_url_never_reaches_the_page(self, now):
        # Last gate before a publisher-supplied string becomes an attribute on
        # a public page. A defence that only exists upstream is one refactor
        # away from being gone.
        item = make_item("A story")
        item.image_url = "javascript:alert(1)"
        html = render({"T": [item]}, now=now)
        assert "javascript:" not in html
        assert "data-image" not in card_with(html, "A story")

    def test_quotes_in_an_image_url_are_escaped(self, now):
        item = make_item("A story")
        item.image_url = 'https://cdn.example/a.jpg?x="onload="alert(1)'
        html = render({"T": [item]}, now=now)
        assert 'onload="alert' not in html

    def test_the_footer_no_longer_claims_pages_are_never_fetched(self, now):
        # v1 promised destination pages are never fetched. v1.1 reads the head
        # of an article for its og:image, so that sentence had to go with the
        # feature rather than quietly outlive it.
        html = flat(render({"T": [make_item("a")]}, now=now))
        assert "never fetched" not in html
        assert "reads the head of an article" in html
        assert "no article text is stored or summarized" in html

    def test_the_footer_stopped_promising_no_third_party_requests(self, now):
        # v1.1 could honestly say the browser never requested the picture,
        # because the page did not draw it. v2 draws it. The promise had to go
        # with the behaviour rather than quietly outlive it.
        item = make_item("A story")
        item.image_url = "https://cdn.example/a.jpg"
        html = flat(render({"T": [item]}, now=now))
        assert "no third-party requests" not in html
        assert "never requests it" not in html
        assert "hotlinked from the publishers" in html
        assert "no-referrer" in html
        assert "newsletter items never load an image at all" in html


class TestAddTopicLink:
    """The manager's path: edit the keyword file, on GitHub, with no backend."""

    def test_a_github_repo_gets_an_editor_link(self, now):
        html = render(
            {"T": [make_item("a")]},
            now=now,
            repo_url="https://github.com/joydai2026-del/news-curator",
        )
        assert "https://github.com/joydai2026-del/news-curator/edit/main/topics.yaml" in html
        assert "Add a topic or keyword" in html

    def test_a_trailing_slash_does_not_double_up(self, now):
        html = render({"T": [make_item("a")]}, now=now, repo_url="https://github.com/a/b/")
        assert "https://github.com/a/b/edit/main/topics.yaml" in html

    def test_a_non_github_host_gets_instructions_instead_of_a_broken_link(self, now):
        # /edit/<branch>/<file> is GitHub's route. A self-hosted fork gets no
        # link rather than a wrong one.
        html = render({"T": [make_item("a")]}, now=now, repo_url="https://git.example/a/b")
        assert "/edit/main/topics.yaml" not in html
        assert "topics.yaml" in html

    def test_no_repo_url_still_explains_the_path(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert "/edit/main/" not in html
        assert "topics.yaml" in html

    def test_an_unsafe_repo_url_produces_no_link(self, now):
        html = render({"T": [make_item("a")]}, now=now, repo_url="javascript:alert(1)")
        assert "javascript:" not in html


class TestOneStoryOneCard:
    """A story in three categories is one card cross-tagged, never three rows.

    This is the correctness claim the whole v2 layout rests on. The old page
    rendered a section per category and repeated the story in each, which made
    the story count wrong, the search count wrong, and the "All" view a list of
    duplicates.
    """

    def cross_tagged(self):
        # `assign_categories` hands each bucket its own COPY, so this is what
        # the renderer actually receives: two objects, one canonical URL.
        shared_a = make_item("Shared story", "https://example.com/shared")
        shared_b = make_item("Shared story", "https://example.com/shared")
        return {
            "AI": [make_item("Only AI", "https://example.com/ai"), shared_a],
            "Crypto": [shared_b, make_item("Only crypto", "https://example.com/crypto")],
        }

    def test_a_cross_tagged_story_renders_exactly_once(self, now):
        html = render(self.cross_tagged(), now=now)
        assert len(re.findall(r">Shared story</a>", html)) == 1
        assert len(cards(html)) == 3

    def test_the_card_is_tagged_with_every_category_it_belongs_to(self, now):
        card = card_with(render(self.cross_tagged(), now=now), "Shared story")
        assert 'data-topics="crypto ai"' in card or 'data-topics="ai crypto"' in card

    def test_each_tab_keeps_its_own_exact_ranking(self, now):
        # Second in AI, first in Crypto. One node, two positions.
        card = card_with(render(self.cross_tagged(), now=now), "Shared story")
        assert 'data-rank-ai="1"' in card
        assert 'data-rank-crypto="0"' in card

    def test_every_card_carries_an_all_rank(self, now):
        html = render(self.cross_tagged(), now=now)
        assert all("data-rank-all=" in c for c in cards(html))

    def test_the_story_count_counts_stories_not_rows(self, now):
        # Three unique stories across two categories, one of them in both.
        assert "3 stories" in render(self.cross_tagged(), now=now)

    def test_the_eyebrow_names_the_category_it_ranked_best_in(self, now):
        card = card_with(render(self.cross_tagged(), now=now), "Shared story")
        assert '<p class="eyebrow">Crypto</p>' in card

    def test_two_categories_that_slugify_alike_do_not_merge(self, now):
        html = render({"AI!": [make_item("a", "https://e.com/1")],
                       "AI?": [make_item("b", "https://e.com/2")]}, now=now)
        assert 'data-filter="ai"' in html and 'data-filter="ai-2"' in html


class TestAccentHues:
    """"Every category has its own colour" is a claim about SPREAD.

    The first implementation hashed the slug, which is stable but not spread:
    the six shipped categories landed on hues 74, 79, 90, 95, 108 and 108. Five
    greens and an exact collision. This is the test that would have caught it.
    """

    def test_every_category_gets_a_different_hue(self):
        from curator.render import _accent_hues

        hues = _accent_hues(["ai", "crypto", "quantum", "energy", "space", "bio"])
        assert len(set(hues.values())) == 6

    def test_the_hues_are_spread_around_the_wheel(self):
        from curator.render import _accent_hues

        values = sorted(_accent_hues([f"c{n}" for n in range(6)]).values())
        gaps = [b - a for a, b in zip(values, values[1:])]
        assert min(gaps) >= 50, f"two categories sit too close together: {values}"

    def test_a_single_category_still_gets_one(self):
        from curator.render import _accent_hues

        assert _accent_hues(["only"]) == {"only": 212}

    def test_no_categories_is_not_a_division_by_zero(self):
        from curator.render import _accent_hues

        assert _accent_hues([]) == {}


class TestCardAnatomy:
    def test_the_description_is_the_sources_own_summary(self, now):
        item = make_item("A story", description="The publisher's own sentence.")
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert '<p class="desc">The publisher&#x27;s own sentence.</p>' in card

    def test_a_story_without_a_summary_renders_without_one(self, now):
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert 'class="desc"' not in card

    def test_a_hostile_description_is_escaped(self, now):
        item = make_item("A story", description="<script>alert(1)</script>")
        html = render({"T": [item]}, now=now)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_quote_in_a_description_cannot_break_out(self, now):
        item = make_item("A story", description='" onmouseover="alert(1)')
        assert 'onmouseover="alert' not in render({"T": [item]}, now=now)

    def test_the_meta_foot_carries_source_age_and_echo(self, now):
        item = make_item("A story", source_name="The Verge", hours_ago=3)
        item.echo_platforms = {"a", "b"}
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert "The Verge" in card and "3h ago" in card and "2 sources" in card


class TestUnfold:
    """Click-to-unfold, and the keyboard path that has to work too."""

    def test_the_detail_starts_closed(self, now):
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert re.search(r'<div class="detail" id="d0" hidden>', card)

    def test_the_toggle_is_a_real_button_wired_to_the_detail(self, now):
        # A div with a click handler is not keyboard-reachable. A button is,
        # and it gets Enter and Space for free.
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert '<button type="button" class="chev" aria-expanded="false" aria-controls="d0"' in card

    def test_the_unfolded_detail_carries_what_we_actually_know(self, now):
        item = make_item("A story", source_name="The Verge", hours_ago=3,
                         description="Full summary text.")
        item.matched_keywords = ["chips", "AI"]
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert '<p class="full">Full summary text.</p>' in card
        assert "<b>Source</b><span>The Verge</span>" in card
        assert "<b>Published</b>" in card and "2026" in card
        assert "<b>Matched on</b><span>chips, AI</span>" in card
        assert "Read at source" in card

    def test_an_estimated_time_is_labelled_in_the_detail_too(self, now):
        item = make_item("A story", hours_ago=3)
        item.time_is_estimated = True
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert "<b>Published</b><span>Updated " in card

    def test_there_is_a_close_control(self, now):
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert '<button type="button" class="shut">Close</button>' in card

    def test_the_script_maintains_aria_expanded(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert "aria-expanded','true'" in html and "aria-expanded','false'" in html

    def test_one_card_open_at_a_time(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert "index.forEach(function(e){if(e.el!==card){collapse(e.el);}});" in html

    def test_a_click_on_the_outbound_link_does_not_unfold(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert "if(t.closest('a')) return;" in html


class TestClusterLinks:
    def test_merged_away_outlets_are_named_and_linked(self, now):
        item = make_item("A story")
        item.cluster = [{"source_name": "The Register", "url": "https://theregister.com/x"}]
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert "<b>Also covered by</b>" in card
        assert '<a href="https://theregister.com/x" rel="noopener noreferrer nofollow">The Register</a>' in card

    def test_an_unsafe_cluster_url_never_becomes_a_link(self, now):
        # The deduper collected these from sources we do not control, so the
        # output boundary revalidates them like everything else.
        item = make_item("A story")
        item.cluster = [{"source_name": "Evil", "url": "javascript:alert(1)"}]
        html = render({"T": [item]}, now=now)
        assert "javascript:" not in html.lower()
        assert "Also covered by" not in html

    def test_a_nameless_cluster_entry_falls_back_to_its_host(self, now):
        item = make_item("A story")
        item.cluster = [{"source_name": "", "url": "https://theregister.com/x"}]
        card = card_with(render({"T": [item]}, now=now), "A story")
        assert ">theregister.com</a>" in card

    def test_no_cluster_means_no_row(self, now):
        card = card_with(render({"T": [make_item("A story")]}, now=now), "A story")
        assert "Also covered by" not in card


class TestSearch:
    """Client-side, over the cards already on the page. No backend."""

    def test_the_search_box_is_present_and_labelled(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert '<input class="q" id="q" type="search"' in html
        assert 'aria-label="Search these stories"' in html

    def test_there_is_a_live_count_and_an_empty_state(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert 'id="count"' in html and 'aria-live="polite"' in html
        assert 'class="empty" id="empty" hidden>' in html

    def test_the_empty_state_shows_when_there_is_nothing_to_show(self, now):
        html = render({"T": []}, now=now)
        assert 'class="empty" id="empty">Nothing matched' in html

    def test_the_filter_reads_title_and_description(self, now):
        html = render({"T": [make_item("a")]}, now=now)
        assert "card.querySelector('.hl')" in html and "card.querySelector('.desc')" in html
        assert "e.text.indexOf(q)>=0" in html


class TestCategoryTabs:
    def test_every_category_gets_a_chip(self, now):
        html = render({"AI": [make_item("a", "https://e.com/1")],
                       "Crypto": [make_item("b", "https://e.com/2")]}, now=now)
        assert '<button class="chip" data-filter="__all__"' in html
        assert 'data-filter="ai">AI</button>' in html
        assert 'data-filter="crypto">Crypto</button>' in html

    def test_switching_a_tab_reorders_by_that_tabs_rank(self, now):
        html = render({"AI": [make_item("a")]}, now=now)
        assert "'data-rank-'+tab" in html and "e.el.style.order=" in html

    def test_a_topic_name_with_markup_cannot_escape_the_chip(self, now):
        html = render({"<b>T</b>": [make_item("a")]}, now=now)
        assert "<b>T</b>" not in html


class TestNewsletterCards:
    """The lane's data arrives later. Its rendering rules are enforced now.

    Every one of these is a privacy rule from the design doc, not a style
    choice, which is why they are tested before there is anything to fetch.
    """

    def test_a_newsletter_card_never_loads_an_image(self, now):
        item = make_newsletter_item("A newsletter story", "https://example.com/story")
        item.image_url = "https://cdn.example/a.jpg"  # even if one somehow arrived
        card = card_with(render({"T": [item]}, now=now), "A newsletter story")
        assert "<img" not in card
        assert "data-image" not in card
        assert 'class="fb"' in card

    def test_it_is_credited_to_the_sender(self, now):
        item = make_newsletter_item("A newsletter story", "https://example.com/story",
                                    sender="Import AI")
        card = card_with(render({"T": [item]}, now=now), "A newsletter story")
        assert "via Import AI" in card
        assert "<b>Newsletter</b><span>Import AI</span>" in card

    def test_no_clean_link_means_no_link_anywhere_on_the_card(self, now):
        # The sanitizer could not strip a subscriber identifier, so it handed
        # back nothing. Publishing the identifier is the failure this prevents;
        # dropping the story is not required and would be worse.
        item = make_newsletter_item("Unlinkable story", "")
        card = card_with(render({"T": [item]}, now=now), "Unlinkable story")
        assert "href" not in card
        assert '<span class="head">Unlinkable story</span>' in card
        assert "Read at source" not in card
        assert "<b>No link</b>" in card

    def test_a_linkable_newsletter_story_still_links(self, now):
        item = make_newsletter_item("Linkable story", "https://publisher.com/story")
        card = card_with(render({"T": [item]}, now=now), "Linkable story")
        assert '<a class="head" href="https://publisher.com/story"' in card

    def test_a_newsletter_canonical_key_does_not_break_uniqueness(self, now):
        one = make_newsletter_item("Same story", "")
        two = make_newsletter_item("Same story", "")
        html = render({"AI": [one], "Crypto": [two]}, now=now)
        assert len(cards(html)) == 1

    def test_an_ordinary_item_with_no_usable_link_is_still_dropped(self, now):
        # The unlinked path is a newsletter concession, not a general one.
        bad = make_item("Ordinary broken row")
        bad.url = ""
        assert "Ordinary broken row" not in render({"T": [bad]}, now=now)


class TestEditTopicsUrl:
    def test_builds_the_github_editor_path(self):
        from curator.render import edit_topics_url

        assert edit_topics_url("https://github.com/a/b") == "https://github.com/a/b/edit/main/topics.yaml"

    def test_rejects_other_hosts(self):
        from curator.render import edit_topics_url

        assert edit_topics_url("https://gitlab.com/a/b") is None

    def test_rejects_a_lookalike_host(self):
        from curator.render import edit_topics_url

        assert edit_topics_url("https://github.com.evil.example/a/b") is None

    def test_rejects_none(self):
        from curator.render import edit_topics_url

        assert edit_topics_url(None) is None
