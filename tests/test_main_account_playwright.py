"""Full rendered main-page OAuth contract. Provider transport is local, not live proof."""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest

from curator.identity import story_id_for_item
from curator.models import TierResult
from curator.render import render_site
from scripts.build_auth_callback import activate_personalization_link, materialize_callback
from tests.conftest import make_item
from tests.test_auth_callback_playwright import (
    ROOT, SUPABASE_ORIGIN, _QuietHandler, _feed_story, _google_callback_location, _jwt, playwright_api,
)


@pytest.mark.parametrize("early_click", [False, True], ids=["ready", "deferred-auth"])
def test_main_google_account_save_reload_and_tab_boundary(tmp_path: Path, now, early_click: bool) -> None:
    site = tmp_path / "site"
    item = make_item("Static public story")
    item.description = "The publisher supplied a complete summary with enough context for a reader."
    render_site({"AI": [item]}, [TierResult(tier="rss", items=[], ok=True)], now, site,
                topic_ids_by_name={"AI": "ai"})
    activate_personalization_link(site / "index.html", supabase_url=SUPABASE_ORIGIN,
                                  publishable_key="sb_publishable_test")
    materialize_callback(supabase_url=SUPABASE_ORIGIN, publishable_key="sb_publishable_test",
                         output=site / "auth/callback/index.html")
    (site / "auth/styles.css").write_bytes((ROOT / "static/auth/styles.css").read_bytes())
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {"read_at": None, "saved_at": None, "state_revision": 0}
    authorize_calls = 0
    authenticated_writes = 0
    refresh_calls = 0
    feed_unavailable = False
    feed_reads = 0
    static_id = story_id_for_item(item)

    def fulfill(route):
        nonlocal authorize_calls, authenticated_writes, refresh_calls, feed_reads
        request = route.request
        body = request.post_data_json if request.post_data else {}
        if "/auth/v1/authorize?" in request.url:
            authorize_calls += 1
            location, _query = _google_callback_location(request.url)
            route.fulfill(status=302, headers={"location": location}, body="")
            return
        if "/auth/v1/token?grant_type=" in request.url:
            refreshing = "grant_type=refresh_token" in request.url
            if refreshing:
                refresh_calls += 1
            payload = {"access_token": _jwt({"sub": "user-a", "email": "reader@example.test", "version": str(refresh_calls)}),
                       "refresh_token": "refresh-two" if refreshing else "refresh-token",
                       "expires_in": 3600, "user": {"id": "user-a"}}
        elif request.url.endswith("/latest_publication"):
            payload = {"publication_seq": 7, "finalized_at": "2026-09-07T12:00:00Z",
                       "topics": [{"topic_id": "ai", "name": "AI"}], "initial_history_cursor": None,
                       "poll_seconds": 30, "page_size": 10}
        elif request.url.endswith(("/feed_page", "/saved_page")):
            feed_reads += 1
            if feed_unavailable:
                route.abort("connectionfailed")
                return
            row = {**_feed_story(1, item.title), "story_id": static_id}
            if request.headers.get("authorization"):
                row.update(state)
            payload = [row]
        elif request.url.endswith("/set_story_state"):
            assert request.headers.get("authorization", "").startswith("Bearer ")
            authenticated_writes += 1
            state.update(read_at="2026-09-07T12:01:00Z" if body["p_read"] else None,
                         saved_at="2026-09-07T12:02:00Z" if body["p_saved"] else None,
                         state_revision=state["state_revision"] + 1)
            payload = {"status": "updated", "revision": state["state_revision"],
                       "read_at": state["read_at"], "saved_at": state["saved_at"]}
        elif "/user_preferences" in request.url:
            payload = []
        elif request.url.endswith("/auth/v1/logout"):
            payload = None
        else:
            raise AssertionError("Unexpected main account contract endpoint")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, channel="chrome", args=["--mute-audio"])
            context = browser.new_context()
            context.add_init_script("""if(window.speechSynthesis) speechSynthesis.speak=()=>{};
                HTMLMediaElement.prototype.play=()=>Promise.resolve();""")
            context.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            page = context.new_page()
            origin = f"http://127.0.0.1:{server.server_port}"
            held_scripts = []
            if early_click:
                def hold_first_script(route):
                    if not held_scripts:
                        held_scripts.append(route)
                    else:
                        route.continue_()
                page.route(origin + "/auth/client.js", hold_first_script)
            page.goto(origin, wait_until="commit" if early_click else "networkidle")
            page.locator(".profile-link").click()
            page.wait_for_function("() => document.querySelector('.profile-link')?.textContent.includes('Signed in')", timeout=5000)
            assert len(context.pages) == 1
            assert page.url == origin + "/"
            assert authorize_calls == 1
            assert "reader@example.test" in page.locator(".profile-link").inner_text()
            card = page.locator(f'[data-story-id="{static_id}"]')
            card.locator(".accordion-toggle").click()
            page.wait_for_function("() => document.querySelector('.card')?.dataset.stateRevision === '1'")
            card.locator(".save-action").click()
            page.wait_for_function("() => document.querySelector('.card')?.classList.contains('is-saved')")
            assert authenticated_writes == 2
            page.reload(wait_until="networkidle")
            assert card.evaluate("card=>card.classList.contains('is-saved')")
            page.evaluate("""() => {
              const key='news-curator.auth.session';
              const session=JSON.parse(sessionStorage.getItem(key));
              session.expires_at=Math.floor(Date.now()/1000)-1;
              sessionStorage.setItem(key,JSON.stringify(session));
              document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_function("() => document.querySelector('.profile-link')?.dataset.signedIn === 'true' && JSON.parse(sessionStorage.getItem('news-curator.auth.session')).refresh_token === 'refresh-two'")
            assert refresh_calls == 1
            feed_unavailable = True
            page.reload(wait_until="networkidle")
            page.get_by_role("link", name="Check sign-in again", exact=True).wait_for()
            assert page.evaluate("NewsCuratorAuth.isConfirmed()") is False
            feed_unavailable = False
            page.locator(".profile-link").click()
            page.wait_for_function("() => document.querySelector('.profile-link')?.dataset.signedIn === 'true'")
            settled_reads = feed_reads
            page.wait_for_timeout(500)
            assert feed_reads - settled_reads <= 1
            fresh = context.new_page()
            fresh.goto(origin, wait_until="networkidle")
            fresh.evaluate("sessionStorage.setItem('news-curator.auth.session', '{invalid')")
            fresh.reload(wait_until="networkidle")
            assert "Signed in" not in fresh.locator(".profile-link").inner_text()
            assert not fresh.locator(f'[data-story-id="{static_id}"]').evaluate("card=>card.classList.contains('is-saved')")
            fresh.close()
            page.locator(".profile-link").click()
            page.wait_for_url(origin + "/auth/callback/")
            assert len(context.pages) == 1
            page.locator("#sign-out").click()
            page.locator(".back-link").click()
            page.wait_for_url(origin + "/")
            assert not card.evaluate("card=>card.classList.contains('is-saved')")
            page.reload(wait_until="networkidle")
            assert "Signed in" not in page.locator(".profile-link").inner_text()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
