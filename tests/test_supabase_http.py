import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

import pytest

from curator.recommendation.supabase_http import (
    SupabaseAuthenticationError,
    SupabaseHTTP,
    _NoRedirect,
)


@pytest.mark.allow_socket
def test_no_redirect_handler_does_not_forward_bearer_to_redirect_target():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/start",
            headers={"Authorization": "Bearer canary"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.build_opener(_NoRedirect).open(request, timeout=1)
        assert error.value.code == 302
        assert seen == [("/start", "Bearer canary")]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_supabase_origin_is_exact_https_origin():
    for origin in ("http://example.test", "https://user@example.test", "https://example.test/path",
                   "https://example.test?query=1", "https://example.test/#fragment"):
        with pytest.raises(ValueError, match="fixed HTTPS origin"):
            SupabaseHTTP(origin=origin, publishable_key="public", service_role_key="service")


def test_auth_401_maps_to_safe_authentication_error():
    client = SupabaseHTTP(origin="https://example.test", publishable_key="public", service_role_key="service")

    class Reject:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())

    client._opener = Reject()
    with pytest.raises(SupabaseAuthenticationError, match="rejected the user session"):
        client.get_user("expired-token")


@pytest.mark.parametrize(("key", "expected"), [("sb_secret_canary", None), ("legacy.jwt", "Bearer legacy.jwt")])
def test_service_key_type_controls_bearer_header_without_affecting_apikey(key, expected):
    client = SupabaseHTTP(origin="https://example.test", publishable_key="public", service_role_key=key)
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b"true"

    class Capture:
        def open(self, request, timeout):
            seen.update(dict(request.header_items()))
            return Response()

    client._opener = Capture()
    assert client.reserve_budget(user_id="owner", request_id="request", amount_usd=.01, daily_limit_usd=2)
    assert seen["Apikey"] == key
    assert seen.get("Authorization") == expected
