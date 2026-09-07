from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://project-ref.supabase.co"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


def _story(index: int) -> dict[str, object]:
    story_id = "story:" + f"{index:064x}"
    return {
        "story_id": story_id,
        "canonical_url": f"https://publisher.example/story-{index}",
        "title": f"Controller story {index}",
        "summary": "A server supplied summary.",
        "language": "en",
        "published_at": "2026-09-07T12:00:00Z",
        "publication_seq": 7 if index < 900 else 8,
        "position": index,
        "page_order_mode": "edition_rank",
        "next_cursor": {"after_position": index, "after_story_id": story_id},
        "ordering_mode": "weighted_total",
        "ordering_key": {"weighted_total": 1},
        "score_components": {"freshness": 1},
        "topic_ids": ["quantum"],
        "topic_ranks": {"quantum": index},
        "source_kind": "outlet",
        "source_name": "Publisher",
        "ranking_explanation": "Weighted using freshness.",
        "coverage_mentions": [],
        "read_at": None,
        "saved_at": None,
        "state_revision": 0,
        "interests": [],
    }


def _update(index: int) -> dict[str, object]:
    story_id = "story:" + f"{1000 + index:064x}"
    return {
        "publication_seq": 8,
        "story_id": story_id,
        "title": f"Update {index}",
        "published_at": "2026-09-07T13:00:00Z",
        "topic_ids": ["quantum"],
        "next_cursor": {
            "after_publication_seq": 8,
            "after_published_at": "2026-09-07T13:00:00Z",
            "after_story_id": story_id,
        },
    }


def _visible_story_ids(page: object) -> list[str]:
    return page.locator("article.card:visible").evaluate_all(
        "cards => cards.map(card => card.dataset.storyId)"
    )


def test_state_actions_preserve_dom_and_update_requires_explicit_refresh(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    (site / "index.html").write_text(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        body{margin:0}.tools{height:80px}.grid{display:block}.card{height:180px;margin:8px}
        .story-detail[hidden],.topic-section[hidden],.card[hidden],#updates-status[hidden]{display:none}
        .spacer{height:1200px}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <div class="tools">
          <button class="chip" data-filter="__all__">All</button>
          <button class="chip" data-filter="quantum-computing" data-topic-id="quantum">Quantum Computing</button>
        </div>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="quantum-computing" data-topic-id="quantum">
          <h2>Quantum Computing</h2><div class="grid"></div>
        </section></main><div class="spacer"></div>
        <script>
        window.__tab = "__all__";
        window.__poll = null;
        window.setInterval = callback => { window.__poll = callback; return 1; };
        window.NewsCuratorAuth = {
          config: () => ({url: "https://project-ref.supabase.co", key: "public-key"}),
          hasSessionCandidate: () => true,
          sessionForRequest: async () => ({access_token: "reader-token"}),
          channelName: "news-curator-auth"
        };
        window.NewsCuratorView = {
          currentTab: () => window.__tab,
          addCard: () => {},
          apply: () => {
            document.querySelectorAll("article.card").forEach(card => {
              card.hidden = window.__tab !== "__all__" &&
                !(card.dataset.topicIds || "").split(" ").includes(window.__tab);
            });
          }
        };
        document.querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {
          window.__tab = chip.dataset.filter;
          window.NewsCuratorView.apply();
        }));
        window.BroadcastChannel = undefined;
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    counts = {"latest": 0, "category": 0, "state": 0, "interest": 0, "updates": 0}

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            counts["latest"] += 1
            sequence = 7 if counts["latest"] < 2 else 8
            payload: object = {
                "publication_seq": sequence,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "quantum", "name": "Quantum Computing"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
            }
        elif request.url.endswith("/feed_page"):
            if counts["latest"] >= 3:
                payload = [_story(900)]
            elif body["p_topic_id"] is None:
                payload = [_story(index) for index in range(1, 21)]
            else:
                assert body["p_topic_id"] == "quantum"
                counts["category"] += 1
                payload = (
                    [_story(index) for index in range(1, 21)]
                    if counts["category"] == 1
                    else [_story(21)]
                )
        elif request.url.endswith("/set_story_state"):
            counts["state"] += 1
            payload = {
                "status": "updated",
                "read_at": "2026-09-07T12:01:00Z" if body["p_read"] else None,
                "saved_at": "2026-09-07T12:02:00Z" if body["p_saved"] else None,
                "revision": counts["state"],
            }
        elif request.url.endswith("/set_story_interest"):
            assert body["p_topic_id"] == "quantum"
            counts["interest"] += 1
            payload = {"status": "updated", "signal": "more_like", "revision": 1}
        elif request.url.endswith("/updates_since"):
            counts["updates"] += 1
            payload = [_update(1)]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 900, "height": 700})
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            page.locator("article.card", has_text="Controller story 20").wait_for()
            page.locator('.chip[data-filter="quantum-computing"]').click()

            first = page.locator("article.card").first
            first.locator(".accordion-toggle").evaluate(
                "button => { button.setAttribute('aria-expanded', 'true'); button.click(); }"
            )
            page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            page.evaluate("window.scrollTo(0, 240)")

            async_actions = [
                (".save-action", "card => card.classList.contains('is-saved')"),
                (".interest-action", "card => card.classList.contains('is-more-like')"),
                (".read-action", "card => !card.classList.contains('is-read')"),
            ]
            for selector, completed in async_actions:
                before_ids = _visible_story_ids(page)
                before_scroll = page.evaluate("window.scrollY")
                first.locator(selector).evaluate("button => button.click()")
                first.wait_for(state="attached")
                page.wait_for_function(completed, arg=first.element_handle())
                assert _visible_story_ids(page) == before_ids
                assert page.evaluate("window.scrollY") == before_scroll

            before_page = _visible_story_ids(page)
            page.locator("#load-more").evaluate("button => button.click()")
            page.locator("#reader-status").get_by_text("1 older story loaded.").wait_for()
            after_page = _visible_story_ids(page)
            assert after_page[: len(before_page)] == before_page
            assert len(after_page) == len(set(after_page)) == len(before_page) + 1

            before_poll = _visible_story_ids(page)
            page.evaluate("window.__poll()")
            page.locator("#show-updates").get_by_text("1 new story available").wait_for()
            assert _visible_story_ids(page) == before_poll
            assert page.locator('article.card[data-story-id="story:' + f"{900:064x}" + '"]').count() == 0

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("#show-updates").evaluate("button => button.click()")
            page.locator('article.card[data-story-id="story:' + f"{900:064x}" + '"]').wait_for()
            assert counts["latest"] == 3
            assert counts["updates"] == 1
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
