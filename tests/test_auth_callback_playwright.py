from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.build_auth_callback import materialize_callback


playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
SUPABASE_ORIGIN = "https://project-ref.supabase.co"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


def _jwt(payload: dict[str, str]) -> str:
    import base64

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"x.{encoded}.x"


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
