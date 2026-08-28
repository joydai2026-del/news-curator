"""Preview images: the parser, the cache, and the enrichment pass.

Entirely offline. Every fixture below is markup or a fake transport, because a
test that needs the internet is a test that goes red when a publisher has a bad
morning.
"""

from __future__ import annotations

import json

import pytest
from datetime import datetime, timedelta, timezone

from curator.fetchers.rss import entry_image
from curator.images import ImageCache, enrich, is_public_host, parse_image_meta
from tests.conftest import make_item

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def page(head: str) -> str:
    return f"<!doctype html><html><head><title>t</title>{head}</head><body><p>x</p></body></html>"


class TestParseImageMeta:
    def test_reads_og_image(self):
        markup = page('<meta property="og:image" content="https://cdn.example/a.jpg">')
        assert parse_image_meta(markup) == "https://cdn.example/a.jpg"

    def test_accepts_name_as_well_as_property(self):
        # Publishers disagree about which attribute og: tags belong in, and both
        # appear in the wild.
        markup = page('<meta name="og:image" content="https://cdn.example/a.jpg">')
        assert parse_image_meta(markup) == "https://cdn.example/a.jpg"

    def test_falls_back_to_twitter_image(self):
        markup = page('<meta name="twitter:image" content="https://cdn.example/t.jpg">')
        assert parse_image_meta(markup) == "https://cdn.example/t.jpg"

    def test_og_image_wins_over_twitter_image(self):
        markup = page(
            '<meta name="twitter:image" content="https://cdn.example/t.jpg">'
            '<meta property="og:image" content="https://cdn.example/o.jpg">'
        )
        assert parse_image_meta(markup) == "https://cdn.example/o.jpg"

    def test_first_og_image_wins_when_several_are_declared(self):
        markup = page(
            '<meta property="og:image" content="https://cdn.example/1.jpg">'
            '<meta property="og:image" content="https://cdn.example/2.jpg">'
        )
        assert parse_image_meta(markup) == "https://cdn.example/1.jpg"

    def test_relative_urls_resolve_against_the_article(self):
        markup = page('<meta property="og:image" content="/img/a.jpg">')
        got = parse_image_meta(markup, "https://news.example/story/1")
        assert got == "https://news.example/img/a.jpg"

    def test_protocol_relative_urls_resolve(self):
        markup = page('<meta property="og:image" content="//cdn.example/a.jpg">')
        assert parse_image_meta(markup, "https://news.example/x") == "https://cdn.example/a.jpg"

    def test_a_javascript_url_is_refused(self):
        # A publisher's markup does not get to decide what ends up in a src on
        # a public page. Same allow-list as every other link.
        markup = page("<meta property=\"og:image\" content=\"javascript:alert(1)\">")
        assert parse_image_meta(markup) is None

    def test_a_data_url_is_refused(self):
        markup = page('<meta property="og:image" content="data:image/png;base64,AAAA">')
        assert parse_image_meta(markup) is None

    def test_no_meta_tags_is_a_clean_miss(self):
        assert parse_image_meta(page("")) is None

    def test_empty_content_is_a_miss(self):
        assert parse_image_meta(page('<meta property="og:image" content="">')) is None

    def test_entities_in_the_url_are_decoded(self):
        markup = page('<meta property="og:image" content="https://cdn.example/a.jpg?w=1&amp;h=2">')
        assert parse_image_meta(markup) == "https://cdn.example/a.jpg?w=1&h=2"

    def test_body_images_are_ignored(self):
        # Only the head declares a preview image. An <img> in the article is a
        # picture in the article, which is a different thing.
        markup = (
            "<html><head></head><body>"
            '<meta property="og:image" content="https://cdn.example/late.jpg">'
            "</body></html>"
        )
        assert parse_image_meta(markup) is None

    def test_self_closing_xhtml_meta_is_read(self):
        markup = page('<meta property="og:image" content="https://cdn.example/x.jpg" />')
        assert parse_image_meta(markup) == "https://cdn.example/x.jpg"

    def test_single_quoted_attributes_are_read(self):
        markup = page("<meta property='og:image' content='https://cdn.example/s.jpg'>")
        assert parse_image_meta(markup) == "https://cdn.example/s.jpg"

    def test_uppercase_attribute_names_are_read(self):
        markup = page('<META PROPERTY="OG:IMAGE" CONTENT="https://cdn.example/u.jpg">')
        assert parse_image_meta(markup) == "https://cdn.example/u.jpg"

    def test_malformed_markup_does_not_raise(self):
        assert parse_image_meta("<html><head><meta property=og:image content=") is None

    def test_empty_input(self):
        assert parse_image_meta("") is None


class TestEntryImage:
    """Feed-declared images, which cost no extra request at all."""

    def test_media_content(self):
        entry = {"media_content": [{"url": "https://cdn.example/a.jpg", "medium": "image"}]}
        assert entry_image(entry) == "https://cdn.example/a.jpg"

    def test_media_thumbnail(self):
        entry = {"media_thumbnail": [{"url": "https://cdn.example/t.jpg"}]}
        assert entry_image(entry) == "https://cdn.example/t.jpg"

    def test_image_enclosure(self):
        entry = {"links": [{"rel": "enclosure", "type": "image/jpeg", "href": "https://cdn.example/e.jpg"}]}
        assert entry_image(entry) == "https://cdn.example/e.jpg"

    def test_a_video_enclosure_is_not_an_image(self):
        entry = {"links": [{"rel": "enclosure", "type": "video/mp4", "href": "https://cdn.example/v.mp4"}]}
        assert entry_image(entry) == ""

    def test_media_content_that_says_it_is_audio_is_skipped(self):
        # media:content carries podcasts too. Taking it blindly puts an audio
        # file in an image slot.
        entry = {"media_content": [{"url": "https://cdn.example/a.mp3", "medium": "audio"}]}
        assert entry_image(entry) == ""

    def test_untyped_media_content_is_accepted(self):
        # The common sloppy case: no medium, no type. Real feeds do this.
        entry = {"media_content": [{"url": "https://cdn.example/a.jpg"}]}
        assert entry_image(entry) == "https://cdn.example/a.jpg"

    def test_an_unsafe_url_is_refused(self):
        entry = {"media_content": [{"url": "javascript:alert(1)", "medium": "image"}]}
        assert entry_image(entry) == ""

    def test_nothing_declared(self):
        assert entry_image({}) == ""


class TestImageCache:
    def test_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "image_cache.json"
        cache = ImageCache(path)
        cache.put("https://e.example/a", "https://cdn.example/a.jpg", "ok", NOW)
        assert cache.save() is True

        reloaded = ImageCache.load(path)
        hit, image = reloaded.get("https://e.example/a", NOW, retry_error_after_hours=24)
        assert hit and image == "https://cdn.example/a.jpg"

    def test_a_clean_no_image_answer_is_a_hit(self):
        # "This page declares no image" is a real answer and must stop us asking
        # again every hour for the rest of the article's life.
        cache = ImageCache(None)
        cache.put("k", None, "none", NOW)
        hit, image = cache.get("k", NOW, retry_error_after_hours=24)
        assert hit is True and image == ""

    def test_an_error_is_retried_once_it_has_aged(self):
        cache = ImageCache(None)
        cache.put("k", None, "error", NOW)
        assert cache.get("k", NOW, retry_error_after_hours=24)[0] is True
        later = NOW + timedelta(hours=25)
        assert cache.get("k", later, retry_error_after_hours=24)[0] is False

    def test_a_clean_no_image_answer_is_never_retried(self):
        cache = ImageCache(None)
        cache.put("k", None, "none", NOW)
        assert cache.get("k", NOW + timedelta(days=10), retry_error_after_hours=24)[0] is True

    def test_a_miss_is_a_miss(self):
        assert ImageCache(None).get("nope", NOW, retry_error_after_hours=24)[0] is False

    def test_save_is_a_no_op_when_nothing_changed(self, tmp_path):
        # An hourly job that rewrites an unchanged file makes an empty commit
        # every hour forever.
        path = tmp_path / "c.json"
        cache = ImageCache(path)
        cache.put("k", None, "none", NOW)
        cache.save()
        assert ImageCache.load(path).save() is False

    def test_pruning_drops_links_no_longer_in_circulation(self):
        cache = ImageCache(None)
        cache.put("old", "https://cdn.example/o.jpg", "ok", NOW - timedelta(days=90))
        cache.put("new", "https://cdn.example/n.jpg", "ok", NOW)
        assert cache.prune(NOW, retain_days=45) == 1
        assert set(cache.entries) == {"new"}

    def test_touching_a_link_spares_it_from_pruning(self):
        cache = ImageCache(None)
        cache.put("old", "https://cdn.example/o.jpg", "ok", NOW - timedelta(days=90))
        cache.touch("old", NOW)
        assert cache.prune(NOW, retain_days=45) == 0

    def test_a_corrupt_cache_file_starts_empty_rather_than_crashing(self, tmp_path):
        # A cache is a performance optimization. It must never be able to fail
        # the build.
        path = tmp_path / "c.json"
        path.write_text("{not json", encoding="utf-8")
        assert ImageCache.load(path).entries == {}

    def test_a_future_cache_version_is_discarded(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"version": 99, "entries": {"k": {}}}), encoding="utf-8")
        assert ImageCache.load(path).entries == {}

    def test_a_missing_file_starts_empty(self, tmp_path):
        assert ImageCache.load(tmp_path / "absent.json").entries == {}


class TestEnrich:
    """The pass that decides what to ask for, and what never to ask about."""

    def test_a_feed_supplied_image_is_never_fetched(self):
        item = make_item("A story", "https://e.example/a")
        item.image_url = "https://cdn.example/feed.jpg"
        stats = enrich([item], ImageCache(None), NOW, user_agent="t")
        assert stats["from_feed"] == 1 and stats["fetched"] == 0
        assert item.image_url == "https://cdn.example/feed.jpg"

    def test_a_cached_image_is_applied_without_a_fetch(self):
        item = make_item("A story", "https://e.example/a")
        cache = ImageCache(None)
        cache.put(item.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        stats = enrich([item], cache, NOW, user_agent="t")
        assert stats["from_cache"] == 1 and stats["fetched"] == 0
        assert item.image_url == "https://cdn.example/c.jpg"

    def test_disabled_means_no_network_and_no_invention(self):
        item = make_item("A story", "https://e.example/a")
        enrich([item], ImageCache(None), NOW, user_agent="t", config={"enabled": False})
        assert item.image_url == ""

    def test_the_same_story_in_two_categories_is_one_question(self):
        # assign_categories clones an item per category. Asking the publisher
        # twice about one article is rude and pointless.
        cache = ImageCache(None)
        a = make_item("A story", "https://e.example/a")
        b = make_item("A story", "https://e.example/a")
        cache.put(a.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        stats = enrich([a, b], cache, NOW, user_agent="t")
        assert stats["from_cache"] == 1
        assert a.image_url == b.image_url == "https://cdn.example/c.jpg"

    def test_every_seen_link_is_touched_so_pruning_spares_it(self):
        cache = ImageCache(None)
        item = make_item("A story", "https://e.example/a")
        cache.put(item.canonical_url, None, "none", NOW - timedelta(days=90))
        enrich([item], cache, NOW, user_agent="t", config={"enabled": False})
        assert cache.prune(NOW, retain_days=45) == 0

    def test_no_items_is_not_an_error(self):
        assert enrich([], ImageCache(None), NOW, user_agent="t")["total"] == 0


class TestNewsletterItemsAreNeverLookedUp:
    """A newsletter URL can carry a subscriber identifier. Two consequences.

    Requesting one tells the sender which subscriber's mail was processed and
    when. Writing one into `image_cache.json` publishes it, permanently, in a
    public repository. The pipeline is also expected to skip these, and that is
    exactly why the guard is repeated here: a privacy rule that lives in only
    one layer is one refactor away from being gone.
    """

    def test_a_newsletter_item_is_never_fetched(self):
        item = make_item("A newsletter story", "https://sender.example/r/abc123")
        item.is_newsletter = True
        stats = enrich([item], ImageCache(None), NOW, user_agent="t")
        assert stats["newsletter_skipped"] == 1
        assert stats["fetched"] == 0 and stats["from_cache"] == 0
        assert item.image_url == ""

    def test_a_newsletter_item_never_reaches_the_cache_at_all(self):
        cache = ImageCache(None)
        item = make_item("A newsletter story", "https://sender.example/r/abc123")
        item.is_newsletter = True
        enrich([item], cache, NOW, user_agent="t")
        assert cache.entries == {}

    def test_a_cached_image_is_not_even_applied_to_a_newsletter_item(self):
        # Not merely "we do not ask again": we do not attach one either. The
        # card renders its typographic panel, which is the design.
        cache = ImageCache(None)
        item = make_item("A newsletter story", "https://sender.example/r/abc123")
        item.is_newsletter = True
        cache.put(item.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        enrich([item], cache, NOW, user_agent="t")
        assert item.image_url == ""

    def test_ordinary_items_alongside_one_are_unaffected(self):
        cache = ImageCache(None)
        ordinary = make_item("A story", "https://e.example/a")
        cache.put(ordinary.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        letter = make_item("A newsletter story", "https://sender.example/r/abc123")
        letter.is_newsletter = True
        stats = enrich([letter, ordinary], cache, NOW, user_agent="t")
        assert stats["total"] == 2 and stats["newsletter_skipped"] == 1
        assert ordinary.image_url == "https://cdn.example/c.jpg"


class TestPublicHostGuard:
    """v1 never fetched a destination page, so this is new attack surface.

    A feed we do not control now supplies addresses a CI runner will request.
    The value of blocking is not the request, it is that an internal page's
    meta tag can never be parsed onto a public page.
    """

    @pytest.mark.allow_socket
    def test_ordinary_public_hosts_pass(self):
        # The one place a real lookup is the point. Everything else in the suite
        # is blocked from the network by the autouse fixture in conftest.
        assert is_public_host("https://techcrunch.com/a")

    def test_a_public_ip_literal_passes_without_resolving(self):
        assert is_public_host("https://1.1.1.1/a")

    def test_loopback_is_refused(self):
        assert not is_public_host("http://127.0.0.1:8080/a")
        assert not is_public_host("http://localhost/a")
        assert not is_public_host("http://LOCALHOST:9000/a")

    def test_cloud_metadata_endpoint_is_refused(self):
        # The one that actually matters on a CI runner.
        assert not is_public_host("http://169.254.169.254/latest/meta-data/")

    def test_private_ranges_are_refused(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.5", "0.0.0.0"):
            assert not is_public_host(f"http://{host}/a"), host

    def test_ipv6_loopback_and_link_local_are_refused(self):
        assert not is_public_host("http://[::1]/a")
        assert not is_public_host("http://[fe80::1]/a")

    def test_a_name_that_resolves_to_a_private_address_is_refused(self, monkeypatch):
        # The whole reason a string check is not enough: `evil.example` pointing
        # at the metadata endpoint passes any literal-address test.
        import socket as s
        from curator import images

        monkeypatch.setattr(
            images.socket, "getaddrinfo",
            lambda *a, **k: [(s.AF_INET, s.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
        )
        assert not is_public_host("https://evil.example/a")

    def test_a_name_resolving_to_a_public_address_is_allowed(self, monkeypatch):
        import socket as s
        from curator import images

        monkeypatch.setattr(
            images.socket, "getaddrinfo",
            lambda *a, **k: [(s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )
        assert is_public_host("https://example.com/a")

    def test_a_name_with_one_private_answer_is_refused(self, monkeypatch):
        # Mixed results must fail closed: one private answer is enough.
        import socket as s
        from curator import images

        monkeypatch.setattr(
            images.socket, "getaddrinfo",
            lambda *a, **k: [
                (s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (s.AF_INET, s.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ],
        )
        assert not is_public_host("https://mixed.example/a")

    def test_a_name_that_does_not_resolve_is_refused(self, monkeypatch):
        # A name we cannot resolve is a name we cannot vouch for.
        import socket as s
        from curator import images

        def boom(*a, **k):
            raise s.gaierror("nope")

        monkeypatch.setattr(images.socket, "getaddrinfo", boom)
        assert not is_public_host("https://nxdomain.example/a")

    def test_no_host_is_refused(self):
        assert not is_public_host("")
        assert not is_public_host("not a url")


class TestOfflineStillUsesTheCache:
    def test_a_disabled_run_still_applies_a_cached_image(self):
        # `--offline` means no network, not "pretend we never learned anything".
        # Reading the cache touches no wire, so an offline render keeps every
        # picture it already knows about.
        cache = ImageCache(None)
        item = make_item("A story", "https://e.example/a")
        cache.put(item.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        stats = enrich([item], cache, NOW, user_agent="t", config={"enabled": False})
        assert item.image_url == "https://cdn.example/c.jpg"
        assert stats["from_cache"] == 1 and stats["fetched"] == 0


class TestCacheIsRevalidatedOnRead:
    """image_cache.json is committed, so it is editable. Do not trust it blindly."""

    def test_an_unsafe_cached_value_is_refused(self):
        cache = ImageCache(None)
        cache.entries["k"] = {"image": "javascript:alert(1)", "outcome": "ok",
                              "checked_at": NOW.isoformat(), "seen_at": NOW.isoformat()}
        hit, image = cache.get("k", NOW, retry_error_after_hours=24)
        assert hit is True and image == ""

    def test_a_non_string_cached_value_does_not_crash(self):
        cache = ImageCache(None)
        cache.entries["k"] = {"image": {"nested": "junk"}, "outcome": "ok",
                              "checked_at": NOW.isoformat(), "seen_at": NOW.isoformat()}
        assert cache.get("k", NOW, retry_error_after_hours=24) == (True, "")

    def test_a_hand_edited_unsafe_entry_never_reaches_an_item(self):
        cache = ImageCache(None)
        item = make_item("A story", "https://e.example/a")
        cache.entries[item.canonical_url] = {"image": "javascript:alert(1)", "outcome": "ok",
                                             "checked_at": NOW.isoformat(), "seen_at": NOW.isoformat()}
        enrich([item], cache, NOW, user_agent="t", config={"enabled": False})
        assert item.image_url == ""


class TestNoFetchIsProvenNotInferred:
    """These assert the transport was never reached, not just that counters agree.

    The earlier versions checked `stats["fetched"] == 0` and a final field, which
    a broken implementation that fetched and then discarded the result would
    also satisfy. Monkeypatching the fetch is what actually proves it.
    """

    def _explode(self, monkeypatch):
        from curator import images

        def boom(*a, **k):
            raise AssertionError("fetch_image_meta was called and should not have been")

        monkeypatch.setattr(images, "fetch_image_meta", boom)

    def test_a_feed_supplied_image_never_reaches_the_transport(self, monkeypatch):
        self._explode(monkeypatch)
        item = make_item("A story", "https://e.example/a")
        item.image_url = "https://cdn.example/feed.jpg"
        enrich([item], ImageCache(None), NOW, user_agent="t")
        assert item.image_url == "https://cdn.example/feed.jpg"

    def test_a_cache_hit_never_reaches_the_transport(self, monkeypatch):
        self._explode(monkeypatch)
        cache = ImageCache(None)
        item = make_item("A story", "https://e.example/a")
        cache.put(item.canonical_url, "https://cdn.example/c.jpg", "ok", NOW)
        enrich([item], cache, NOW, user_agent="t")
        assert item.image_url == "https://cdn.example/c.jpg"

    def test_disabled_never_reaches_the_transport(self, monkeypatch):
        self._explode(monkeypatch)
        item = make_item("A story", "https://e.example/a")
        enrich([item], ImageCache(None), NOW, user_agent="t", config={"enabled": False})
        assert item.image_url == ""

    def test_one_article_in_two_categories_is_asked_about_once(self, monkeypatch):
        from curator import images

        calls = []

        def once(url, **kwargs):
            calls.append(url)
            return "https://cdn.example/x.jpg", "ok"

        monkeypatch.setattr(images, "fetch_image_meta", once)
        a = make_item("A story", "https://e.example/a")
        b = make_item("A story", "https://e.example/a")  # the clone assign_categories makes
        enrich([a, b], ImageCache(None), NOW, user_agent="t")
        assert calls == ["https://e.example/a"]
        assert a.image_url == b.image_url == "https://cdn.example/x.jpg"

    def test_a_definitive_miss_and_a_refusal_are_cached_differently(self, monkeypatch):
        from curator import images

        cache = ImageCache(None)
        monkeypatch.setattr(images, "fetch_image_meta", lambda url, **k: (None, "error"))
        item = make_item("A story", "https://e.example/a")
        enrich([item], cache, NOW, user_agent="t")
        # Non-definitive: retried once it has aged.
        assert cache.get(item.canonical_url, NOW + timedelta(hours=25),
                         retry_error_after_hours=24)[0] is False

        monkeypatch.setattr(images, "fetch_image_meta", lambda url, **k: (None, "none"))
        other = make_item("B story", "https://e.example/b")
        enrich([other], cache, NOW, user_agent="t")
        # Definitive: never retried.
        assert cache.get(other.canonical_url, NOW + timedelta(days=10),
                         retry_error_after_hours=24)[0] is True


class _FakeResponse:
    """Minimal stand-in for a streamed requests response."""

    def __init__(self, status=200, headers=None, chunks=(), url="https://pub.example/a"):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html"}
        self._chunks = list(chunks)
        self.url = url
        self.encoding = "utf-8"
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    is_permanent_redirect = False

    def iter_content(self, size):
        yield from self._chunks

    def close(self):
        self.closed = True


class TestFetchImageMetaTransport:
    """The bounds and the redirect policy, with a fake transport."""

    def _public(self, monkeypatch):
        from curator import images

        monkeypatch.setattr(images, "is_public_host", lambda url: "private" not in url)

    def _run(self, monkeypatch, responses):
        from curator import images

        seen = []

        class FakeSession:
            def get(self, url, **kwargs):
                seen.append(url)
                assert kwargs.get("allow_redirects") is False, "redirects must be followed manually"
                return responses.pop(0)

        result = images.fetch_image_meta(
            "https://pub.example/a", user_agent="t", timeout=5,
            max_bytes=65536, session=FakeSession(),
        )
        return result, seen

    def test_a_redirect_is_followed_manually_and_rechecked(self, monkeypatch):
        self._public(monkeypatch)
        html = b'<html><head><meta property="og:image" content="https://cdn.example/z.jpg"></head><body>'
        (image, outcome), seen = self._run(monkeypatch, [
            _FakeResponse(302, {"Location": "https://pub.example/final"}),
            _FakeResponse(200, {"Content-Type": "text/html"}, [html], url="https://pub.example/final"),
        ])
        assert outcome == "ok" and image == "https://cdn.example/z.jpg"
        assert seen == ["https://pub.example/a", "https://pub.example/final"]

    def test_a_redirect_into_a_private_host_is_never_requested(self, monkeypatch):
        # The point of manual redirects: refuse to REQUEST it, not merely refuse
        # to parse what came back.
        self._public(monkeypatch)
        (image, outcome), seen = self._run(monkeypatch, [
            _FakeResponse(302, {"Location": "https://private.internal/meta"}),
        ])
        assert image is None and outcome == "error"
        assert seen == ["https://pub.example/a"]  # the second hop never happened

    def test_a_non_200_is_never_parsed(self, monkeypatch):
        # Measured: real publishers return a styled block page carrying its own
        # og:image. Parsing it attaches "you are blocked" artwork to a story.
        self._public(monkeypatch)
        blocked = b'<html><head><meta property="og:image" content="https://cdn.example/blocked.jpg"></head>'
        (image, outcome), _ = self._run(monkeypatch, [
            _FakeResponse(403, {"Content-Type": "text/html"}, [blocked]),
        ])
        assert image is None and outcome == "error"

    def test_truncation_before_the_head_ends_is_not_a_definitive_miss(self, monkeypatch):
        self._public(monkeypatch)
        filler = b"<html><head>" + b"<!-- pad -->" * 20000  # never reaches </head>
        (image, outcome), _ = self._run(monkeypatch, [
            _FakeResponse(200, {"Content-Type": "text/html"}, [filler]),
        ])
        assert image is None
        assert outcome == "error", "a cut-off read must not be cached as 'declares no image'"

    def test_reaching_the_end_of_the_head_with_nothing_is_definitive(self, monkeypatch):
        self._public(monkeypatch)
        (image, outcome), _ = self._run(monkeypatch, [
            _FakeResponse(200, {"Content-Type": "text/html"}, [b"<html><head><title>x</title></head><body>"]),
        ])
        assert image is None and outcome == "none"

    def test_a_non_html_content_type_is_a_definitive_miss(self, monkeypatch):
        self._public(monkeypatch)
        (image, outcome), _ = self._run(monkeypatch, [
            _FakeResponse(200, {"Content-Type": "application/pdf"}, [b"%PDF-1.4"]),
        ])
        assert image is None and outcome == "none"

    def test_the_byte_cap_is_a_ceiling_not_a_ceiling_plus_one_chunk(self, monkeypatch):
        from curator import images

        self._public(monkeypatch)
        captured = {}

        def spy(markup, base_url=""):
            captured["len"] = len(markup)
            return None

        monkeypatch.setattr(images, "parse_image_meta", spy)

        class FakeSession:
            def get(self, url, **kwargs):
                return _FakeResponse(200, {"Content-Type": "text/html"}, [b"x" * 16384] * 10)

        images.fetch_image_meta("https://pub.example/a", user_agent="t", timeout=5,
                                max_bytes=40000, session=FakeSession())
        assert captured["len"] <= 40000, "the cap must not be overshot by a whole chunk"

    def test_a_redirect_loop_terminates(self, monkeypatch):
        from curator import images

        self._public(monkeypatch)

        class LoopSession:
            def get(self, url, **kwargs):
                return _FakeResponse(302, {"Location": "https://pub.example/loop"})

        image, outcome = images.fetch_image_meta(
            "https://pub.example/a", user_agent="t", timeout=5,
            max_bytes=65536, session=LoopSession(),
        )
        assert image is None and outcome == "error"

    def test_a_non_public_initial_host_is_never_requested(self, monkeypatch):
        from curator import images

        monkeypatch.setattr(images, "is_public_host", lambda url: False)

        class NeverSession:
            def get(self, url, **kwargs):
                raise AssertionError("a non-public host must not be requested")

        image, outcome = images.fetch_image_meta(
            "http://169.254.169.254/latest/meta-data/", user_agent="t", timeout=5,
            max_bytes=65536, session=NeverSession(),
        )
        assert image is None and outcome == "error"


class TestNonCanonicalIpFormsAreRefused:
    """Every one of these is 127.0.0.1 or the metadata endpoint to a C resolver.

    None of them parses as an IP with `ipaddress`, so a literal-only check waves
    them through. `0177.0.0.1` is the sharp case: getaddrinfo answers
    177.0.0.1 (global, so it would PASS) while a client applying octal rules
    connects to 127.0.0.1. Two parsers disagreeing is a bypass, not a residual.
    """

    def test_decimal_and_octal_and_hex_forms_are_refused(self):
        for host in ("2130706433", "2852039166", "127.1", "0177.0.0.1", "0x7f.1", "127.0.0.001", "0"):
            assert not is_public_host(f"http://{host}/a"), host

    def test_a_real_domain_is_still_allowed(self, monkeypatch):
        import socket as s
        from curator import images

        monkeypatch.setattr(
            images.socket, "getaddrinfo",
            lambda *a, **k: [(s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )
        assert is_public_host("https://techcrunch.com/2026/08/28/a-story")
