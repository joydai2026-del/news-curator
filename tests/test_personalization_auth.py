import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from curator.personalization.auth import (
    AgentAuth,
    AuthConfig,
    AuthError,
    MacOSKeychainStorage,
    MemoryTokenStorage,
    Session,
    UrlLibTransport,
)
from curator.personalization.preferences import JsonRestTransport


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = "sb_publishable_test"


def jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"x.{encoded}.x"


def assert_exception_graph_omits(error: BaseException, *sentinels: str) -> None:
    pending: list[object] = [error]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, BaseException):
            rendered.extend((str(value), repr(value)))
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.extend(vars(value).values())
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            pending.extend(value)
        elif isinstance(value, bytes):
            rendered.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            rendered.append(value)
    combined = "\n".join(rendered)
    for sentinel in sentinels:
        assert sentinel not in combined


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, *, headers, body=None, timeout=15.0):
        self.calls.append({"url": url, "headers": dict(headers), "body": dict(body or {})})
        return self.responses.pop(0)


def auth_with(responses: list[tuple[int, dict]], now: float = 1000):
    store = MemoryTokenStorage()
    transport = FakeTransport(responses)
    auth = AgentAuth(AuthConfig("https://example.supabase.co", PUBLIC_KEY), store, transport=transport, clock=lambda: now)
    return auth, store, transport


def login_payload(*, access=None, refresh="refresh-one", user="user-a") -> dict:
    payload = {
        "access_token": access or jwt({"sub": user}),
        "refresh_token": refresh,
        "expires_in": 3600,
        "user": {"id": user},
    }
    return payload


def callback(attempt, *, state=None, port=43123, code="authorization-code") -> str:
    return f"http://127.0.0.1:{port}/callback?code={code}&client_state={state or attempt.state}"


def test_pkce_login_uses_exact_redirect_state_and_single_use(capsys) -> None:
    auth, store, transport = auth_with([])
    attempt, authorize = auth.begin_login("http://127.0.0.1:43123/callback")
    query = parse_qs(urlsplit(authorize).query)
    redirect = urlsplit(query["redirect_to"][0])
    assert (redirect.scheme, redirect.hostname, redirect.port, redirect.path) == (
        "http",
        "127.0.0.1",
        43123,
        "/callback",
    )
    assert parse_qs(redirect.query) == {"client_state": [attempt.state]}
    assert query["code_challenge_method"] == ["S256"]
    assert "state" not in query
    transport.responses.append((200, login_payload()))
    session = auth.finish_login(attempt, callback(attempt))
    assert session.user_id == "user-a"
    assert store.load() is session
    assert transport.calls[0]["body"]["code_verifier"] == attempt.verifier
    output = capsys.readouterr()
    assert "access-one" not in output.out + output.err
    assert "refresh-one" not in output.out + output.err
    with pytest.raises(AuthError, match="already used"):
        attempt.consume_callback(callback(attempt))


def test_invalid_state_and_wrong_redirect_fail_before_transport() -> None:
    auth, _, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    with pytest.raises(AuthError, match="could not be verified"):
        auth.finish_login(attempt, callback(attempt, state="wrong"))
    assert transport.calls == []

    other, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    with pytest.raises(AuthError, match="redirect was invalid"):
        auth.finish_login(other, callback(other, port=43124))
    assert transport.calls == []


def test_access_token_subject_mismatch_fails_without_saving() -> None:
    auth, store, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    transport.responses.append((200, login_payload(access=jwt({"sub": "user-b"}))))
    with pytest.raises(AuthError, match="authentication response"):
        auth.finish_login(attempt, callback(attempt))
    assert store.load() is None


@pytest.mark.parametrize("access_token", [jwt({}), "not-a-jwt"])
def test_access_token_requires_decodable_subject(access_token) -> None:
    auth, store, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    transport.responses.append((200, login_payload(access=access_token)))

    with pytest.raises(AuthError, match="authentication response"):
        auth.finish_login(attempt, callback(attempt))
    assert store.load() is None


def test_provider_id_token_is_not_required_for_pkce_success() -> None:
    auth, store, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    transport.responses.append((200, login_payload()))

    session = auth.finish_login(attempt, callback(attempt))

    assert session.user_id == "user-a"
    assert store.load() is session


@pytest.mark.parametrize("user", [None, "", "x" * 257])
def test_login_requires_a_bounded_user_identity_before_persisting(user) -> None:
    auth, store, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    payload = login_payload()
    if user is None:
        payload.pop("user")
    else:
        payload["user"] = {"id": user}
    transport.responses.append((200, payload))

    with pytest.raises(AuthError, match="authentication response"):
        auth.finish_login(attempt, callback(attempt))
    assert store.load() is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"access_token": "x" * 16_385},
        {"refresh_token": "x" * 16_385},
        {"expires_in": True},
        {"expires_in": float("nan")},
        {"expires_at": 87_401, "expires_in": None},
    ],
    ids=(
        "oversized-access-token",
        "oversized-refresh-token",
        "boolean-relative-expiry",
        "non-finite-relative-expiry",
        "absolute-expiry-over-24-hours",
    ),
)
def test_initial_login_rejects_invalid_session_bounds_before_persisting(mutation) -> None:
    auth, store, transport = auth_with([])
    attempt, _ = auth.begin_login("http://127.0.0.1:43123/callback")
    payload = login_payload()
    payload.update(mutation)
    transport.responses.append((200, payload))

    with pytest.raises(AuthError, match="authentication response"):
        auth.finish_login(attempt, callback(attempt))

    assert store.load() is None


def test_protected_session_json_round_trips_the_exact_persisted_shape() -> None:
    session = Session(
        "access-one",
        "refresh-one",
        expires_at=2000,
        user_id="user-a",
    )

    assert Session.from_json(session.to_json()) == session


@pytest.mark.parametrize("user_id", [None, "", "x" * 257])
def test_protected_session_json_requires_a_bounded_user_identity(user_id) -> None:
    value = json.dumps(
        {
            "access_token": "access-one",
            "refresh_token": "refresh-one",
            "expires_at": 2000,
            "user_id": user_id,
        }
    )

    with pytest.raises(AuthError, match="Protected session data was invalid"):
        Session.from_json(value)


def test_redirect_validation_rejects_non_loopback_or_extra_path() -> None:
    auth, _, _ = auth_with([])
    for redirect in (
        "http://localhost:43123/callback",
        "https://127.0.0.1:43123/callback",
        "http://127.0.0.1:43123/other",
        "http://127.0.0.1:43123/callback?next=x",
    ):
        with pytest.raises(ValueError, match="Redirect"):
            auth.begin_login(redirect)


def test_transports_ignore_environment_proxy_and_netrc_sentinels(monkeypatch, capsys) -> None:
    sentinels = {
        "HTTP_PROXY": "http://proxy-user:proxy-secret@proxy.invalid:8080",
        "HTTPS_PROXY": "http://proxy-user:proxy-secret@proxy.invalid:8443",
        "ALL_PROXY": "socks5://proxy-user:proxy-secret@proxy.invalid:1080",
        "NETRC": "/private/tmp/netrc-secret-sentinel",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)

    for transport in (UrlLibTransport(), JsonRestTransport()):
        proxy_handlers = [
            handler for handler in transport._opener.handlers if isinstance(handler, urllib.request.ProxyHandler)
        ]
        assert transport._proxy_handler.proxies == {}
        assert proxy_handlers == []
        assert not any(
            isinstance(
                handler,
                (
                    urllib.request.HTTPCookieProcessor,
                    urllib.request.HTTPBasicAuthHandler,
                    urllib.request.ProxyBasicAuthHandler,
                ),
            )
            for handler in transport._opener.handlers
        )
        handler_state = repr([vars(handler) for handler in transport._opener.handlers])
        assert "proxy-secret" not in handler_state
        assert "netrc-secret-sentinel" not in handler_state

    output = capsys.readouterr()
    assert "proxy-secret" not in output.out + output.err
    assert "netrc-secret-sentinel" not in output.out + output.err


def test_refresh_rotates_tokens_and_expiry_triggers_refresh() -> None:
    auth, store, transport = auth_with([(200, login_payload(access="access-two", refresh="refresh-two"))])
    store.save(Session("access-one", "refresh-one", expires_at=1020, user_id="user-a"))
    rotated = auth.valid_session(leeway=30)
    assert rotated.access_token == "access-two"
    assert rotated.refresh_token == "refresh-two"
    assert store.load() is rotated
    assert transport.calls[0]["body"] == {"refresh_token": "refresh-one"}


def test_refresh_accepts_same_user_rotated_token_and_bounded_absolute_expiry() -> None:
    payload = login_payload(access="access-two", refresh="refresh-two", user="user-a")
    payload.pop("expires_in")
    payload["expires_at"] = 1100
    auth, store, _ = auth_with([(200, payload)])
    store.save(Session("access-one", "refresh-one", expires_at=900, user_id="user-a"))

    rotated = auth.refresh()

    assert rotated == Session("access-two", "refresh-two", expires_at=1100, user_id="user-a")
    assert store.load() is rotated


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "access-two", "refresh_token": "refresh-two", "expires_in": 3600},
        login_payload(access="access-two", refresh="refresh-two", user="user-b"),
        login_payload(access="access-two", refresh="refresh-one", user="user-a"),
        {"access_token": "access-two", "expires_in": 3600, "user": {"id": "user-a"}},
        {"refresh_token": "refresh-two", "expires_in": 3600, "user": {"id": "user-a"}},
        login_payload(access="x" * 16_385, refresh="refresh-two", user="user-a"),
        login_payload(access="access-two", refresh="x" * 16_385, user="user-a"),
        {"access_token": "access-two", "refresh_token": "refresh-two", "user": {"id": "user-a"}},
        {
            "access_token": "access-two",
            "refresh_token": "refresh-two",
            "expires_at": 1000,
            "user": {"id": "user-a"},
        },
        {
            "access_token": "access-two",
            "refresh_token": "refresh-two",
            "expires_at": 87401,
            "user": {"id": "user-a"},
        },
        {
            "access_token": "access-two",
            "refresh_token": "refresh-two",
            "expires_in": True,
            "user": {"id": "user-a"},
        },
        {
            "access_token": "access-two",
            "refresh_token": "refresh-two",
            "expires_in": float("nan"),
            "user": {"id": "user-a"},
        },
    ],
    ids=(
        "missing-user",
        "different-user",
        "unchanged-refresh-token",
        "missing-refresh-token",
        "missing-access-token",
        "oversized-access-token",
        "oversized-refresh-token",
        "missing-expiry",
        "expired-absolute-expiry",
        "absolute-expiry-over-24-hours",
        "boolean-relative-expiry",
        "non-finite-relative-expiry",
    ),
)
def test_invalid_refresh_response_erases_session_without_exception_chaining(payload) -> None:
    auth, store, _ = auth_with([(200, payload)])
    store.save(Session("access-one", "refresh-one", expires_at=900, user_id="user-a"))

    with pytest.raises(AuthError, match="Session refresh failed") as caught:
        auth.refresh()

    assert store.load() is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "access-one", "refresh-one", "access-two", "refresh-two")


def test_refresh_transport_failure_erases_session_and_drops_token_bearing_exception() -> None:
    class TokenBearingFailureTransport:
        def post(self, url, *, headers, body=None, timeout=15.0):
            raise RuntimeError(f"upstream rejected {body['refresh_token']}")

    store = MemoryTokenStorage()
    store.save(Session("access-one", "refresh-one", expires_at=900, user_id="user-a"))
    auth = AgentAuth(
        AuthConfig("https://example.supabase.co", PUBLIC_KEY),
        store,
        transport=TokenBearingFailureTransport(),
        clock=lambda: 1000,
    )

    with pytest.raises(AuthError, match="Session refresh failed") as caught:
        auth.refresh()

    assert store.load() is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "access-one", "refresh-one")


@pytest.mark.parametrize(
    "current",
    [
        Session("access-one", "refresh-one", expires_at=900, user_id=None),
        Session("access-one", "refresh-one", expires_at=900, user_id=""),
        Session("", "refresh-one", expires_at=900, user_id="user-a"),
        Session("access-one", "", expires_at=900, user_id="user-a"),
        Session("access-one", "refresh-one", expires_at=float("nan"), user_id="user-a"),
    ],
)
def test_refresh_rejects_invalid_saved_session_before_http_and_erases_it(current) -> None:
    auth, store, transport = auth_with([])
    store.save(current)

    with pytest.raises(AuthError, match="Session refresh failed") as caught:
        auth.refresh()

    assert transport.calls == []
    assert store.load() is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_failed_refresh_erases_expired_session() -> None:
    auth, store, _ = auth_with([(401, {})])
    store.save(Session("access-one", "refresh-one", expires_at=900, user_id="user-a"))
    with pytest.raises(AuthError, match="erased"):
        auth.valid_session()
    assert store.load() is None


def test_logout_revokes_then_erases_and_never_returns_token() -> None:
    auth, store, transport = auth_with([(204, {})])
    store.save(Session("access-one", "refresh-one", expires_at=2000))
    assert auth.logout() is None
    assert store.load() is None
    assert transport.calls[0]["url"].endswith("/auth/v1/logout")


def test_logout_erases_even_when_remote_revocation_fails() -> None:
    auth, store, _ = auth_with([(500, {})])
    store.save(Session("access-one", "refresh-one", expires_at=2000))
    with pytest.raises(AuthError, match="Remote logout failed"):
        auth.logout()
    assert store.load() is None


def test_logout_attempts_clear_when_loading_saved_session_fails() -> None:
    class FailingLoadStorage:
        clear_called = False

        def load(self):
            raise RuntimeError("load exposed secret-access")

        def save(self, session):
            raise AssertionError("save must not be called")

        def clear(self):
            self.clear_called = True

    storage = FailingLoadStorage()
    auth = AgentAuth(
        AuthConfig("https://example.supabase.co", PUBLIC_KEY),
        storage,
        transport=FakeTransport([]),
    )

    with pytest.raises(AuthError, match="protected storage was cleared") as caught:
        auth.logout()

    assert storage.clear_called is True
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "secret-access")


def test_logout_erases_when_remote_provider_raises_without_leaking_token() -> None:
    class TokenBearingFailureTransport:
        def post(self, url, *, headers, body=None, timeout=15.0):
            raise RuntimeError(f"provider rejected {headers['authorization']}")

    store = MemoryTokenStorage()
    store.save(Session("secret-access", "secret-refresh", expires_at=2000))
    auth = AgentAuth(
        AuthConfig("https://example.supabase.co", PUBLIC_KEY),
        store,
        transport=TokenBearingFailureTransport(),
    )

    with pytest.raises(AuthError, match="Remote logout failed") as caught:
        auth.logout()

    assert store.load() is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "secret-access", "secret-refresh")


def test_logout_load_and_clear_failures_are_sanitized() -> None:
    class FailingStorage:
        clear_called = False

        def load(self):
            raise RuntimeError("load exposed secret-access")

        def save(self, session):
            raise AssertionError("save must not be called")

        def clear(self):
            self.clear_called = True
            raise RuntimeError("clear exposed secret-refresh")

    storage = FailingStorage()
    auth = AgentAuth(
        AuthConfig("https://example.supabase.co", PUBLIC_KEY),
        storage,
        transport=FakeTransport([]),
    )

    with pytest.raises(AuthError, match="could not be cleared") as caught:
        auth.logout()

    assert storage.clear_called is True
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "secret-access", "secret-refresh")


def test_two_user_sessions_are_isolated_by_storage_instance() -> None:
    first, first_store, first_transport = auth_with([])
    second, second_store, second_transport = auth_with([])
    first_attempt, _ = first.begin_login("http://127.0.0.1:43123/callback")
    second_attempt, _ = second.begin_login("http://127.0.0.1:43124/callback")
    first_transport.responses.append((200, login_payload(user="user-a")))
    second_transport.responses.append((200, login_payload(user="user-b")))
    first.finish_login(first_attempt, callback(first_attempt))
    second.finish_login(second_attempt, callback(second_attempt, port=43124))
    assert first_store.load().user_id == "user-a"
    assert second_store.load().user_id == "user-b"


def test_service_role_key_rejected_and_tokens_hidden_from_repr() -> None:
    with pytest.raises(ValueError, match="public publishable"):
        AuthConfig("https://example.supabase.co", jwt({"role": "service_role"}))
    session = Session("secret-access", "secret-refresh", expires_at=2000)
    assert "secret-access" not in repr(session)
    assert "secret-refresh" not in repr(session)
    config = AuthConfig("https://example.supabase.co", PUBLIC_KEY)
    assert PUBLIC_KEY not in repr(config)


def test_keychain_failure_is_closed_without_secret_in_process_args(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", "failure")

    monkeypatch.setattr(subprocess, "run", fake_run)
    store = MacOSKeychainStorage(account="example.supabase.co")
    session = Session("secret-access", "secret-refresh", expires_at=2000)
    with pytest.raises(AuthError, match="Protected token storage failed"):
        store.save(session)
    args, kwargs = calls[0]
    assert "secret-access" not in " ".join(args)
    assert "secret-refresh" not in " ".join(args)
    assert args[-1] == "-w"
    assert "secret-access" in kwargs["input"]


def test_malformed_keychain_json_has_no_token_in_exception_graph(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    store = MacOSKeychainStorage(account="example.supabase.co")
    malformed = '{"access_token":"KEYCHAIN_ACCESS_SENTINEL","refresh_token":"KEYCHAIN_REFRESH_SENTINEL"'
    monkeypatch.setattr(
        store,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, malformed, ""),
    )

    with pytest.raises(AuthError, match="Protected session data was invalid") as caught:
        store.load()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "KEYCHAIN_ACCESS_SENTINEL", "KEYCHAIN_REFRESH_SENTINEL")


def test_malformed_auth_http_json_has_no_token_in_exception_graph() -> None:
    malformed = b'{"access_token":"HTTP_ACCESS_SENTINEL","refresh_token":"HTTP_REFRESH_SENTINEL"'

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.supabase.co/auth/v1/token"

        def read(self, _limit):
            return malformed

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    transport = UrlLibTransport()
    transport._opener = FakeOpener()
    with pytest.raises(AuthError, match="could not be reached") as caught:
        transport.post(
            "https://example.supabase.co/auth/v1/token",
            headers={"apikey": PUBLIC_KEY},
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_exception_graph_omits(caught.value, "HTTP_ACCESS_SENTINEL", "HTTP_REFRESH_SENTINEL")


def test_static_callback_cleans_history_and_has_restrictive_csp() -> None:
    html = (ROOT / "static/auth/callback/index.html").read_text()
    js = (ROOT / "static/auth/client.js").read_text()
    assert "default-src 'none'" in html
    assert "script-src 'self'" in html
    assert "connect-src 'self';" in html
    assert "*.supabase.co" not in html
    assert "http:" not in html
    assert "history.replaceState(null, \"\", CALLBACK_PATH)" in js
    callback_source = js[js.index("async function finishCallback"):js.index("async function signOut")]
    assert callback_source.index("history.replaceState") < callback_source.index("await fetch")
    assert "sessionStorage" in js
    assert "code_verifier" in js
    assert "const safeSession = projectSession(rawSession)" in js
    assert "sessionStorage.setItem(SESSION_KEY, JSON.stringify(safeSession))" in js
    assert "JSON.stringify(rawSession)" not in js
    assert "provider_token" not in js
    assert "provider_refresh_token" not in js
    assert "window.NewsCuratorPersonalization" in js
    assert "service_role" not in (html + js).lower()


def test_cli_has_no_token_output_command_or_plaintext_fallback() -> None:
    cli = (ROOT / "scripts/personalization_cli.py").read_text()
    assert 'choices=("login", "status", "refresh", "logout", "get", "set")' in cli
    assert "--memory-only" in cli
    assert "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY" in cli
    assert "SERVICE_ROLE" not in cli
    assert "access_token" not in cli
    assert "refresh_token" not in cli


def test_memory_only_rejects_cross_process_commands(capsys) -> None:
    from scripts.personalization_cli import main

    for command in ("status", "refresh", "logout", "get", "set"):
        assert main([command, "--memory-only"]) == 2
    output = capsys.readouterr()
    assert "no cross-process session" in output.err
