from __future__ import annotations

import json
import base64
import hashlib
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

from curator.identity import story_id_for_item
from curator.models import TierResult
from curator.render import render_site
from scripts.build_auth_callback import activate_personalization_link, materialize_callback
from tests.conftest import make_item


playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
SUPABASE_ORIGIN = "https://project-ref.supabase.co"


def _google_callback_location(authorize_url: str) -> tuple[str, dict[str, list[str]]]:
    query = parse_qs(urlsplit(authorize_url).query)
    assert query["provider"] == ["google"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    callback = urlsplit(query["redirect_to"][0])
    assert callback.path == "/auth/callback/"
    params = parse_qs(callback.query)
    assert len(params["client_state"][0]) == 43
    params["code"] = ["authorization-code"]
    return urlunsplit(callback._replace(query=urlencode(params, doseq=True))), query


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


def _jwt(payload: dict[str, str]) -> str:
    import base64

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"x.{encoded}.x"


def _feed_story(index: int, title: str) -> dict[str, object]:
    story_id = "story:" + f"{index:064x}"
    return {
        "story_id": story_id,
        "canonical_url": f"https://publisher.example/story-{index}",
        "title": title,
        "summary": "A server supplied summary.",
        "language": "en",
        "published_at": "2026-09-07T12:00:00Z",
        "publication_seq": 7,
        "position": index,
        "page_order_mode": "history_freshness",
        "next_cursor": {
            "before_published_at": "2026-09-07T12:00:00Z",
            "before_story_id": story_id,
        },
        "ordering_mode": "weighted_total",
        "ordering_key": {"weighted_total": 1},
        "score_components": {"freshness": 1},
        "topic_ids": ["ai"],
        "topic_ranks": {"ai": index},
        "source_kind": "outlet",
        "source_name": "Publisher",
        "ranking_explanation": "Weighted using freshness.",
        "coverage_mentions": [],
        "read_at": None,
        "saved_at": None,
        "state_revision": 0,
        "interests": [],
    }


def test_oauth_callback_is_consumed_and_scrubbed_during_page_startup(tmp_path: Path) -> None:
    site = tmp_path / "site"
    callback = site / "auth" / "callback" / "index.html"
    materialize_callback(
        supabase_url=SUPABASE_ORIGIN,
        publishable_key="sb_publishable_test",
        output=callback,
    )
    (site / "auth" / "client.js").write_bytes(
        (ROOT / "static" / "auth" / "client.js").read_bytes()
    )
    (site / "auth" / "styles.css").write_bytes(
        (ROOT / "static" / "auth" / "styles.css").read_bytes()
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls: list[tuple[str, dict]] = []
    fail_exchange = False

    def fulfill(route: object) -> None:
        request = route.request
        calls.append((request.url, request.post_data_json if request.post_data else {}))
        if "/auth/v1/token?grant_type=pkce" in request.url:
            if fail_exchange:
                route.fulfill(
                    status=400,
                    content_type="application/json",
                    body=json.dumps({
                        "error": "invalid_grant",
                        "error_description": "sensitive provider detail",
                    }),
                )
                return
            payload: object = {
                "access_token": _jwt({"sub": "user-a"}),
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "user": {"id": "user-a"},
            }
        elif "/rest/v1/user_preferences" in request.url:
            payload = [{
                "user_id": "user-a",
                "revision": 1,
                "locale": "en",
                "interests": ["agents"],
                "saved_searches": [],
                "created_at": "2026-09-07T12:00:00Z",
                "updated_at": "2026-09-07T12:00:00Z",
            }]
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(
                """
                sessionStorage.setItem('news-curator.auth.state', 'expected-state');
                sessionStorage.setItem('news-curator.auth.verifier', 'expected-verifier');
                """
            )
            page = context.new_page()
            page.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            url = (
                f"http://127.0.0.1:{server.server_port}/auth/callback/"
                "?code=authorization-code&client_state=expected-state"
            )
            page.goto(url, wait_until="networkidle")

            assert page.url == f"http://127.0.0.1:{server.server_port}/auth/callback/"
            page.locator("#preferences-panel").wait_for(state="visible")
            assert page.locator("#status").inner_text() == "Signed in. Your interests are ready."
            assert page.locator("#interests").input_value() == "agents"
            assert calls[0] == (
                f"{SUPABASE_ORIGIN}/auth/v1/token?grant_type=pkce",
                {"auth_code": "authorization-code", "code_verifier": "expected-verifier"},
            )
            assert page.evaluate(
                "[sessionStorage.getItem('news-curator.auth.state'), "
                "sessionStorage.getItem('news-curator.auth.verifier')]"
            ) == [None, None]

            fail_exchange = True
            failed = context.new_page()
            failed.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            failed.goto(url, wait_until="networkidle")
            assert failed.url == f"http://127.0.0.1:{server.server_port}/auth/callback/"
            failed.locator("#login-panel").wait_for(state="visible")
            assert failed.locator("#status").inner_text() == "Sign in failed. Try again."
            assert "sensitive provider detail" not in failed.locator("body").inner_text()
            assert failed.evaluate(
                "[sessionStorage.getItem('news-curator.auth.state'), "
                "sessionStorage.getItem('news-curator.auth.verifier'), "
                "sessionStorage.getItem('news-curator.auth.session')]"
            ) == [None, None, None]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_back_to_digest_keeps_session_without_broadcast_channel(tmp_path: Path) -> None:
    site = tmp_path / "site"
    callback = site / "auth" / "callback" / "index.html"
    materialize_callback(
        supabase_url=SUPABASE_ORIGIN,
        publishable_key="sb_publishable_test",
        output=callback,
    )
    (site / "auth" / "client.js").write_bytes(
        (ROOT / "static" / "auth" / "client.js").read_bytes()
    )
    (site / "auth" / "styles.css").write_bytes(
        (ROOT / "static" / "auth" / "styles.css").read_bytes()
    )
    (site / "index.html").write_text(
        """<!doctype html><html><body>
        <p id="session-available"></p>
        <script>
        document.getElementById("session-available").textContent =
          sessionStorage.getItem("news-curator.auth.session") ? "available" : "missing";
        </script></body></html>""",
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def fulfill(route: object) -> None:
        request = route.request
        if "/auth/v1/token?grant_type=pkce" in request.url:
            payload: object = {
                "access_token": _jwt({"sub": "user-a"}),
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "user": {"id": "user-a"},
            }
        elif "/rest/v1/user_preferences" in request.url:
            payload = []
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(
                """
                Object.defineProperty(window, "BroadcastChannel", {
                  configurable: false, value: undefined
                });
                sessionStorage.setItem("news-curator.auth.state", "expected-state");
                sessionStorage.setItem("news-curator.auth.verifier", "expected-verifier");
                """
            )
            page = context.new_page()
            page.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            page.goto(
                f"http://127.0.0.1:{server.server_port}/auth/callback/"
                "?code=authorization-code&client_state=expected-state",
                wait_until="networkidle",
            )
            page.locator("#preferences-panel").wait_for(state="visible")
            assert page.evaluate("typeof BroadcastChannel") == "undefined"
            assert len(context.pages) == 1

            page.get_by_text("Back to the digest", exact=True).click()
            page.wait_for_url(f"http://127.0.0.1:{server.server_port}/")

            assert len(context.pages) == 1
            assert page.locator("#session-available").inner_text() == "available"
            assert page.evaluate(
                "sessionStorage.getItem('news-curator.auth.session') !== null"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("returning_user", [False, True])
def test_google_button_pkce_login_and_reload_for_new_and_returning_users(
    tmp_path: Path, returning_user: bool
) -> None:
    """Local OAuth contract. Real Google consent is a separate production gate."""
    site = tmp_path / "site"
    materialize_callback(
        supabase_url=SUPABASE_ORIGIN,
        publishable_key="sb_publishable_test",
        output=site / "auth/callback/index.html",
    )
    for asset in ("client.js", "styles.css"):
        (site / "auth" / asset).write_bytes((ROOT / "static/auth" / asset).read_bytes())
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    authorize_query: dict[str, list[str]] = {}
    exchanges = 0

    def fulfill(route: object) -> None:
        nonlocal authorize_query, exchanges
        request = route.request
        if "/auth/v1/authorize?" in request.url:
            location, authorize_query = _google_callback_location(request.url)
            assert urlsplit(location).netloc == f"127.0.0.1:{server.server_port}"
            route.fulfill(status=302, headers={"location": location}, body="")
            return
        if "/auth/v1/token?grant_type=pkce" in request.url:
            exchanges += 1
            body = request.post_data_json
            assert body["auth_code"] == "authorization-code"
            digest = hashlib.sha256(body["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == authorize_query["code_challenge"][0]
            payload: object = {
                "access_token": _jwt({"sub": "user-a"}), "refresh_token": "refresh-token",
                "expires_in": 3600, "user": {"id": "user-a"},
                "provider_token": "do-not-retain-provider-token",
            }
        elif "/rest/v1/user_preferences" in request.url:
            payload = [{
                "user_id": "user-a", "revision": 1, "locale": "en",
                "interests": ["agents"], "saved_searches": [],
                "created_at": "2026-09-07T12:00:00Z", "updated_at": "2026-09-07T12:00:00Z",
            }] if returning_user else []
        else:
            raise AssertionError("Unexpected auth contract endpoint")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.route(f"{SUPABASE_ORIGIN}/**", fulfill)
                page.goto(f"http://127.0.0.1:{server.server_port}/auth/callback/", wait_until="networkidle")
                assert page.locator('input[type="email"], #send-code, #verify-code').count() == 0
                google = page.get_by_role("button", name="Sign in with Google", exact=True)
                google.focus()
                page.keyboard.press("Enter")
                page.locator("#preferences-panel").wait_for(state="visible")
                assert page.url == f"http://127.0.0.1:{server.server_port}/auth/callback/"
                assert page.locator("#interests").input_value() == ("agents" if returning_user else "")
                assert exchanges == 1
                assert page.evaluate("Object.keys(JSON.parse(sessionStorage.getItem('news-curator.auth.session'))).sort()") == [
                    "access_token", "expires_at", "refresh_token", "user_id",
                ]
                page.reload(wait_until="networkidle")
                page.locator("#preferences-panel").wait_for(state="visible")
                assert page.locator("#interests").input_value() == ("agents" if returning_user else "")
                assert exchanges == 1
                assert page.locator("#google-sign-in").is_hidden()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("callback_suffix", [
    "?error=access_denied&error_description=private-provider-detail",
    "#error=access_denied&error_description=private-provider-detail",
    "#access_token=private-provider-token&refresh_token=private-provider-refresh",
])
def test_google_callback_errors_are_scrubbed_and_retryable(tmp_path: Path, callback_suffix: str) -> None:
    site = tmp_path / "site"
    materialize_callback(
        supabase_url=SUPABASE_ORIGIN, publishable_key="sb_publishable_test",
        output=site / "auth/callback/index.html",
    )
    for asset in ("client.js", "styles.css"):
        (site / "auth" / asset).write_bytes((ROOT / "static/auth" / asset).read_bytes())
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                calls = []
                page.route(f"{SUPABASE_ORIGIN}/**", lambda route: (calls.append(True), route.abort()))
                base = f"http://127.0.0.1:{server.server_port}/auth/callback/"
                page.goto(base + callback_suffix, wait_until="networkidle")
                assert page.url == base
                assert page.locator("#status").inner_text() == "Sign in failed. Try again."
                assert page.get_by_role("button", name="Sign in with Google", exact=True).is_enabled()
                assert "private-provider" not in page.locator("body").inner_text()
                assert calls == []
                assert page.evaluate("sessionStorage.getItem('news-curator.auth.session')") is None
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("logout_failure", [None, "500", "redirect", "network"])
def test_profile_logout_always_clears_private_digest_state_across_tabs(
    tmp_path: Path, now: object, logout_failure: str | None
) -> None:
    site = tmp_path / "site"
    item = make_item("Static public story")
    item.description = (
        "The publisher supplied a complete public summary with enough context for a reader. "
        "This static edition card remains safe to show after the personalized session ends."
    )
    render_site(
        {"AI": [item]},
        [TierResult(tier="rss", items=[], ok=True)],
        now,
        site,
        topic_ids_by_name={"AI": "ai"},
    )
    activate_personalization_link(
        site / "index.html",
        supabase_url=SUPABASE_ORIGIN,
        publishable_key="sb_publishable_test",
    )
    materialize_callback(
        supabase_url=SUPABASE_ORIGIN,
        publishable_key="sb_publishable_test",
        output=site / "auth" / "callback" / "index.html",
    )
    (site / "auth" / "styles.css").write_bytes(
        (ROOT / "static" / "auth" / "styles.css").read_bytes()
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logged_out = False
    calls: list[dict[str, object]] = []
    static_id = story_id_for_item(item)

    def fulfill(route: object) -> None:
        nonlocal logged_out
        request = route.request
        headers = request.headers
        calls.append({
            "url": request.url,
            "authorization": headers.get("authorization"),
            "after_logout": logged_out,
        })
        body = request.post_data_json if request.post_data else {}
        status = 200
        if request.url.endswith("/latest_publication"):
            payload: object = {
                "publication_seq": 7,
                "finalized_at": "2026-09-07T12:00:00Z",
                "topics": [{"topic_id": "ai", "name": "AI"}],
                "initial_history_cursor": None,
                "poll_seconds": 30,
                "page_size": 10,
            }
        elif request.url.endswith("/feed_page"):
            assert body["p_topic_id"] is None
            public_row = _feed_story(50, "Anonymous public story")
            if headers.get("authorization"):
                static_row = _feed_story(1, "Static public story")
                static_row.update({
                    "story_id": static_id,
                    "read_at": "2026-09-07T12:01:00Z",
                    "saved_at": "2026-09-07T12:02:00Z",
                    "state_revision": 4,
                    "interests": [{"topic_id": "ai", "signal": "more_like", "revision": 3}],
                })
                private_ranked = _feed_story(51, "Private interest-ranked story")
                private_ranked.update({
                    "ordering_mode": "preference_then_freshness",
                    "ranking_explanation": "Private interest: confidential topic.",
                })
                payload = [static_row, public_row, private_ranked]
            else:
                payload = [public_row]
        elif request.url.endswith("/saved_page"):
            saved = _feed_story(777, "Private saved-only story")
            saved.update({
                "page_order_mode": "saved_at",
                "next_cursor": {
                    "before_saved_at": "2026-09-07T12:03:00Z",
                    "before_story_id": saved["story_id"],
                },
                "read_at": "2026-09-07T12:01:00Z",
                "saved_at": "2026-09-07T12:03:00Z",
                "state_revision": 5,
                "interests": [{"topic_id": "ai", "signal": "more_like", "revision": 2}],
            })
            payload = [saved]
        elif "/auth/v1/authorize?" in request.url:
            location, _query = _google_callback_location(request.url)
            route.fulfill(status=302, headers={"location": location}, body="")
            return
        elif request.url.endswith("/auth/v1/token?grant_type=pkce"):
            payload = {
                "access_token": _jwt({"sub": "user-a"}),
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "user": {"id": "user-a"},
            }
        elif "/rest/v1/user_preferences" in request.url:
            payload = [{
                "user_id": "user-a",
                "revision": 1,
                "locale": "en",
                "interests": ["confidential topic"],
                "saved_searches": [],
                "created_at": "2026-09-07T12:00:00Z",
                "updated_at": "2026-09-07T12:00:00Z",
            }]
        elif request.url.endswith("/auth/v1/logout"):
            logged_out = True
            if logout_failure == "network":
                route.abort("connectionfailed")
                return
            if logout_failure == "redirect":
                route.fulfill(
                    status=302,
                    headers={"location": "https://attacker.invalid/capture"},
                    body="",
                )
                return
            payload = {"error": "sensitive remote detail"} if logout_failure == "500" else None
            status = 500 if logout_failure == "500" else 204
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.route(f"{SUPABASE_ORIGIN}/**", fulfill)
            digest = context.new_page()
            digest.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")

            with context.expect_page() as profile_info:
                digest.locator(".profile-link").click()
            profile = profile_info.value
            profile.wait_for_load_state("networkidle")
            profile.get_by_role("button", name="Sign in with Google", exact=True).click()
            profile.locator("#preferences-panel").wait_for(state="visible")
            digest.wait_for_function(
                "() => sessionStorage.getItem('news-curator.auth.session') !== null"
            )
            digest.get_by_text("Private interest-ranked story", exact=True).wait_for()

            digest.locator('.chip[data-filter="__saved__"]').first.click()
            digest.get_by_text("Private saved-only story", exact=True).wait_for()
            assert digest.locator("article.is-saved").count() >= 1
            assert digest.get_by_text("Unsave", exact=True).count() >= 1

            for forged in (
                {"type": "logout", "extra": True},
                {"type": "logout", "session": None},
                {"type": "session"},
            ):
                profile.evaluate(
                    "message => { const channel = new BroadcastChannel('news-curator.auth.v1'); "
                    "channel.postMessage(message); channel.close(); }",
                    forged,
                )
            digest.wait_for_timeout(100)
            assert digest.evaluate(
                "sessionStorage.getItem('news-curator.auth.session') !== null"
            )
            assert digest.get_by_text("Private saved-only story", exact=True).is_visible()

            digest.evaluate(
                """
                window.__logoutDisabledObserved = false;
                new MutationObserver(() => {
                  const buttons = [...document.querySelectorAll('.state-action')];
                  if (sessionStorage.getItem('news-curator.auth.session') === null &&
                      buttons.length > 0 && buttons.every(button => button.disabled)) {
                    window.__logoutDisabledObserved = true;
                  }
                }).observe(document.body, {attributes: true, subtree: true});
                """
            )
            profile.locator("#sign-out").click()
            profile_status = (
                "Signed out."
                if logout_failure is None
                else "Signed out locally. Remote sign-out could not be confirmed."
            )
            profile.get_by_text(profile_status, exact=True).wait_for()
            digest.wait_for_function(
                "() => sessionStorage.getItem('news-curator.auth.session') === null"
            )
            digest.get_by_text("Signed out. Public stories are ready.", exact=True).wait_for()

            assert digest.evaluate("window.__logoutDisabledObserved") is True
            assert digest.locator('.chip[data-filter="__all__"]').first.get_attribute("aria-pressed") == "true"
            assert digest.get_by_text("Private saved-only story", exact=True).count() == 0
            assert digest.get_by_text("Private interest-ranked story", exact=True).count() == 0
            assert digest.locator(".is-read, .is-saved, .is-more-like, .is-less-like").count() == 0
            assert digest.get_by_text("Unsave", exact=True).count() == 0
            assert digest.get_by_text("Mark unread", exact=True).count() == 0
            assert digest.get_by_text("More like this added", exact=True).count() == 0
            assert digest.get_by_text("confidential topic", exact=False).count() == 0
            assert digest.get_by_text(
                "Remote sign-out could not be confirmed.", exact=False
            ).count() == 0
            assert "sensitive remote detail" not in profile.locator("body").inner_text()

            writes_before = len([
                call for call in calls
                if str(call["url"]).endswith(("/set_story_state", "/set_story_interest"))
            ])
            digest.locator("article.card", has_text="Anonymous public story").locator(
                ".save-action"
            ).evaluate("button => button.click()")
            digest.get_by_text("Sign in to sync reading controls.", exact=True).wait_for()
            writes_after = len([
                call for call in calls
                if str(call["url"]).endswith(("/set_story_state", "/set_story_interest"))
            ])
            assert writes_after == writes_before
            assert all(
                call["authorization"] is None
                for call in calls
                if call["after_logout"] and "/rest/v1/rpc/" in str(call["url"])
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
