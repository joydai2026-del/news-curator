from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from curator.models import TierResult
from curator.render import render_site
from tests.conftest import make_item


playwright_api = pytest.importorskip("playwright.sync_api")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


def _launch_browser(playwright: object) -> object:
    try:
        return playwright.chromium.launch(headless=True, args=["--mute-audio"])
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        return playwright.chromium.launch(
            headless=True, channel="chrome", args=["--mute-audio"]
        )


@pytest.mark.parametrize(
    ("viewport", "navigation"),
    [
        ({"width": 1440, "height": 1000}, ".rail"),
        ({"width": 1100, "height": 560}, ".rail"),
        ({"width": 390, "height": 844}, ".tools"),
    ],
    ids=["desktop", "short-desktop", "phone"],
)
def test_navigation_remains_viewport_accessible_while_scrolling(
    tmp_path: Path,
    now: object,
    viewport: dict[str, int],
    navigation: str,
) -> None:
    site = tmp_path / "site"
    topic_names = (
        "AI",
        "Crypto",
        "Quantum computing",
        "Energy and nuclear",
        "Space technology",
        "Biotechnology",
        "World",
        "US News",
        "Business",
        "Trending",
    )
    ranked = {
        topic: [
                make_item(
                    f"{topic} story {story}",
                    f"https://publisher.example/topic-{topic_names.index(topic)}-story-{story}",
                    description=(
                        "The publisher supplied a complete summary for the navigation "
                        "scrolling regression and its rendered story layout."
                    ),
                )
            for story in range(1, 5)
        ]
        for topic in topic_names
    }
    render_site(ranked, [TierResult(tier="rss", items=[], ok=True)], now, site)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(viewport=viewport)
            context.add_init_script(
                """speechSynthesis.speak=()=>{};
                HTMLMediaElement.prototype.play=()=>Promise.resolve();"""
            )
            page = context.new_page()
            page.goto(
                f"http://127.0.0.1:{server.server_port}/?silent=1",
                wait_until="networkidle",
            )
            page.locator("article.card").last.scroll_into_view_if_needed()
            page.wait_for_timeout(100)
            scroll_before = page.evaluate("window.scrollY")
            box = page.locator(navigation).bounding_box()
            assert box is not None
            assert box["y"] >= 0
            assert box["y"] + box["height"] <= viewport["height"]
            assert page.evaluate(
                "document.documentElement.scrollWidth === document.documentElement.clientWidth"
            )

            topic_nav = page.locator(
                ".mobiletopics" if viewport["width"] <= 980 else ".railnav"
            )
            visible_topics = topic_nav.locator(".chip:visible:enabled")
            visible_topics.first.focus()
            for _ in range(visible_topics.count() - 1):
                page.keyboard.press("Tab")
                page.wait_for_timeout(20)
            last_topic = visible_topics.last
            assert last_topic.evaluate("node => document.activeElement === node")
            assert last_topic.evaluate("node => node.matches(':focus-visible')")
            assert page.evaluate("window.scrollY") == scroll_before
            focused = last_topic.bounding_box()
            nav_box = topic_nav.bounding_box()
            assert focused is not None and nav_box is not None
            assert focused["x"] >= nav_box["x"]
            assert focused["x"] + focused["width"] <= nav_box["x"] + nav_box["width"]
            assert focused["y"] >= nav_box["y"]
            assert focused["y"] + focused["height"] <= nav_box["y"] + nav_box["height"]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
