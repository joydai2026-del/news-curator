from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from curator.models import TierResult
from curator.render import render_site
from scripts.build_auth_callback import activate_personalization_link
from tests.conftest import make_item
from tests.test_auth_callback_playwright import (
    SUPABASE_ORIGIN,
    _QuietHandler,
    _feed_story,
    _jwt,
    playwright_api,
)


def _site(tmp_path: Path, now: object, *, configured: bool) -> Path:
    site = tmp_path / "site"
    item = make_item("Public edition story")
    item.description = "The publisher supplied a complete summary for the current edition."
    render_site(
        {"AI": [item]},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
        topic_ids_by_name={"AI": "ai"},
    )
    if configured:
        activate_personalization_link(
            site / "index.html",
            supabase_url=SUPABASE_ORIGIN,
            publishable_key="sb_publishable_test",
        )
    return site


def _serve(site: Path) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_dashboard_signed_out_shell_contains_no_private_rows(tmp_path: Path, now: object) -> None:
    site = _site(tmp_path, now, configured=False)
    server, thread = _serve(site)
    try:
        with playwright_api.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                headless=True, channel="chrome", args=["--mute-audio"]
            )
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(
                f"http://127.0.0.1:{server.server_port}/dashboard/",
                wait_until="networkidle",
            )
            assert page.locator("#signed-out").is_visible()
            assert page.locator("#private-dashboard").is_hidden()
            assert page.locator(".saved-card").count() == 0
            assert page.locator('.main-link[href="../"]').count() == 1
            assert page.locator("body").evaluate(
                "node => node.scrollWidth === node.clientWidth"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_logout_discards_delayed_private_loads(tmp_path: Path, now: object) -> None:
    site = _site(tmp_path, now, configured=True)
    server, thread = _serve(site)
    held_summary: list[object] = []
    held_saved: list[object] = []
    hold_summary = True
    hold_saved = False
    fail_summary = False
    summary = {
        "schema_version": 1,
        "scope": "current_retained_state",
        "snapshot_at": "2026-09-08T12:00:00Z",
        "saved_count": 1,
        "saved_unread_count": 1,
        "read_count": 0,
        "active_interest_signal_count": 0,
        "topic_signals": [],
    }
    preference = {
        "user_id": "user-a", "revision": 1, "locale": "en",
        "interests": ["private topic"], "saved_searches": [],
        "created_at": "2026-09-01T12:00:00Z", "updated_at": "2026-09-08T12:00:00Z",
    }
    saved_row = _feed_story(1, "Private delayed story")
    saved_row.update(
        story_id=f"story:{'b' * 64}", page_order_mode="saved_at",
        saved_at="2026-09-08T11:00:00Z", read_at=None, state_revision=1,
        next_cursor={"before_saved_at": "2026-09-08T11:00:00Z", "before_story_id": f"story:{'b' * 64}"},
    )

    def fulfill(route: object) -> None:
        nonlocal hold_summary, hold_saved, fail_summary
        url = route.request.url
        if url.endswith("/latest_publication"):
            payload = {"publication_seq": 7, "finalized_at": "2026-09-08T12:00:00Z", "topics": [], "initial_history_cursor": None, "poll_seconds": 30, "page_size": 7}
        elif url.endswith("/dashboard_summary"):
            if hold_summary:
                held_summary.append(route)
                return
            if fail_summary:
                route.fulfill(status=503, content_type="application/json", body="{}")
                return
            payload = summary
        elif "/user_preferences?" in url:
            payload = [preference]
        elif url.endswith("/saved_page"):
            if hold_saved:
                held_saved.append(route)
                return
            payload = [saved_row]
        else:
            raise AssertionError("Unexpected dashboard endpoint")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, channel="chrome", args=["--mute-audio"])
            context = browser.new_context(viewport={"width": 390, "height": 844})
            session = {"access_token": _jwt({"sub": "user-a"}), "refresh_token": "refresh-token", "expires_at": 4_000_000_000, "user_id": "user-a"}
            context.add_init_script(
                "sessionStorage.setItem('news-curator.auth.session', JSON.stringify(%s));" % json.dumps(session)
            )
            context.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/dashboard/", wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelector('#dashboard-status')?.textContent === 'Checking sign-in.'")
            page.wait_for_timeout(100)
            assert len(held_summary) == 1
            assert page.locator("#download-view").is_disabled()
            page.evaluate("sessionStorage.removeItem('news-curator.auth.session'); dispatchEvent(new Event('news-curator:auth-changed'))")
            hold_summary = False
            held_summary.pop().fulfill(status=200, content_type="application/json", body=json.dumps(summary))
            page.wait_for_timeout(100)
            assert page.locator("#private-dashboard").is_hidden()
            assert page.locator("#interest-list").input_value() == ""
            assert page.locator("#metric-saved").inner_text() == ""

            fail_summary = True
            page.evaluate("sessionStorage.setItem('news-curator.auth.session', JSON.stringify(%s)); dispatchEvent(new Event('news-curator:auth-changed'))" % json.dumps(session))
            page.wait_for_function("() => document.querySelector('#dashboard-status')?.textContent.includes('could not be loaded')")
            assert page.locator("#dashboard-status").is_visible()
            assert page.locator("#private-dashboard").is_hidden()

            fail_summary = False
            hold_saved = True
            page.evaluate("dispatchEvent(new Event('news-curator:auth-changed'))")
            page.wait_for_selector("#private-dashboard:not([hidden])")
            page.wait_for_timeout(100)
            assert len(held_saved) == 1
            assert page.locator("#download-view").is_disabled()
            page.evaluate("sessionStorage.removeItem('news-curator.auth.session'); dispatchEvent(new Event('news-curator:auth-changed'))")
            held_saved.pop().fulfill(status=200, content_type="application/json", body=json.dumps([saved_row]))
            page.wait_for_timeout(100)
            assert page.locator("#private-dashboard").is_hidden()
            assert page.locator(".saved-card").count() == 0
            assert page.locator("#interest-list").input_value() == ""
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_saved_preferences_snapshot_and_logout(tmp_path: Path, now: object) -> None:
    site = _site(tmp_path, now, configured=True)
    server, thread = _serve(site)
    story_id = f"story:{'a' * 64}"
    story_ids = [f"story:{digit * 64}" for digit in "abcdef01"]
    story_ids[0] = story_id
    state = {
        "read_at": None,
        "saved_at": "2026-09-08T11:00:00Z",
        "state_revision": 1,
    }
    preferences = {
        "user_id": "user-a",
        "revision": 2,
        "locale": "en",
        "interests": ["quantum computing"],
        "saved_searches": [{"id": "energy", "query": "nuclear", "enabled": True}],
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-08T11:30:00Z",
    }
    writes: list[dict[str, object]] = []
    held_summaries: list[object] = []
    held_writes: list[object] = []
    held_preferences: list[object] = []
    hold_summaries = False
    hold_write = False
    hold_preference = False
    fail_summary = False

    def summary_payload(saved_count: int = 8) -> dict[str, object]:
        return {
            "schema_version": 1,
            "scope": "current_retained_state",
            "snapshot_at": "2026-09-08T12:00:00Z",
            "saved_count": saved_count,
            "saved_unread_count": 8,
            "read_count": 0,
            "active_interest_signal_count": 1,
            "topic_signals": [
                {"topic_id": "quantum", "more_like_count": 1, "less_like_count": 0}
            ],
        }

    def fulfill(route: object) -> None:
        request = route.request
        body = request.post_data_json if request.post_data else {}
        if request.url.endswith("/latest_publication"):
            payload = {
                "publication_seq": 7,
                "finalized_at": "2026-09-08T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 7,
            }
        elif request.url.endswith("/dashboard_summary"):
            if hold_summaries:
                held_summaries.append(route)
                return
            if fail_summary:
                route.fulfill(status=503, content_type="application/json", body="{}")
                return
            payload = summary_payload()
        elif request.url.endswith("/saved_page"):
            offset = 7 if body["p_before_story_id"] else 0
            payload = []
            for index, current_id in enumerate(story_ids[offset : offset + 7], start=offset):
                row = _feed_story(index + 1, f"Private saved story {index + 1}")
                saved_at = f"2026-09-08T{11 - index:02d}:00:00Z"
                row.update(
                    story_id=current_id,
                    page_order_mode="saved_at",
                    next_cursor={"before_saved_at": saved_at, "before_story_id": current_id},
                    read_at=state["read_at"] if current_id == story_id else None,
                    saved_at=state["saved_at"] if current_id == story_id else saved_at,
                    state_revision=state["state_revision"] if current_id == story_id else 1,
                )
                if row["saved_at"]:
                    payload.append(row)
        elif request.url.endswith("/set_story_state"):
            if hold_write:
                held_writes.append(route)
                return
            writes.append(body)
            state.update(
                read_at="2026-09-08T12:01:00Z" if body["p_read"] else None,
                saved_at="2026-09-08T11:00:00Z" if body["p_saved"] else None,
                state_revision=int(state["state_revision"]) + 1,
            )
            payload = {
                "status": "updated",
                "revision": state["state_revision"],
                "read_at": state["read_at"],
                "saved_at": state["saved_at"],
            }
        elif "/user_preferences?" in request.url:
            payload = [preferences]
        elif request.url.endswith("/compare_and_swap_user_preferences"):
            if hold_preference:
                held_preferences.append(route)
                return
            assert body["new_saved_searches"][0] == {
                "id": "energy", "query": "fusion", "enabled": True
            }
            assert len(body["new_saved_searches"]) == 2
            preferences.update(
                revision=3,
                interests=body["new_interests"],
                saved_searches=body["new_saved_searches"],
                updated_at="2026-09-08T12:02:00Z",
            )
            payload = {"status": "updated", "revision": 3, "updated_at": preferences["updated_at"]}
        else:
            raise AssertionError("Unexpected dashboard endpoint")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                headless=True, channel="chrome", args=["--mute-audio"]
            )
            context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
            token = _jwt({"sub": "user-a"})
            session = {
                "access_token": token,
                "refresh_token": "refresh-token",
                "expires_at": 4_000_000_000,
                "user_id": "user-a",
            }
            context.add_init_script(
                "sessionStorage.setItem('news-curator.auth.session', JSON.stringify(%s));"
                % json.dumps(session)
            )
            context.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            page = context.new_page()
            page.goto(
                f"http://127.0.0.1:{server.server_port}/dashboard/",
                wait_until="networkidle",
            )
            page.wait_for_selector("#private-dashboard:not([hidden])")
            assert page.evaluate(
                "Object.keys(NewsCuratorReaderApi.create()).sort().join(',')"
            ) == "latestPublication,savedPage,setStoryState"
            assert page.locator(".saved-card").count() == 7
            assert page.locator("#load-saved").is_visible()
            assert page.locator("#metric-saved").inner_text() == "8"
            page.locator("#load-saved").click()
            page.wait_for_function("() => document.querySelectorAll('.saved-card').length === 8")
            assert page.locator("#load-saved").is_hidden()
            card = page.locator(f'.saved-card[data-story-id="{story_id}"]')
            card.locator(".saved-toggle").click()
            page.wait_for_function("() => document.querySelector('#dashboard-status')?.textContent.includes('updated')")
            assert card.evaluate("node => node.classList.contains('is-read')")
            original = card.locator('a:text-is("Read original")')
            assert original.get_attribute("target") == "_blank"
            assert original.get_attribute("rel") == "noopener noreferrer"
            assert writes[-1]["p_read"] is True
            card.locator(".read-action").click()
            page.wait_for_function("() => window.getComputedStyle(document.querySelector('.saved-card')).opacity === '1'")
            assert writes[-1]["p_read"] is False

            page.locator("#interest-list").fill("quantum computing\nenergy storage")
            page.locator('input[aria-label="Saved search query"]').fill("fusion")
            page.locator("#add-search").click()
            assert page.locator("#interest-list").input_value() == "quantum computing\nenergy storage"
            assert page.locator('input[aria-label="Saved search query"]').first.input_value() == "fusion"
            page.locator('input[aria-label="Saved search query"]').last.fill("space launch")
            assert page.locator("#download-view").is_disabled()
            page.locator("#save-preferences").click()
            page.wait_for_function("() => document.querySelector('#dashboard-status')?.textContent.includes('saved')")
            assert preferences["interests"] == ["quantum computing", "energy storage"]
            assert page.locator("#download-view").is_enabled()

            hold_summaries = True
            card.locator(".saved-toggle").click()
            card.locator(".saved-toggle").click()
            page.locator(".saved-card").nth(1).locator(".saved-toggle").click()
            page.wait_for_timeout(300)
            assert len(held_summaries) == 2
            held_summaries[1].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(summary_payload(10)),
            )
            page.wait_for_function("() => document.querySelector('#metric-saved')?.textContent === '10'")
            held_summaries[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(summary_payload(9)),
            )
            page.wait_for_timeout(100)
            assert page.locator("#metric-saved").inner_text() == "10"
            hold_summaries = False

            fail_summary = True
            page.locator(".saved-card").nth(2).locator(".saved-toggle").click()
            page.wait_for_function("() => document.querySelector('#dashboard-status')?.textContent.includes('Reload the dashboard')")
            assert page.locator("#download-view").is_disabled()
            fail_summary = False

            card.locator(".read-action").click()
            page.wait_for_function(
                "() => !document.querySelector('#download-view')?.disabled"
            )
            assert page.locator("#download-view").is_enabled()

            with page.expect_download() as download_info:
                page.locator("#download-view").click()
            download = download_info.value
            downloaded = tmp_path / "loaded.json"
            download.save_as(downloaded)
            snapshot = json.loads(downloaded.read_text(encoding="utf-8"))
            assert list(snapshot) == [
                "schema_version", "kind", "snapshot_at", "summary", "preferences", "saved"
            ]
            assert snapshot["saved"]["loaded_count"] == 8
            assert snapshot["saved"]["page_size"] == 7
            assert snapshot["saved"]["all_saved_loaded"] is True
            serialized = json.dumps(snapshot)
            assert "user-a" not in serialized
            assert token not in serialized

            page.locator("#your-data").scroll_into_view_if_needed()
            screenshot_dir = os.environ.get("NEWS_CURATOR_SCREENSHOT_DIR")
            for viewport in (
                {"width": 1440, "height": 1000},
                {"width": 1100, "height": 560},
                {"width": 390, "height": 844},
            ):
                page.set_viewport_size(viewport)
                if screenshot_dir:
                    label = f"{viewport['width']}x{viewport['height']}"
                    page.evaluate("scrollTo(0, 0)")
                    page.screenshot(path=str(Path(screenshot_dir) / f"dashboard-{label}-top.png"))
                    page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
                    page.screenshot(path=str(Path(screenshot_dir) / f"dashboard-{label}-bottom.png"))
                page.locator("#your-data").scroll_into_view_if_needed()
                rail = page.locator(".rail").bounding_box()
                assert rail is not None
                assert rail["y"] >= 0
                assert rail["y"] + rail["height"] <= viewport["height"]
                assert page.evaluate(
                    "document.documentElement.scrollWidth === document.documentElement.clientWidth"
                )
                nav_links = page.locator(".rail nav a:visible")
                scroll_before = page.evaluate("window.scrollY")
                nav_links.first.focus()
                for _ in range(nav_links.count() - 1):
                    page.keyboard.press("Tab")
                last_link = nav_links.last
                assert last_link.evaluate("node => document.activeElement === node")
                assert last_link.evaluate("node => node.matches(':focus-visible')")
                link_box = last_link.bounding_box()
                assert link_box is not None
                assert link_box["x"] >= rail["x"]
                assert link_box["x"] + link_box["width"] <= rail["x"] + rail["width"]
                assert link_box["y"] >= rail["y"]
                assert link_box["y"] + link_box["height"] <= rail["y"] + rail["height"]
                assert page.evaluate("window.scrollY") == scroll_before

                if viewport["width"] == 390:
                    last_link.click()
                    heading = page.locator("#your-data h2").bounding_box()
                    current_rail = page.locator(".rail").bounding_box()
                    assert heading is not None and current_rail is not None
                    assert heading["y"] >= current_rail["y"] + current_rail["height"]

            hold_write = True
            hold_preference = True
            page.locator("#interest-list").fill("unsaved private edit")
            page.locator("#save-preferences").click()
            card.locator(".save-action").click()
            page.wait_for_timeout(100)
            assert len(held_writes) == 1
            assert len(held_preferences) == 1
            page.evaluate(
                "sessionStorage.removeItem('news-curator.auth.session');"
                "dispatchEvent(new Event('news-curator:auth-changed'));"
            )
            held_writes.pop().fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "updated", "revision": 8, "read_at": None, "saved_at": None}),
            )
            held_preferences.pop().fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "updated", "revision": 4, "updated_at": "2026-09-08T12:03:00Z"}),
            )
            page.wait_for_timeout(100)
            assert page.locator("#private-dashboard").is_hidden()
            assert page.locator("#signed-out").is_visible()
            assert page.locator(".saved-card").count() == 0
            assert page.locator("#interest-list").input_value() == ""
            assert page.locator("#metric-saved").inner_text() == ""

            hold_write = False
            hold_preference = False
            page.evaluate(
                "sessionStorage.setItem('news-curator.auth.session', JSON.stringify(%s));"
                "dispatchEvent(new Event('news-curator:auth-changed'));" % json.dumps(session)
            )
            page.wait_for_selector("#private-dashboard:not([hidden])")
            for control_id in (
                "interest-list", "add-search", "save-preferences", "reload-preferences"
            ):
                assert page.locator(f"#{control_id}").is_enabled()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
