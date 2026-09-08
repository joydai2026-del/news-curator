from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from curator.models import TierResult
from curator.render import JS as VIEW_JS, render_site
from tests.conftest import make_item


playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://project-ref.supabase.co"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


def _story(index: int, mode: str = "edition_rank") -> dict[str, object]:
    story_id = "story:" + f"{index:064x}"
    cursor = (
        {"after_position": index, "after_story_id": story_id}
        if mode == "edition_rank"
        else {
            "before_published_at": "2026-09-07T11:00:00Z",
            "before_story_id": story_id,
        }
    )
    return {
        "story_id": story_id,
        "canonical_url": f"https://publisher.example/story-{index}",
        "title": f"Controller story {index}",
        "summary": "A server supplied summary.",
        "language": "en",
        "published_at": "2026-09-07T12:00:00Z",
        "publication_seq": 7 if index < 900 else 8,
        "position": index,
        "page_order_mode": mode,
        "next_cursor": cursor,
        "ordering_mode": "weighted_total",
        "ordering_key": {"weighted_total": 1},
        "score_components": {"freshness": 1},
        "topic_ids": ["ai", "quantum"] if index == 1 else ["quantum"],
        "topic_ranks": {"ai": index, "quantum": index} if index == 1 else {"quantum": index},
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


def _visually_ordered_story_ids(page: object) -> list[str]:
    return page.locator("article.card:visible").evaluate_all(
        "cards => cards.map(card => ({id: card.dataset.storyId, top: card.getBoundingClientRect().top}))"
        ".sort((left, right) => left.top - right.top).map(entry => entry.id)"
    )


def test_state_actions_preserve_dom_and_update_requires_explicit_refresh(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    (site / "index.html").write_text(
        """<!doctype html><html><head><meta charset="utf-8"><style>
        body{margin:0}.tools{height:80px}.grid{display:flex;flex-direction:column}.card{height:180px;margin:8px}
        .story-detail[hidden],.topic-section[hidden],.card[hidden],#updates-status[hidden]{display:none}
        .spacer{height:1200px}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <div class="tools">
          <button class="chip" data-filter="__all__">All</button>
          <button class="chip" data-filter="__saved__">Saved</button>
          <button class="chip" data-filter="ai" data-topic-id="ai">AI</button>
          <button class="chip" data-filter="quantum-computing" data-topic-id="quantum">Quantum Computing</button>
        </div>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="quantum-computing" data-topic-id="quantum">
          <h2>Quantum Computing</h2><div class="grid">
            <article class="card" data-story-id="story:0000000000000000000000000000000000000000000000000000000000000001"
              data-topic-ids="ai quantum-computing" data-topic-api-ids="ai quantum"
              data-state-revision="0" data-interest-revision="0" data-rank-all="1">
              <button class="accordion-toggle" aria-expanded="false">Controller story 1</button>
              <button class="state-action read-action" hidden disabled>Mark read</button>
              <button class="state-action save-action" hidden disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="ai" hidden disabled>More like this</button>
            </article>
            <article class="card" data-story-id="story:0000000000000000000000000000000000000000000000000000000000000002"
              data-topic-ids="quantum-computing" data-topic-api-ids="quantum"
              data-state-revision="0" data-interest-revision="0" data-rank-all="2">
              <button class="accordion-toggle" aria-expanded="false">Controller story 2</button>
              <button class="state-action read-action" hidden disabled>Mark read</button>
              <button class="state-action save-action" hidden disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="quantum" hidden disabled>More like this</button>
            </article>
          </div>
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
              card.hidden = window.__tab === "__saved__"
                ? !card.classList.contains("is-saved")
                : window.__tab !== "__all__" &&
                  !(card.dataset.topicIds || "").split(" ").includes(window.__tab);
              const rank = card.getAttribute(window.__tab === "__all__"
                ? "data-rank-all" : `data-rank-${window.__tab}`);
              card.style.order = rank === null ? "0" : rank;
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
    counts = {"latest": 0, "all": 0, "category": 0, "state": 0, "interest": 0, "updates": 0}
    interest_writes: list[tuple[str, int]] = []
    state_writes: list[tuple[str, int]] = []
    fail_next_state = {"value": False}

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            counts["latest"] += 1
            sequence = 7 if counts["latest"] < 2 else 8
            payload: object = {
                "publication_seq": sequence,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [
                    {"topic_id": "ai", "name": "AI"},
                    {"topic_id": "quantum", "name": "Quantum Computing"},
                ],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 3,
            }
        elif request.url.endswith("/feed_page"):
            assert body["p_limit"] == 3
            if counts["latest"] >= 3:
                payload = [_story(900)]
            elif body["p_topic_id"] is None:
                counts["all"] += 1
                if counts["all"] == 1:
                    fresher = _story(10, "history_freshness")
                    fresher.update({"position": 20, "publication_seq": 6,
                                    "topic_ids": ["ai"], "topic_ranks": {"ai": 20}})
                    older = _story(11, "history_freshness")
                    older.update({"position": 1, "publication_seq": 5,
                                  "topic_ids": ["ai"], "topic_ranks": {"ai": 1}})
                    payload = [_story(1, "history_freshness"), fresher, older]
                else:
                    duplicate_position = _story(12, "history_freshness")
                    duplicate_position.update({"position": 1, "publication_seq": 4,
                                               "topic_ids": ["ai"], "topic_ranks": {"ai": 1}})
                    payload = [duplicate_position]
            elif body["p_topic_id"] == "ai":
                row = _story(1)
                row["saved_at"] = "2026-09-07T12:02:00Z"
                row["state_revision"] = counts["state"]
                row["interests"] = [
                    {"topic_id": "quantum", "signal": "more_like", "revision": 1}
                ]
                payload = [row]
            else:
                assert body["p_topic_id"] == "quantum"
                counts["category"] += 1
                if counts["category"] == 1:
                    assert body["p_order_mode"] == "edition_rank"
                    second = _story(2)
                    second["state_revision"] = 7
                    payload = [_story(1), second]
                else:
                    assert body["p_order_mode"] == "history_freshness"
                    assert body.get("p_before_published_at") is None
                    first_history = _story(1, "history_freshness")
                    first_history["saved_at"] = "2026-09-07T12:02:00Z"
                    first_history["state_revision"] = counts["state"]
                    first_history["interests"] = [
                        {"topic_id": "ai", "signal": "more_like", "revision": 1},
                        {"topic_id": "quantum", "signal": "more_like", "revision": 1},
                    ]
                    payload = [first_history, _story(2, "history_freshness"),
                               _story(3, "history_freshness")]
        elif request.url.endswith("/saved_page"):
            payload = [{
                **_story(1, "saved_at"),
                "saved_at": "2026-09-07T12:02:00Z",
                "next_cursor": {
                    "before_saved_at": "2026-09-07T12:02:00Z",
                    "before_story_id": "story:" + f"{1:064x}",
                },
            }]
        elif request.url.endswith("/set_story_state"):
            state_writes.append((body["p_story_id"], body["p_expected_revision"]))
            if fail_next_state["value"]:
                fail_next_state["value"] = False
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=json.dumps({"error": "controlled failure"}),
                )
                return
            counts["state"] += 1
            payload = {
                "status": "updated",
                "read_at": "2026-09-07T12:01:00Z" if body["p_read"] else None,
                "saved_at": "2026-09-07T12:02:00Z" if body["p_saved"] else None,
                "revision": counts["state"],
            }
        elif request.url.endswith("/set_story_interest"):
            assert body["p_topic_id"] in {"ai", "quantum"}
            counts["interest"] += 1
            interest_writes.append((body["p_topic_id"], body["p_expected_revision"]))
            payload = {"status": "updated", "signal": "more_like", "revision": 1}
        elif request.url.endswith("/updates_since"):
            assert body["p_limit"] == 3
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
            current_ids = ["story:" + f"{index:064x}" for index in (1, 2)]
            history_ids = ["story:" + f"{index:064x}" for index in (10, 11, 12)]
            assert _visually_ordered_story_ids(page) == current_ids + history_ids[:2]
            page.locator("#load-more").click()
            page.locator("#reader-status").get_by_text("1 older story loaded.").wait_for()
            assert _visually_ordered_story_ids(page) == current_ids + history_ids
            assert page.locator(".state-action:visible").count() == 6
            assert page.locator(".state-action:enabled").count() == 12
            second = page.locator("article.card", has_text="Controller story 2")
            assert second.locator(".state-action:enabled").count() == 0
            second.locator(".save-action").evaluate(
                "button => button.dispatchEvent(new MouseEvent('click', {bubbles: true}))"
            )
            assert state_writes == []
            with page.expect_response(
                lambda response: response.url.endswith("/feed_page")
            ):
                page.locator('.chip[data-filter="quantum-computing"]').click()
            page.locator("article.card", has_text="Controller story 2").wait_for()
            assert page.locator("article.card:visible").count() == 2
            assert second.locator(".state-action:enabled").count() == 3
            assert second.get_attribute("data-state-revision") == "7"
            second.locator(".read-action").click()
            page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert state_writes[-1] == (second.get_attribute("data-story-id"), 7)

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
            assert first.locator(".interest-action").get_attribute("data-topic-id") == "quantum"

            with page.expect_response(
                lambda response: response.url.endswith("/feed_page")
            ):
                page.locator('.chip[data-filter="ai"]').click()
            interest_button = first.locator(".interest-action")
            assert interest_button.get_attribute("data-topic-id") == "ai"
            assert interest_button.get_attribute("aria-pressed") == "false"
            assert not first.evaluate("card => card.classList.contains('is-more-like')")
            interest_button.evaluate("button => button.click()")
            page.wait_for_function(
                "card => card.classList.contains('is-more-like') && "
                "card.querySelector('.interest-action').dataset.topicId === 'ai'",
                arg=first.element_handle(),
            )
            assert interest_writes == [("quantum", 0), ("ai", 0)]

            page.locator('.chip[data-filter="quantum-computing"]').click()
            assert interest_button.get_attribute("data-topic-id") == "quantum"
            assert interest_button.get_attribute("aria-pressed") == "true"
            assert first.evaluate("card => card.classList.contains('is-more-like')")
            assert first.get_attribute("data-interest-revision") == "1"

            before_page = _visible_story_ids(page)
            page.locator("#load-more").evaluate("button => button.click()")
            page.locator("#reader-status").get_by_text("3 older stories loaded.").wait_for()
            after_page = _visible_story_ids(page)
            assert after_page[: len(before_page)] == before_page
            assert len(after_page) == len(set(after_page)) == len(before_page) + 1

            topic_history_id = "story:" + f"{3:064x}"
            page.locator('.chip[data-filter="__all__"]').click()
            all_after_topic_history = _visually_ordered_story_ids(page)
            assert all_after_topic_history[: len(current_ids)] == current_ids
            assert all_after_topic_history == current_ids + history_ids + [topic_history_id]
            assert interest_button.get_attribute("data-topic-id") == "ai"
            assert interest_button.get_attribute("aria-pressed") == "true"
            page.locator('.chip[data-filter="__saved__"]').click()
            assert interest_button.get_attribute("data-topic-id") == "ai"
            assert _visible_story_ids(page) == [first.get_attribute("data-story-id")]
            save_button = first.locator(".save-action")
            page.wait_for_function(
                "button => !button.disabled", arg=save_button.element_handle()
            )
            save_button.focus()
            page.evaluate("window.scrollTo(0, 240)")
            rollback_scroll = page.evaluate("window.scrollY")
            fail_next_state["value"] = True
            save_button.evaluate("button => button.click()")
            page.locator("#reader-status").get_by_text(
                "Reading state could not be saved. Try again."
            ).wait_for()
            assert first.is_visible()
            assert first.evaluate("card => card.classList.contains('is-saved')")
            assert page.evaluate("document.activeElement.classList.contains('save-action')")
            assert page.evaluate("window.scrollY") == rollback_scroll

            save_button.evaluate("button => button.click()")
            page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert first.is_hidden()
            assert page.evaluate(
                "document.activeElement.matches('.chip[data-filter=\"__saved__\"]')"
            )
            assert page.evaluate("window.scrollY") == rollback_scroll

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


def test_persisted_topic_history_is_reconciled_by_first_all_page(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    current_id = "story:" + f"{1:064x}"
    site.joinpath("index.html").write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><style>
        .grid{{display:flex;flex-direction:column}}.card{{height:80px}}
        .topic-section[hidden],.card[hidden]{{display:none}}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <button class="chip" data-filter="__all__">All</button>
        <button class="chip" data-filter="quantum-computing" data-topic-id="quantum">Quantum</button>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="quantum-computing">
          <div class="grid"><article class="card" data-story-id="{current_id}"
            data-topic-ids="quantum-computing" data-topic-api-ids="quantum" data-rank-all="1">
            <button class="accordion-toggle">Current edition</button>
          </article></div>
        </section></main>
        <script>
        window.__tab = localStorage.getItem("nc-tab") || "__all__";
        window.setInterval = () => 1;
        window.NewsCuratorAuth = {{
          config: () => ({{url: "{ORIGIN}", key: "public-key"}}),
          hasSessionCandidate: () => true,
          sessionForRequest: async () => ({{access_token: "reader-token"}})
        }};
        window.NewsCuratorView = {{
          currentTab: () => window.__tab,
          addCard: () => {{}},
          apply: () => {{
            document.querySelectorAll("article.card").forEach(card => {{
              card.hidden = window.__tab !== "__all__" &&
                !(card.dataset.topicIds || "").split(" ").includes(window.__tab);
              const rank = card.getAttribute(window.__tab === "__all__"
                ? "data-rank-all" : `data-rank-${{window.__tab}}`);
              card.style.order = rank === null ? "0" : rank;
            }});
          }}
        }};
        document.querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {{
          window.__tab = chip.dataset.filter;
          localStorage.setItem("nc-tab", window.__tab);
          window.NewsCuratorView.apply();
        }}));
        window.NewsCuratorView.apply();
        window.BroadcastChannel = undefined;
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    older = _story(21, "history_freshness")
    older.update({"publication_seq": 5, "topic_ids": ["quantum"], "topic_ranks": {"quantum": 1}})
    fresher = _story(20, "history_freshness")
    fresher.update({"publication_seq": 6, "topic_ids": ["quantum"], "topic_ranks": {"quantum": 2}})
    calls = {"quantum": 0}

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "quantum", "name": "Quantum Computing"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 2,
            }
        elif request.url.endswith("/feed_page"):
            if body["p_topic_id"] == "quantum":
                calls["quantum"] += 1
                payload = [_story(1)] if calls["quantum"] == 1 else [older]
            else:
                assert body["p_topic_id"] is None
                assert body["p_order_mode"] == "history_freshness"
                payload = [fresher]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script("localStorage.setItem('nc-tab', 'quantum-computing')")
            page = context.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            page.locator("#load-more").click()
            page.locator("#reader-status").get_by_text("1 older story loaded.").wait_for()
            assert _visually_ordered_story_ids(page) == [current_id, older["story_id"]]
            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.locator('.chip[data-filter="__all__"]').click()
            assert _visually_ordered_story_ids(page) == [
                current_id, fresher["story_id"], older["story_id"],
            ]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_saved_only_card_stays_after_current_edition_and_reconciles_on_all(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    current_id = "story:" + f"{1:064x}"
    site.joinpath("index.html").write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><style>
        .grid{{display:flex;flex-direction:column}}.card{{height:80px}}
        .topic-section[hidden],.card[hidden]{{display:none}}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <button class="chip" data-filter="__all__">All</button>
        <button class="chip" data-filter="__saved__">Saved</button>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="ai" data-topic-id="ai">
          <div class="grid"><article class="card" data-story-id="{current_id}"
            data-topic-ids="ai" data-topic-api-ids="ai" data-rank-all="1">
            <button class="accordion-toggle">Current edition</button>
          </article></div>
        </section></main>
        <script>
        window.__tab = localStorage.getItem("nc-tab") || "__all__";
        window.setInterval = () => 1;
        window.NewsCuratorAuth = {{
          config: () => ({{url: "{ORIGIN}", key: "public-key"}}),
          hasSessionCandidate: () => true,
          sessionForRequest: async () => ({{access_token: "reader-token"}})
        }};
        window.NewsCuratorView = {{
          currentTab: () => window.__tab,
          addCard: () => {{}},
          apply: () => {{
            document.querySelectorAll("article.card").forEach(card => {{
              card.hidden = window.__tab === "__saved__"
                ? !card.classList.contains("is-saved")
                : false;
              const rank = card.getAttribute("data-rank-all");
              card.style.order = rank === null ? "0" : rank;
            }});
          }}
        }};
        document.querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {{
          window.__tab = chip.dataset.filter;
          localStorage.setItem("nc-tab", window.__tab);
          window.NewsCuratorView.apply();
        }}));
        window.NewsCuratorView.apply();
        window.BroadcastChannel = undefined;
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    saved_only = _story(40, "saved_at")
    saved_only.update(
        {
            "saved_at": "2026-09-06T12:00:00Z",
            "topic_ids": ["ai"],
            "topic_ranks": {"ai": 9},
            "next_cursor": {
                "before_saved_at": "2026-09-06T12:00:00Z",
                "before_story_id": saved_only["story_id"],
            },
        }
    )
    fresher = _story(30, "history_freshness")
    fresher.update({"topic_ids": ["ai"], "topic_ranks": {"ai": 8}})
    saved_history = {**saved_only, "page_order_mode": "history_freshness"}
    saved_history["next_cursor"] = {
        "before_published_at": saved_only["published_at"],
        "before_story_id": saved_only["story_id"],
    }

    def fulfill(route: object) -> None:
        request = route.request
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 2,
            }
        elif request.url.endswith("/saved_page"):
            payload = [saved_only]
        elif request.url.endswith("/feed_page"):
            assert request.post_data_json["p_topic_id"] is None
            payload = [fresher, saved_history]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script("localStorage.setItem('nc-tab', '__saved__')")
            page = context.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            saved_card = page.locator(
                f'article.card[data-story-id="{saved_only["story_id"]}"]'
            )
            saved_card.wait_for()
            provisional_rank = int(saved_card.get_attribute("data-rank-all") or "0")
            assert provisional_rank > 1_000_000

            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.locator('.chip[data-filter="__all__"]').click()
            assert _visually_ordered_story_ids(page) == [
                current_id, fresher["story_id"], saved_only["story_id"],
            ]
            assert int(saved_card.get_attribute("data-rank-all") or "0") == 1_000_002
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_short_initial_all_page_continues_at_retention_cursor(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    current_id = "story:" + f"{1:064x}"
    site.joinpath("index.html").write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><style>
        .grid{{display:flex;flex-direction:column}}.card{{height:80px}}
        .topic-section[hidden],.card[hidden]{{display:none}}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <button class="chip" data-filter="__all__">All</button>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="ai">
          <div class="grid"><article class="card" data-story-id="{current_id}"
            data-topic-ids="ai" data-topic-api-ids="ai" data-rank-all="1">
            <button class="accordion-toggle">Current edition</button>
          </article></div>
        </section></main>
        <script>
        window.setInterval = () => 1;
        window.NewsCuratorAuth = {{
          config: () => ({{url: "{ORIGIN}", key: "public-key"}}),
          hasSessionCandidate: () => false,
          sessionForRequest: async () => null
        }};
        window.NewsCuratorView = {{
          currentTab: () => "__all__", addCard: () => {{}},
          apply: () => {{
            document.querySelectorAll("article.card").forEach(card => {{
              card.hidden = false;
              card.style.order = card.getAttribute("data-rank-all") || "0";
            }});
          }}
        }};
        window.BroadcastChannel = undefined;
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first_history = _story(30, "history_freshness")
    older_history = _story(31, "history_freshness")
    calls: list[dict[str, object]] = []
    retention_cursor = {
        "before_published_at": "2026-09-02T12:00:00Z",
        "before_story_id": "",
    }

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": retention_cursor,
                "poll_seconds": 30,
                "page_size": 2,
            }
        elif request.url.endswith("/feed_page"):
            calls.append(body)
            if len(calls) == 1:
                assert body.get("p_before_published_at") is None
                payload = [first_history]
            else:
                assert body["p_before_published_at"] == retention_cursor["before_published_at"]
                assert body["p_before_story_id"] == retention_cursor["before_story_id"]
                payload = [older_history]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            assert _visually_ordered_story_ids(page) == [current_id, first_history["story_id"]]
            page.locator("#load-more").click()
            page.locator("#reader-status").get_by_text("1 older story loaded.").wait_for()
            assert len(calls) == 2
            assert _visually_ordered_story_ids(page) == [
                current_id, first_history["story_id"], older_history["story_id"],
            ]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_session_arrival_invalidates_anonymous_tabs_before_private_hydration(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    first_id = "story:" + f"{1:064x}"
    second_id = "story:" + f"{2:064x}"
    site.joinpath("index.html").write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><style>
        .grid{{display:flex;flex-direction:column}}.topic-section[hidden],.card[hidden]{{display:none}}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <button class="chip" data-filter="__all__">All</button>
        <button class="chip" data-filter="ai" data-topic-id="ai">AI</button>
        <button class="chip" data-filter="quantum-computing" data-topic-id="quantum">Quantum</button>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="ai">
          <div class="grid">
            <article class="card" data-story-id="{first_id}" data-topic-ids="ai"
              data-topic-api-ids="ai" data-state-revision="0" data-interest-revision="0"
              data-rank-all="1" data-rank-ai="1">
              <button class="accordion-toggle" aria-expanded="false">AI story</button>
              <button class="state-action read-action" disabled>Mark read</button>
              <button class="state-action save-action" disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="ai" disabled>More like this</button>
            </article>
            <article class="card" data-story-id="{second_id}" data-topic-ids="quantum-computing"
              data-topic-api-ids="quantum" data-state-revision="0" data-interest-revision="0"
              data-rank-all="2" data-rank-quantum-computing="1">
              <button class="accordion-toggle" aria-expanded="false">Quantum story</button>
              <button class="state-action read-action" disabled>Mark read</button>
              <button class="state-action save-action" disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="quantum" disabled>More like this</button>
            </article>
          </div>
        </section></main>
        <script>
        window.__tab = "__all__";
        window.__signedIn = false;
        window.setInterval = () => 1;
        window.NewsCuratorAuth = {{
          config: () => ({{url: "{ORIGIN}", key: "public-key"}}),
          hasSessionCandidate: () => window.__signedIn,
          sessionForRequest: async () => window.__signedIn ? ({{access_token: "private-token"}}) : null,
          acceptSession: () => {{ window.__signedIn = true; }},
          clearSession: () => {{ window.__signedIn = false; }},
          channelName: "news-curator-auth"
        }};
        window.NewsCuratorView = {{
          currentTab: () => window.__tab, addCard: () => {{}},
          apply: () => {{
            document.querySelectorAll("article.card").forEach(card => {{
              card.hidden = window.__tab !== "__all__" &&
                !(card.dataset.topicIds || "").split(" ").includes(window.__tab);
            }});
          }}
        }};
        document.querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {{
          window.__tab = chip.dataset.filter;
          window.NewsCuratorView.apply();
        }}));
        window.BroadcastChannel = class {{
          constructor() {{ window.__authChannel = this; }}
          addEventListener(_type, listener) {{ this.listener = listener; }}
          postMessage() {{}}
        }};
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    feed_calls: list[tuple[bool, str | None]] = []
    state_writes: list[tuple[str, int]] = []

    def state_row(index: int, *, authenticated: bool, topic_id: str | None) -> dict[str, object]:
        row = _story(index, "history_freshness" if topic_id is None else "edition_rank")
        row["topic_ids"] = ["ai"] if index == 1 else ["quantum"]
        row["topic_ranks"] = {row["topic_ids"][0]: 1}
        if authenticated:
            row["state_revision"] = 5 if index == 1 else 7
            row["saved_at"] = "2026-09-07T12:02:00Z"
            row["read_at"] = "2026-09-07T12:01:00Z"
        return row

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        authenticated = request.headers.get("authorization") == "Bearer private-token"
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [
                    {"topic_id": "ai", "name": "AI"},
                    {"topic_id": "quantum", "name": "Quantum Computing"},
                ],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 2,
            }
        elif request.url.endswith("/feed_page"):
            topic_id = body["p_topic_id"]
            feed_calls.append((authenticated, topic_id))
            if topic_id is None:
                payload = [
                    state_row(1, authenticated=authenticated, topic_id=topic_id),
                    state_row(2, authenticated=authenticated, topic_id=topic_id),
                ]
            elif topic_id == "ai":
                payload = [state_row(1, authenticated=authenticated, topic_id=topic_id)]
            else:
                assert topic_id == "quantum"
                payload = [state_row(2, authenticated=authenticated, topic_id=topic_id)]
        elif request.url.endswith("/set_story_state"):
            state_writes.append((body["p_story_id"], body["p_expected_revision"]))
            payload = {
                "status": "updated", "read_at": "2026-09-07T12:01:00Z",
                "saved_at": None, "revision": body["p_expected_revision"] + 1,
            }
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            quantum = page.locator(f'article.card[data-story-id="{second_id}"]')
            page.locator('.chip[data-filter="quantum-computing"]').click()
            page.wait_for_function("() => window.__tab === 'quantum-computing'")
            page.locator('.chip[data-filter="ai"]').click()
            page.wait_for_function("() => window.__tab === 'ai'")
            assert (False, "quantum") in feed_calls
            assert (False, "ai") in feed_calls

            page.evaluate(
                "window.__authChannel.listener({data: {type: 'session', session: {token: 'opaque'}}})"
            )
            ai = page.locator(f'article.card[data-story-id="{first_id}"]')
            page.wait_for_function(
                "card => card.dataset.stateRevision === '5'", arg=ai.element_handle()
            )
            assert ai.locator(".read-action").inner_text() == "Mark unread"
            assert ai.locator(".save-action").inner_text() == "Unsave"
            assert quantum.locator(".state-action:enabled").count() == 0

            quantum.locator(".save-action").evaluate(
                "button => button.dispatchEvent(new MouseEvent('click', {bubbles: true}))"
            )
            assert state_writes == []

            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.locator('.chip[data-filter="quantum-computing"]').click()
            page.wait_for_function(
                "card => card.dataset.stateRevision === '7'", arg=quantum.element_handle()
            )
            assert quantum.locator(".read-action").inner_text() == "Mark unread"
            assert quantum.locator(".save-action").inner_text() == "Unsave"
            assert quantum.locator(".state-action:enabled").count() == 3
            quantum.locator(".save-action").click()
            page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert state_writes == [(second_id, 7)]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unconfigured_page_keeps_articles_readable_without_interactive_state_controls(
    tmp_path: Path, now: object
) -> None:
    site = tmp_path / "site"
    item = make_item("Public story")
    item.description = (
        "The publisher supplied a complete summary of this public story for readers. "
        "It explains the reported development with enough context to understand why it matters. "
        "It also identifies the next expected step without requiring any synchronized reading features."
    )
    render_site(
        {"AI": [item]},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            assert page.get_by_text("Public story", exact=True).is_visible()
            page.get_by_text("Public story", exact=True).click()
            assert page.locator("a", has_text="Read original").is_visible()
            assert page.locator(".state-action:visible").count() == 0
            assert page.locator(".state-action:enabled").count() == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_logout_removes_dynamic_saved_card_from_dom_and_view_index(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "reader.js").write_bytes((ROOT / "static" / "reader.js").read_bytes())
    public_id = _story(1)["story_id"]
    dynamic_id = _story(77)["story_id"]
    (site / "index.html").write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><style>
        .card[hidden],.topic-section[hidden],#empty[hidden],#updates-status[hidden]{{display:none}}
        .grid{{display:flex;flex-direction:column}}
        </style></head><body>
        <a class="profile-link" href="#">Profile</a>
        <button class="chip" data-filter="__all__">All</button>
        <button class="chip" data-filter="__saved__">Saved</button>
        <button class="chip" data-filter="ai" data-topic-id="ai">AI</button>
        <input id="q"><span id="count"></span><span id="active-topic" hidden></span>
        <p id="reader-status"></p><button id="load-more">Load more</button>
        <p id="updates-status" hidden><button id="show-updates"></button></p>
        <main id="sections"><section class="topic-section" data-section="ai" data-topic-id="ai">
          <div class="grid"><article class="card" data-story-id="{public_id}"
            data-topic-ids="ai" data-topic-api-ids="ai" data-rank-all="1" data-rank-ai="1"
            data-state-revision="0" data-interest-revision="0">
            <button class="headline accordion-toggle">Public story</button><div class="full">Public summary</div>
            <button class="state-action read-action" disabled>Mark read</button>
            <button class="state-action save-action" disabled>Save</button>
            <button class="state-action interest-action" data-topic-id="ai" disabled>More like this</button>
          </article></div>
        </section></main><p id="empty" hidden>Nothing matched in this window.</p>
        <script>localStorage.setItem("nc-tab", "__saved__");</script>
        <script>{VIEW_JS}</script>
        <script>
        window.__signedIn = true;
        window.setInterval = () => 1;
        window.NewsCuratorAuth = {{
          config: () => ({{url: "{ORIGIN}", key: "public-key"}}),
          hasSessionCandidate: () => window.__signedIn,
          sessionForRequest: async () => window.__signedIn ? ({{access_token: "private-token"}}) : null,
          clearSession: () => {{ window.__signedIn = false; }},
          channelName: "news-curator-auth"
        }};
        window.BroadcastChannel = class {{
          constructor() {{ window.__authChannel = this; }}
          addEventListener(_type, listener) {{ this.listener = listener; }}
          postMessage() {{}}
        }};
        </script><script src="reader.js"></script></body></html>""",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def fulfill(route: object) -> None:
        request = route.request
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 2,
            }
        elif request.url.endswith("/saved_page"):
            payload = [_story(77, "saved_at") | {
                "title": "Saved-only story",
                "saved_at": "2026-09-07T12:02:00Z",
                "topic_ids": ["ai"],
                "topic_ranks": {"ai": 7},
                "next_cursor": {"before_saved_at": "2026-09-07T12:02:00Z", "before_story_id": dynamic_id},
            }]
        elif request.url.endswith("/feed_page"):
            payload = [_story(1)]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            dynamic = page.locator(f'article.card[data-story-id="{dynamic_id}"]')
            assert dynamic.is_visible()
            page.locator("#q").fill("Saved-only")
            assert page.locator("#count").inner_text() == "1 matching story"

            page.evaluate("window.__authChannel.listener({data: {type: 'logout'}})")
            page.locator("#reader-status").get_by_text("Signed out. Public stories are ready.").wait_for()
            assert page.evaluate("window.NewsCuratorView.currentTab()") == "__all__"
            assert dynamic.count() == 0
            assert page.locator("#count").inner_text() == "0 matching stories"
            assert page.locator("#empty").is_visible()
            page.locator("#q").fill("")
            public = page.locator(f'article.card[data-story-id="{public_id}"]')
            assert public.is_visible()
            assert page.locator("article.card").count() == 1
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
