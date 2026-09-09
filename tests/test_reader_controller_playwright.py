from __future__ import annotations

import json
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from curator.models import TierResult
from curator.identity import story_id_for_item
from curator.render import JS as VIEW_JS, render_site
from scripts.build_auth_callback import activate_personalization_link
from tests.conftest import make_item
from tests.test_auth_callback_playwright import _muted_browser_runtime  # noqa: F401


playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://project-ref.supabase.co"


def _launch_browser(playwright: object) -> object:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        return playwright.chromium.launch(headless=True, channel="chrome")


def _install_signed_auth_stub(site: Path) -> None:
    (site / "auth-stub.js").write_text(
        f'''window.NewsCuratorAuth={{
          config:()=>({{url:"{ORIGIN}",key:"public-key"}}),
          hasSessionCandidate:()=>true,
          sessionForRequest:async()=>({{access_token:"reader-token"}}),
          channelName:"news-curator-auth"
        }};''',
        encoding="utf-8",
    )
    html = (site / "index.html").read_text(encoding="utf-8")
    html, replacements = re.subn(
        r'<script src="auth/client\.js\?v=[0-9a-f]{16}" defer></script>',
        '<script src="auth-stub.js" defer></script>',
        html,
    )
    assert replacements == 1, "Signed fixture must replace the rendered auth client exactly once"
    (site / "index.html").write_text(html, encoding="utf-8")


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


def test_pagination_hides_exhausted_scopes_but_preserves_cursor_and_retry(
    tmp_path: Path, now: object
) -> None:
    site = tmp_path / "site"
    render_site(
        {name: [make_item(name)] for name in ("AI", "US News")},
        [TierResult(tier="rss", items=[], ok=True)], now, site,
        topic_ids_by_name={"AI": "ai", "US News": "us"},
    )
    activate_personalization_link(
        site / "index.html", supabase_url=ORIGIN, publishable_key="sb_publishable_test",
    )
    _install_signed_auth_stub(site)
    calls = []
    fail_once = [True]
    held = []
    initial_cursor = {"before_published_at": "2026-09-02T12:00:00Z",
                      "before_story_id": "story:" + "0" * 64}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload = {
                "publication_seq": 7, "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}, {"topic_id": "us", "name": "US News"}],
                "initial_history_cursor": initial_cursor, "page_size": 2, "poll_seconds": 300,
            }
        elif request.url.endswith("/saved_page"):
            payload = []
        elif request.url.endswith("/feed_page"):
            calls.append(body)
            topic = body["p_topic_id"]
            if topic is None and body.get("p_before_published_at") is None:
                payload = [_story(1, "history_freshness"), _story(2, "history_freshness")]
            elif topic is None and fail_once[0]:
                fail_once[0] = False
                route.fulfill(status=500, content_type="application/json", body='{}')
                return
            elif topic == "us" and body["p_order_mode"] == "history_freshness":
                held.append(route)
                return
            else:
                payload = []
        else:
            raise AssertionError("Unexpected pagination RPC")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            more = page.locator("#load-more")
            playwright_api.expect(more).to_be_visible()
            search = page.get_by_role("searchbox")
            search.fill("No matching local headline")
            playwright_api.expect(more).to_be_visible()
            more.click()
            playwright_api.expect(page.locator("#reader-status")).to_have_text(
                "Older stories could not be loaded. Try again."
            )
            playwright_api.expect(more).to_be_visible()
            playwright_api.expect(more).to_be_enabled()
            failed_cursor = calls[-1]
            more.click()
            playwright_api.expect(more).to_be_hidden()
            assert calls[-1] == failed_cursor, "Retry must preserve the failed page cursor"
            playwright_api.expect(page.locator("#reader-status")).to_have_text(
                "No older stories remain in this section."
            )
            search.fill("")
            playwright_api.expect(more).to_be_hidden()
            page.get_by_role("button", name="AI", exact=True).click()
            playwright_api.expect(more).to_be_visible()
            playwright_api.expect(more).to_be_enabled()
            playwright_api.expect(page.locator("#reader-status")).not_to_contain_text(
                "No older stories remain"
            )
            more.click()
            playwright_api.expect(more).to_be_hidden()
            assert calls[-1]["p_order_mode"] == "history_freshness"
            assert calls[-1]["p_before_published_at"] == initial_cursor["before_published_at"]
            page.get_by_role("button", name="Saved", exact=True).click()
            playwright_api.expect(more).to_be_hidden()
            page.get_by_role("button", name="US News", exact=True).click()
            playwright_api.expect(more).to_be_visible()
            playwright_api.expect(more).to_be_enabled()
            more.click()
            playwright_api.expect(more).to_be_disabled()
            page.get_by_role("button", name="AI", exact=True).click()
            playwright_api.expect(more).to_be_hidden()
            prior_status = page.locator("#reader-status").text_content()
            assert len(held) == 1
            held.pop().fulfill(status=200, content_type="application/json", body='[]')
            page.wait_for_timeout(50)
            playwright_api.expect(more).to_be_hidden()
            assert page.locator("#reader-status").text_content() == prior_status
            page.get_by_role("button", name="US News", exact=True).click()
            playwright_api.expect(more).to_be_hidden()
            page.get_by_role("button", name="All", exact=True).click()
            playwright_api.expect(more).to_be_hidden()
            page.set_viewport_size({"width": 390, "height": 844})
            playwright_api.expect(more).to_be_hidden()
            footer = page.locator("footer")
            assert footer.inner_text().strip() == "Privacy"
            assert footer.get_by_role("link", name="Privacy").get_attribute("href") == "privacy.html"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
          <button class="chip" data-filter="__saved__" hidden disabled>Saved</button>
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
              <button class="state-action read-action" hidden disabled>Mark unread</button>
              <button class="state-action save-action" hidden disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="ai" hidden disabled>More like this</button>
            </article>
            <article class="card" data-story-id="story:0000000000000000000000000000000000000000000000000000000000000002"
              data-topic-ids="quantum-computing" data-topic-api-ids="quantum"
              data-state-revision="0" data-interest-revision="0" data-rank-all="2">
              <button class="accordion-toggle" aria-expanded="false">Controller story 2</button>
              <button class="state-action read-action" hidden disabled>Mark unread</button>
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
    fail_state_write_number: dict[str, int | None] = {"value": None}

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
            if fail_next_state["value"] or len(state_writes) == fail_state_write_number["value"]:
                fail_next_state["value"] = False
                fail_state_write_number["value"] = None
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
                "revision": body["p_expected_revision"] + 1,
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
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 900, "height": 700})
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            page.locator('.chip[data-filter="__saved__"]:visible:enabled').wait_for()
            current_ids = ["story:" + f"{index:064x}" for index in (1, 2)]
            history_ids = ["story:" + f"{index:064x}" for index in (10, 11, 12)]
            assert _visually_ordered_story_ids(page) == current_ids + history_ids[:2]
            page.locator("#load-more").click()
            page.locator("#reader-status").get_by_text("1 older story loaded.").wait_for()
            assert _visually_ordered_story_ids(page) == current_ids + history_ids
            assert page.locator(".state-action:visible").count() == 4
            assert page.locator(".state-action:enabled").count() == 13
            second = page.locator("article.card", has_text="Controller story 2")
            assert second.locator(".state-action:enabled").count() == 1
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
            second.locator(".accordion-toggle").evaluate(
                "button => { button.setAttribute('aria-expanded', 'true'); button.click(); }"
            )
            page.wait_for_function(
                "card => card.dataset.stateRevision === '8'", arg=second.element_handle()
            )
            assert state_writes[-1] == (second.get_attribute("data-story-id"), 7)
            assert second.evaluate("card => card.classList.contains('is-read')")
            assert second.locator(".read-action").is_visible()

            first = page.locator("article.card").first
            first_start_revision = int(first.get_attribute("data-state-revision") or "0")
            first.locator(".accordion-toggle").evaluate(
                "button => { button.setAttribute('aria-expanded', 'true'); button.click(); }"
            )
            page.wait_for_function(
                "([card, revision]) => card.dataset.stateRevision === String(revision + 1)",
                arg=[first.element_handle(), first_start_revision],
            )
            first.locator(".read-action").click()
            page.wait_for_function(
                "([card, revision]) => card.dataset.stateRevision === String(revision + 2)",
                arg=[first.element_handle(), first_start_revision],
            )
            assert not first.evaluate("card => card.classList.contains('is-read')")
            rapid_revision = int(first.get_attribute("data-state-revision") or "0")
            rapid_writes = len(state_writes)
            fail_state_write_number["value"] = rapid_writes + 2
            first.evaluate(
                "card => {"
                " const toggle = card.querySelector('.accordion-toggle');"
                " toggle.setAttribute('aria-expanded', 'true');"
                " toggle.click();"
                " card.querySelector('.read-action').click();"
                "}"
            )
            page.locator("#reader-status").get_by_text(
                "Reading state could not be saved. Try again."
            ).wait_for()
            assert len(state_writes) == rapid_writes + 2
            assert state_writes[-2:] == [
                (first.get_attribute("data-story-id"), rapid_revision),
                (first.get_attribute("data-story-id"), rapid_revision + 1),
            ]
            assert first.evaluate("card => card.classList.contains('is-read')")
            assert first.get_attribute("data-state-revision") == str(rapid_revision + 1)
            assert first.locator(".read-action").is_visible()
            double_click_revision = int(first.get_attribute("data-state-revision") or "0")
            writes_before_double_click = len(state_writes)
            first.locator(".save-action").evaluate(
                "button => {"
                " button.dispatchEvent(new MouseEvent('click', {bubbles: true}));"
                " button.dispatchEvent(new MouseEvent('click', {bubbles: true}));"
                "}"
            )
            page.wait_for_function(
                "([card, revision]) => card.dataset.stateRevision === String(revision + 1)",
                arg=[first.element_handle(), double_click_revision],
            )
            assert len(state_writes) == writes_before_double_click + 1
            assert state_writes[-1] == (
                first.get_attribute("data-story-id"), double_click_revision
            )
            assert first.evaluate("card => card.classList.contains('is-saved')")
            assert first.locator(".read-action").is_enabled()
            assert first.locator(".save-action").is_enabled()

            first.locator(".save-action").click()
            page.wait_for_function(
                "([card, revision]) => card.dataset.stateRevision === String(revision + 2)",
                arg=[first.element_handle(), double_click_revision],
            )
            assert state_writes[-1] == (
                first.get_attribute("data-story-id"), double_click_revision + 1
            )
            assert not first.evaluate("card => card.classList.contains('is-saved')")
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
            browser = _launch_browser(playwright)
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
            browser = _launch_browser(playwright)
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
            browser = _launch_browser(playwright)
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
              <button class="state-action read-action" hidden disabled>Mark unread</button>
              <button class="state-action save-action" disabled>Save</button>
              <button class="state-action interest-action" data-topic-id="ai" disabled>More like this</button>
            </article>
            <article class="card" data-story-id="{second_id}" data-topic-ids="quantum-computing"
              data-topic-api-ids="quantum" data-state-revision="0" data-interest-revision="0"
              data-rank-all="2" data-rank-quantum-computing="1">
              <button class="accordion-toggle" aria-expanded="false">Quantum story</button>
              <button class="state-action read-action" hidden disabled>Mark unread</button>
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
            browser = _launch_browser(playwright)
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
            assert quantum.locator(".state-action:enabled").count() == 1

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
        {"AI": [item], "Quantum Computing": [item]},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
        topic_ids_by_name={"AI": "ai", "Quantum Computing": "quantum"},
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context()
            context.add_init_script("localStorage.setItem('nc-tab', '__saved__')")
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            assert page.get_by_text("Public story", exact=True).is_visible()
            saved = page.locator('.chip[data-filter="__saved__"]')
            assert saved.count() == 2
            assert saved.evaluate_all(
                "tabs => tabs.every(tab => tab.hidden && tab.disabled)"
            )
            page.get_by_text("Public story", exact=True).click()
            assert page.locator("a", has_text="Read original").is_visible()
            assert page.locator(".state-action:visible").count() == 1
            assert page.get_by_role("button", name="Mark unread").is_enabled()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_main_navigation_stays_fully_visible_at_real_footer_bottom(
    tmp_path: Path, now: object
) -> None:
    site = tmp_path / "site"
    ranked = {}
    topic_ids = {}
    for topic_index in range(8):
        name = f"Long topic {topic_index + 1}"
        topic_ids[name] = f"topic-{topic_index + 1}"
        ranked[name] = [
            make_item(
                f"Story {topic_index + 1}-{story_index + 1}",
                f"https://publisher.example/{topic_index + 1}/{story_index + 1}",
            )
            for story_index in range(6)
        ]
    render_site(
        ranked,
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
        topic_ids_by_name=topic_ids,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            for viewport in (
                {"width": 1440, "height": 1000},
                {"width": 1100, "height": 560},
                {"width": 390, "height": 844},
            ):
                page.set_viewport_size(viewport)
                page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
                scroll_before = page.evaluate("scrollY")
                if viewport["width"] > 800:
                    container = page.locator("aside.rail")
                    chips = page.locator(".railnav .chip:visible:not([disabled])")
                else:
                    container = page.locator(".tools")
                    chips = page.locator(".mobiletopics .chip:visible:not([disabled])")
                chips.first.focus()
                for _ in range(chips.count() - 1):
                    page.keyboard.press("Tab")
                last = chips.last
                assert last.evaluate("node => document.activeElement === node")
                box = last.bounding_box()
                bounds = container.bounding_box()
                assert box is not None and bounds is not None
                assert 0 <= bounds["y"]
                assert bounds["y"] + bounds["height"] <= viewport["height"]
                assert bounds["x"] <= box["x"]
                assert box["x"] + box["width"] <= bounds["x"] + bounds["width"]
                assert bounds["y"] <= box["y"]
                assert box["y"] + box["height"] <= bounds["y"] + bounds["height"]
                assert page.evaluate("scrollY") == scroll_before
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("signed_in", [False, True], ids=["anonymous", "signed-delayed-hydration"])
def test_real_render_open_marks_read_locally_and_unread_reopens_without_layout_jump(
    tmp_path: Path, now: object, signed_in: bool
) -> None:
    site = tmp_path / "site"
    item = make_item("Automatic read feedback")
    item.description = (
        "The publisher supplied a complete summary that exercises the actual rendered "
        "accordion, local read feedback, and deliberate unread behavior."
    )
    story_id = story_id_for_item(item)
    render_site(
        {"AI": [item]},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
    )
    activate_personalization_link(
        site / "index.html",
        supabase_url=ORIGIN,
        publishable_key="sb_publishable_test",
    )
    if signed_in:
        _install_signed_auth_stub(site)
    (site / "pre-reader-open.js").write_text(
        "document.querySelector('article.card .headline').click();",
        encoding="utf-8",
    )
    html = (site / "index.html").read_text(encoding="utf-8")
    html, replacements = re.subn(
        r'(<script src="reader\.js\?v=[0-9a-f]{16}" defer></script>)',
        r'<script src="pre-reader-open.js"></script>\1',
        html,
    )
    assert replacements == 1, "Early-open fixture must precede the rendered reader exactly once"
    (site / "index.html").write_text(html, encoding="utf-8")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state_writes: list[tuple[bool, int]] = []

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7, "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [
                    {"topic_id": "ai", "name": "AI"},
                    {"topic_id": "quantum", "name": "Quantum Computing"},
                ],
                "initial_history_cursor": None, "poll_seconds": 30, "page_size": 3,
            }
        elif request.url.endswith("/feed_page"):
            payload = [_story(1) | {
                "story_id": story_id, "title": "Automatic read feedback",
                "topic_ids": ["ai", "quantum"], "topic_ranks": {"ai": 1, "quantum": 1},
            }]
        elif request.url.endswith("/set_story_state"):
            state_writes.append((body["p_read"], body["p_expected_revision"]))
            payload = {
                "status": "updated", "read_at": "2026-09-07T12:01:00Z",
                "saved_at": None, "revision": 1,
            }
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(viewport={"width": 900, "height": 700})
            context.add_init_script(
                """(() => {
                  const nativeFetch = window.fetch.bind(window);
                  window.fetch = (...args) => String(args[0]).includes('latest_publication')
                    ? new Promise((resolve, reject) => {
                        window.__releaseReaderFetch = () => nativeFetch(...args).then(resolve, reject);
                      })
                    : nativeFetch(...args);
                })();"""
            )
            page = context.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            card = page.locator("article.card")
            headline = card.locator(".headline")
            before_color = card.evaluate(
                """node => {
                  node.classList.remove('is-read');
                  const color = getComputedStyle(node.querySelector('.headline')).color;
                  node.classList.add('is-read');
                  return color;
                }"""
            )
            before_weight = headline.evaluate("node => getComputedStyle(node).fontWeight")
            before_order = _visible_story_ids(page)
            page.evaluate("window.scrollTo(0, 40)")
            before_scroll = page.evaluate("window.scrollY")
            before_headline_box = headline.bounding_box()

            assert card.evaluate("node => node.classList.contains('is-read')")
            assert headline.evaluate("node => getComputedStyle(node).color") != before_color
            assert headline.evaluate("node => getComputedStyle(node).fontWeight") == before_weight
            assert headline.bounding_box() == pytest.approx(before_headline_box, abs=0.5)
            assert _visible_story_ids(page) == before_order
            assert page.evaluate("window.scrollY") == before_scroll
            assert page.get_by_role("button", name="Mark read").count() == 0
            if not signed_in:
                assert not page.locator("#reader-status").inner_text().startswith("Signed out.")
            unread = page.get_by_role("button", name="Mark unread")
            assert unread.is_visible() and unread.is_enabled()
            page.locator('.chip[data-filter="ai"]:visible').click()
            assert card.is_visible()
            assert unread.is_enabled()

            unread.scroll_into_view_if_needed()
            before_unread_scroll = page.evaluate("window.scrollY")
            before_unread_order = _visible_story_ids(page)
            unread.click()
            assert card.evaluate("node => !node.classList.contains('is-read')")
            assert unread.is_hidden()
            assert page.evaluate("document.activeElement.classList.contains('accordion-toggle')")
            assert page.evaluate("window.scrollY") == before_unread_scroll
            assert _visible_story_ids(page) == before_unread_order
            assert card.locator(".detail").is_visible()
            card.locator(".shut").click()
            assert not card.evaluate("node => node.classList.contains('is-read')")
            headline.click()
            assert card.evaluate("node => node.classList.contains('is-read')")
            assert unread.is_visible()
            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.evaluate("window.__releaseReaderFetch()")
            if signed_in:
                page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
                assert state_writes == [(True, 0)]
            else:
                assert state_writes == []
            assert card.evaluate("node => node.classList.contains('is-read')")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_active_read_mutation_uses_newest_hydrated_rollback_baseline(
    tmp_path: Path, now: object
) -> None:
    site = tmp_path / "site"
    item = make_item("Hydration race story")
    item.description = "The publisher supplied a complete summary for state race coverage."
    story_id = story_id_for_item(item)
    render_site(
        {"AI": [item], "Quantum Computing": [item]},
        [TierResult(tier="rss", items=[], ok=True)], now, site,
        topic_ids_by_name={"AI": "ai", "Quantum Computing": "quantum"},
    )
    activate_personalization_link(
        site / "index.html", supabase_url=ORIGIN, publishable_key="sb_publishable_test",
    )
    _install_signed_auth_stub(site)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state_calls = {"value": 0}
    feed_calls = {"value": 0}

    def row(revision: int, read: bool, saved: bool) -> dict[str, object]:
        return _story(1) | {
            "story_id": story_id, "title": "Hydration race story",
            "read_at": "2026-09-07T12:01:00Z" if read else None,
            "saved_at": "2026-09-07T12:02:00Z" if saved else None,
            "state_revision": revision, "topic_ids": ["ai", "quantum"],
            "topic_ranks": {"ai": 1, "quantum": 1},
        }

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7, "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [
                    {"topic_id": "ai", "name": "AI"},
                    {"topic_id": "quantum", "name": "Quantum Computing"},
                ],
                "initial_history_cursor": None, "poll_seconds": 30, "page_size": 3,
            }
        elif request.url.endswith("/feed_page"):
            feed_calls["value"] += 1
            if body["p_topic_id"] == "ai":
                payload = [row(2, False, True)]
            elif body["p_topic_id"] == "quantum":
                payload = [row(1, False, False)]
            else:
                payload = [row(0, False, False)]
        elif request.url.endswith("/set_story_state"):
            state_calls["value"] += 1
            if state_calls["value"] == 1:
                payload = {"status": "conflict", "revision": 2}
            else:
                assert body["p_expected_revision"] == 2
                assert body["p_read"] is True and body["p_saved"] is True
                payload = {
                    "status": "updated", "read_at": "2026-09-07T12:03:00Z",
                    "saved_at": "2026-09-07T12:02:00Z", "revision": 3,
                }
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context()
            context.add_init_script(
                """(() => {
                  const nativeFetch = window.fetch.bind(window);
                  window.fetch = (...args) => String(args[0]).includes('set_story_state')
                    ? new Promise((resolve, reject) => {
                        window.__releaseStateFetch = () => nativeFetch(...args).then(resolve, reject);
                      })
                    : nativeFetch(...args);
                })();"""
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            card = page.locator(f'article.card[data-story-id="{story_id}"]')
            assert page_errors == []
            page.wait_for_timeout(500)
            assert feed_calls["value"] == 1
            assert card.locator(".save-action").is_enabled()

            card.locator(".headline").click()
            assert card.evaluate("node => node.classList.contains('is-read')")
            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.locator('.chip[data-filter="ai"]:visible').click()
            assert card.evaluate("node => node.classList.contains('is-read')")
            assert not card.evaluate("node => node.classList.contains('is-saved')")
            page.evaluate("window.__releaseStateFetch()")
            page.locator("#reader-status").get_by_text(
                "Reading state could not be saved. Try again."
            ).wait_for()
            assert not card.evaluate("node => node.classList.contains('is-read')")
            assert card.evaluate("node => node.classList.contains('is-saved')")
            assert card.get_attribute("data-state-revision") == "2"

            card.locator(".shut").click()
            card.locator(".headline").click()
            page.evaluate("window.__releaseStateFetch()")
            page.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert card.get_attribute("data-state-revision") == "3"
            with page.expect_response(lambda response: response.url.endswith("/feed_page")):
                page.locator('.chip[data-filter="quantum-computing"]:visible').click()
            assert card.evaluate("node => node.classList.contains('is-read')")
            assert card.evaluate("node => node.classList.contains('is-saved')")
            assert card.get_attribute("data-state-revision") == "3"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_signed_open_and_unread_persist_across_refresh_and_browser_context(
    tmp_path: Path, now: object
) -> None:
    site = tmp_path / "site"
    item = make_item("Signed reading state")
    item.description = "The publisher supplied a complete summary for signed state synchronization."
    story_id = story_id_for_item(item)
    render_site(
        {"AI": [item]}, [TierResult(tier="rss", items=[], ok=True)], now, site,
        topic_ids_by_name={"AI": "ai"},
    )
    activate_personalization_link(
        site / "index.html", supabase_url=ORIGIN, publishable_key="sb_publishable_test",
    )
    _install_signed_auth_stub(site)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {"read": False, "revision": 0}
    writes: list[tuple[bool, int]] = []

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7, "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None, "poll_seconds": 30, "page_size": 3,
            }
        elif request.url.endswith("/feed_page"):
            payload = [_story(1, "history_freshness") | {
                "story_id": story_id,
                "title": "Signed reading state",
                "read_at": "2026-09-07T12:01:00Z" if state["read"] else None,
                "state_revision": state["revision"],
                "topic_ids": ["ai"], "topic_ranks": {"ai": 1},
            }]
        elif request.url.endswith("/set_story_state"):
            assert body["p_expected_revision"] == state["revision"]
            writes.append((body["p_read"], body["p_expected_revision"]))
            state["read"] = body["p_read"]
            state["revision"] += 1
            payload = {
                "status": "updated",
                "read_at": "2026-09-07T12:01:00Z" if state["read"] else None,
                "saved_at": None,
                "revision": state["revision"],
            }
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            first_context = browser.new_context()
            first = first_context.new_page()
            first.route(f"{ORIGIN}/**", fulfill)
            first.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            card = first.locator(f'article.card[data-story-id="{story_id}"]')
            first.wait_for_function(
                "card => !card.querySelector('.save-action').disabled",
                arg=card.element_handle(),
            )
            card.locator(".headline").click()
            first.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert writes == [(True, 0)]
            first.reload(wait_until="networkidle")
            card = first.locator(f'article.card[data-story-id="{story_id}"]')
            assert card.evaluate("node => node.classList.contains('is-read')")

            second_context = browser.new_context()
            second = second_context.new_page()
            second.route(f"{ORIGIN}/**", fulfill)
            second.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            second_card = second.locator(f'article.card[data-story-id="{story_id}"]')
            second.wait_for_function(
                "card => !card.querySelector('.save-action').disabled",
                arg=second_card.element_handle(),
            )
            assert second_card.evaluate("node => node.classList.contains('is-read')")
            second_card.locator(".headline").click()
            second_card.get_by_role("button", name="Mark unread").click()
            second.locator("#reader-status").get_by_text("Reading state saved.").wait_for()
            assert writes[-1] == (False, 1)

            first.reload(wait_until="networkidle")
            card = first.locator(f'article.card[data-story-id="{story_id}"]')
            assert not card.evaluate("node => node.classList.contains('is-read')")
            assert card.locator(".read-action").is_hidden()
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
            <button class="state-action read-action" hidden disabled>Mark unread</button>
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
            browser = _launch_browser(playwright)
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


@pytest.mark.parametrize(
    "viewport",
    [{"width": 1440, "height": 1000}, {"width": 1100, "height": 560}, {"width": 390, "height": 844}],
    ids=["desktop", "short-desktop", "phone"],
)
def test_polled_update_banner_is_an_accessible_overlay_until_explicit_refresh(
    tmp_path: Path,
    now: object,
    viewport: dict[str, int],
) -> None:
    site = tmp_path / "site"
    items = []
    for index in range(1, 9):
        item = make_item(
            f"Banner geometry story {index}",
            f"https://publisher.example/banner-{index}",
        )
        item.description = (
            "The publisher supplied a complete public summary with enough context "
            "to keep this real rendered card visible during the update check."
        )
        items.append(item)
    render_site(
        {"AI": items},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
        topic_ids_by_name={"AI": "ai"},
    )
    activate_personalization_link(
        site / "index.html",
        supabase_url=ORIGIN,
        publishable_key="sb_publishable_test",
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = {"latest": 0, "updates": 0}
    latest_sequences: list[int] = []

    def fulfill(route: object) -> None:
        request = route.request
        if request.url.endswith("/latest_publication"):
            calls["latest"] += 1
            publication_seq = 7 if calls["latest"] == 1 else 8
            latest_sequences.append(publication_seq)
            payload: object = {
                "publication_seq": publication_seq,
                "finalized_at": "2026-09-08T04:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 3,
            }
        elif request.url.endswith("/feed_page"):
            payload = []
        elif request.url.endswith("/updates_since"):
            calls["updates"] += 1
            payload = [_update(1)]
        else:
            raise AssertionError(f"unexpected RPC: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def layout(page: object) -> dict[str, object]:
        return page.evaluate(
            """() => {
              const box = selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
              };
              return {
                topic: box('.topic-section:not([hidden]) .section-title'),
                card: box('article.card:not([hidden])'),
                scrollY: window.scrollY,
                order: [...document.querySelectorAll('article.card:not([hidden])')]
                  .map(card => card.dataset.storyId),
                focus: document.activeElement && document.activeElement.id,
              };
            }"""
        )

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(viewport=viewport)
            context.add_init_script(
                """(() => {
                  const nativeSetInterval = window.setInterval;
                  window.setInterval = (callback, delay, ...args) => {
                    window.__readerPoll = () => callback(...args);
                    window.__readerPollDelay = delay;
                    return 1;
                  };
                  window.__nativeSetInterval = nativeSetInterval;
                })();"""
            )
            page = context.new_page()
            page.route(f"{ORIGIN}/**", fulfill)
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
            page.wait_for_function("() => typeof window.__readerPoll === 'function'")
            page.evaluate(
                "window.scrollTo(0, Math.min(240, document.documentElement.scrollHeight - innerHeight))"
            )
            page.locator("#q").focus()
            before = layout(page)
            assert before["focus"] == "q"

            page.evaluate("() => window.__readerPoll()")
            page.locator("#updates-status").wait_for(state="visible")
            page.get_by_role("button", name="1 new story available").wait_for()
            after = layout(page)

            assert calls["latest"] == 2 and calls["updates"] == 1
            assert after["order"] == before["order"]
            assert after["scrollY"] == before["scrollY"]
            assert after["focus"] == "q"
            assert after["topic"] == pytest.approx(before["topic"], abs=0.5), (
                viewport,
                before,
                after,
            )
            assert after["card"] == pytest.approx(before["card"], abs=0.5), (
                viewport,
                before,
                after,
            )

            updates_button = page.get_by_role("button", name="1 new story available")
            button_box = updates_button.bounding_box()
            assert button_box is not None
            assert 0 <= button_box["x"]
            assert button_box["x"] + button_box["width"] <= viewport["width"]
            assert 0 <= button_box["y"]
            assert button_box["y"] + button_box["height"] <= viewport["height"]
            tools_box = page.locator(".tools").bounding_box()
            assert tools_box is not None
            tools_bottom = tools_box["y"] + tools_box["height"]
            assert tools_bottom <= button_box["y"] <= tools_bottom + 20
            page.screenshot(path=str(tmp_path / "top-banner.png"))
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "show-updates"
            assert updates_button.evaluate("button => button.matches(':focus-visible')")
            with page.expect_response(
                lambda response: response.url.endswith("/latest_publication")
            ):
                with page.expect_navigation(wait_until="domcontentloaded"):
                    page.keyboard.press("Enter")
            assert calls["latest"] == 3
            assert latest_sequences == [7, 8, 8]
            assert page.locator("#updates-status").is_hidden()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
