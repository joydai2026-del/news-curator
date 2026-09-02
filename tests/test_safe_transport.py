"""Offline threat tests for the shared source HTTP transport."""

from __future__ import annotations

import gzip
import io
import subprocess
import threading
from urllib.parse import quote

import pytest

from curator.sources import (
    ConnectedPeer,
    OriginBoundCredential,
    SafeHttpPolicy,
    SafeHttpTransport,
    SafeTransportError,
    SafeTransportReason,
)


PUBLIC_A = "93.184.216.34"
PUBLIC_B = "142.250.72.14"


def http_response(
    status: int = 200,
    *,
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "application/json"),),
    body: bytes = b"{}",
) -> bytes:
    reason = {200: "OK", 302: "Found"}.get(status, "Status")
    rows = [f"HTTP/1.1 {status} {reason}"]
    if not any(name.lower() in {"content-length", "transfer-encoding"} for name, _ in headers):
        rows.append(f"Content-Length: {len(body)}")
    rows.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(rows) + "\r\n\r\n").encode("ascii") + body


class FakeSocket:
    def __init__(self, response: bytes, *, peer_ip: str = PUBLIC_A) -> None:
        self._response = response
        self._peer_ip = peer_ip
        self.sent = bytearray()
        self.timeouts: list[float | None] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def makefile(self, _mode: str, _buffering: int | None = None):
        return io.BytesIO(self._response)

    def getpeername(self):
        return (self._peer_ip, 443)

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


class QueueConnector:
    def __init__(self, *sockets: FakeSocket, tls_validated: bool = True) -> None:
        self.sockets = list(sockets)
        self.tls_validated = tls_validated
        self.calls: list[tuple[str, int, str, bool, float]] = []

    def __call__(self, host: str, port: int, address: str, tls: bool, timeout: float) -> ConnectedPeer:
        self.calls.append((host, port, address, tls, timeout))
        return ConnectedPeer(self.sockets.pop(0), self.tls_validated)


def resolver_for(mapping: dict[str, tuple[str, ...]]):
    def resolve(host: str, _port: int):
        return mapping[host]

    return resolve


def assert_reason(expected: SafeTransportReason, call) -> SafeTransportError:
    with pytest.raises(SafeTransportError) as caught:
        call()
    assert caught.value.reason is expected
    return caught.value


@pytest.mark.parametrize(
    "url",
    (
        "ftp://example.com/a",
        "//example.com/a",
        "https://user:secret@example.com/a",
        "https://2130706433/a",
        "https://127.1/a",
        "https://0x7f.1/a",
        "https://0177.0.0.1/a",
        "https://example.com\\@127.0.0.1/a",
        "https://example.com/a?access_token=secret",
    ),
)
def test_unsafe_or_ambiguous_url_never_reaches_connector(url):
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(SafeTransportReason.INVALID_URL if "ftp:" in url or "//example" == url[:9] or "user:" in url or "\\" in url or "access_token" in url else SafeTransportReason.UNSAFE_HOST,
                  lambda: transport.get("source-one", url))
    assert connector.calls == []


@pytest.mark.parametrize("addresses", (("127.0.0.1",), (PUBLIC_A, "10.0.0.4"), ("169.254.169.254",)))
def test_every_dns_answer_must_be_global_before_connect(addresses):
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: addresses, connector=connector)

    assert_reason(SafeTransportReason.UNSAFE_HOST, lambda: transport.get("feed-a", "https://news.example/a"))
    assert connector.calls == []


def test_resolution_failure_is_typed_and_sanitized():
    def resolver(_host, _port):
        raise RuntimeError("resolver leaked secret=top-secret")

    error = assert_reason(
        SafeTransportReason.RESOLUTION_FAILED,
        lambda: SafeHttpTransport(resolver=resolver).get("feed/a with spaces", "https://news.example/a"),
    )

    assert str(error) == "feedawithspaces: resolution_failed"
    assert "news.example" not in str(error)
    assert "top-secret" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_peer_mismatch_sends_zero_http_bytes():
    sock = FakeSocket(http_response(), peer_ip=PUBLIC_B)
    connector = QueueConnector(sock)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(SafeTransportReason.PEER_MISMATCH, lambda: transport.get("feed", "https://news.example/a"))
    assert sock.sent == b""


def test_unverified_tls_sends_zero_http_bytes():
    sock = FakeSocket(http_response())
    connector = QueueConnector(sock, tls_validated=False)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(
        SafeTransportReason.TLS_VALIDATION_FAILED,
        lambda: transport.get("feed", "https://news.example/a"),
    )
    assert sock.sent == b""


def test_plain_http_needs_validated_peer_but_not_tls_marker():
    sock = FakeSocket(http_response())
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,),
        connector=QueueConnector(sock, tls_validated=False),
    )

    response = transport.get("feed", "http://news.example/a", allowed_mime_types=("application/json",))

    assert response.status_code == 200
    assert sock.sent.startswith(b"GET /a HTTP/1.1\r\n")


def test_environment_proxy_and_netrc_values_are_not_inherited(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-secret@127.0.0.1:9999")
    monkeypatch.setenv("NETRC", "/definitely/not/read")
    sock = FakeSocket(http_response())
    connector = QueueConnector(sock)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    transport.get("feed", "https://news.example/a")

    assert connector.calls[0][0:4] == ("news.example", 443, PUBLIC_A, True)
    assert b"proxy-secret" not in sock.sent
    assert b"Proxy-Authorization" not in sock.sent


@pytest.mark.parametrize("name", ("Authorization", "Cookie", "Proxy-Authorization", "Host", "Connection"))
def test_ambient_or_hop_headers_are_rejected_before_connect(name):
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(
        SafeTransportReason.INVALID_REQUEST,
        lambda: transport.get("feed", "https://news.example/a", headers={name: "secret"}),
    )
    assert connector.calls == []


def test_redirect_target_is_re_resolved_and_private_target_is_rejected():
    first = FakeSocket(
        http_response(302, headers=(("Location", "http://metadata.example/latest"),), body=b"")
    )
    connector = QueueConnector(first)
    transport = SafeHttpTransport(
        resolver=resolver_for({"news.example": (PUBLIC_A,), "metadata.example": ("169.254.169.254",)}),
        connector=connector,
    )

    assert_reason(
        SafeTransportReason.UNSAFE_HOST,
        lambda: transport.get("feed", "https://news.example/start", allowed_mime_types=("application/json",)),
    )
    assert len(connector.calls) == 1
    assert first.sent.startswith(b"GET /start HTTP/1.1")


def test_origin_bound_auth_is_removed_on_cross_origin_redirect():
    first = FakeSocket(
        http_response(302, headers=(("Location", "https://other.example/final"),), body=b"")
    )
    second = FakeSocket(http_response())
    connector = QueueConnector(first, second)
    transport = SafeHttpTransport(
        resolver=resolver_for({"start.example": (PUBLIC_A,), "other.example": (PUBLIC_A,)}),
        connector=connector,
    )
    credential = OriginBoundCredential("https://start.example", "Authorization", "Bearer exact-secret")

    response = transport.get(
        "api",
        "https://start.example/start",
        credential=credential,
        allowed_mime_types=("application/json",),
    )

    assert response.url == "https://other.example/final"
    assert response.redirect_history == ("https://start.example/start",)
    assert b"Authorization: Bearer exact-secret" in first.sent
    assert b"exact-secret" not in second.sent


@pytest.mark.parametrize(
    ("header_name", "credential_value", "leaked_value"),
    (
        ("Authorization", "Bearer bearer-secret-123", "bearer-secret-123"),
        ("Authorization", "  Bearer   spaced-secret-123  ", "spaced-secret-123"),
        ("Authorization", "Basic dXNlcjpzZWNyZXQ=", "dXNlcjpzZWNyZXQ="),
        ("Authorization", "Token token-scheme-secret-123", "token-scheme-secret-123"),
        ("Authorization", "ApiKey api-key-secret-123", "api-key-secret-123"),
        ("x-api-key", "header-api-secret-123", "header-api-secret-123"),
    ),
)
@pytest.mark.parametrize("encoded", (False, True))
def test_credential_secret_cannot_appear_raw_or_percent_encoded_in_url(
    header_name,
    credential_value,
    leaked_value,
    encoded,
):
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)
    appearance = quote(leaked_value, safe="") if encoded else leaked_value
    credential = OriginBoundCredential(
        "https://start.example", header_name, credential_value
    )

    assert_reason(
        SafeTransportReason.INVALID_URL,
        lambda: transport.get(
            "api", f"https://start.example/path/{appearance}", credential=credential
        ),
    )
    assert connector.calls == []


def test_percent_encoded_complete_authorization_value_is_rejected():
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)
    value = "Bearer exact-secret-123"

    assert_reason(
        SafeTransportReason.INVALID_URL,
        lambda: transport.get(
            "api",
            f"https://start.example/path/{quote(value, safe='')}",
            credential=OriginBoundCredential(
                "https://start.example", "Authorization", value
            ),
        ),
    )
    assert connector.calls == []


def test_credential_in_cross_origin_redirect_url_is_rejected_before_connect():
    first = FakeSocket(
        http_response(
            302,
            headers=(("Location", "https://other.example/path/exact-secret-123"),),
            body=b"",
        )
    )
    connector = QueueConnector(first)
    transport = SafeHttpTransport(
        resolver=resolver_for(
            {"start.example": (PUBLIC_A,), "other.example": (PUBLIC_B,)}
        ),
        connector=connector,
    )

    assert_reason(
        SafeTransportReason.INVALID_URL,
        lambda: transport.get(
            "api",
            "https://start.example/start",
            credential=OriginBoundCredential(
                "https://start.example", "Authorization", "Bearer exact-secret-123"
            ),
        ),
    )
    assert len(connector.calls) == 1


@pytest.mark.parametrize(
    ("value", "path"),
    (("Bearer x", "/xylophone"), ("api", "/rapid-growth")),
)
def test_trivial_short_credential_substrings_do_not_reject_safe_urls(value, path):
    sock = FakeSocket(http_response())
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock)
    )
    header_name = "Authorization" if value.startswith("Bearer") else "x-api-key"

    response = transport.get(
        "api",
        f"https://start.example{path}",
        credential=OriginBoundCredential(
            "https://start.example", header_name, value
        ),
    )

    assert response.status_code == 200


def test_multiple_origin_bound_credentials_are_sent_only_to_exact_origin():
    first = FakeSocket(
        http_response(302, headers=(("Location", "https://other.example/final"),), body=b"")
    )
    second = FakeSocket(http_response(), peer_ip=PUBLIC_B)
    transport = SafeHttpTransport(
        resolver=resolver_for({"start.example": (PUBLIC_A,), "other.example": (PUBLIC_B,)}),
        connector=QueueConnector(first, second),
    )
    credentials = (
        OriginBoundCredential("https://start.example", "Authorization", "Bearer exact-secret"),
        OriginBoundCredential("https://start.example", "apikey", "api-exact-secret"),
    )

    transport.get(
        "api", "https://start.example/start", credentials=credentials
    )

    assert b"Authorization: Bearer exact-secret" in first.sent
    assert b"apikey: api-exact-secret" in first.sent
    assert b"exact-secret" not in second.sent


def test_sensitive_api_key_cannot_be_supplied_as_an_ambient_header():
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(
        SafeTransportReason.INVALID_REQUEST,
        lambda: transport.get(
            "api", "https://start.example/a", headers={"apikey": "secret"}
        ),
    )
    assert connector.calls == []


def test_explicit_user_agent_is_validated_and_serialized():
    sock = FakeSocket(http_response())
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock)
    )

    transport.get(
        "feed",
        "https://start.example/a",
        user_agent="news-curator-test/1 (+https://example.com)",
    )

    assert b"User-Agent: news-curator-test/1 (+https://example.com)\r\n" in sock.sent


@pytest.mark.parametrize("value", ("", "bad\r\nInjected: yes", "snowman \N{SNOWMAN}", "x" * 257))
def test_invalid_explicit_user_agent_is_rejected_before_connect(value):
    connector = QueueConnector()
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(
        SafeTransportReason.INVALID_REQUEST,
        lambda: transport.get(
            "feed", "https://start.example/a", user_agent=value
        ),
    )
    assert connector.calls == []


def test_credential_with_wrong_origin_is_rejected_without_dns_or_connect():
    connector = QueueConnector()
    resolver_calls: list[str] = []

    def resolver(host, _port):
        resolver_calls.append(host)
        return (PUBLIC_A,)

    transport = SafeHttpTransport(resolver=resolver, connector=connector)
    credential = OriginBoundCredential("https://other.example", "Authorization", "Bearer secret")

    assert_reason(
        SafeTransportReason.CREDENTIAL_ORIGIN_MISMATCH,
        lambda: transport.get("api", "https://start.example/a", credential=credential),
    )
    assert resolver_calls == []
    assert connector.calls == []


def test_http_origin_bound_credential_is_rejected_before_dns_connect_or_request_bytes():
    sock = FakeSocket(http_response())
    connector = QueueConnector(sock, tls_validated=False)
    resolver_calls: list[str] = []

    def resolver(host, _port):
        resolver_calls.append(host)
        return (PUBLIC_A,)

    transport = SafeHttpTransport(resolver=resolver, connector=connector)

    assert_reason(
        SafeTransportReason.CREDENTIAL_ORIGIN_MISMATCH,
        lambda: transport.get(
            "api",
            "http://start.example/a",
            credential=OriginBoundCredential(
                "http://start.example", "Authorization", "Bearer exact-secret"
            ),
        ),
    )
    assert resolver_calls == []
    assert connector.calls == []
    assert sock.sent == b""


def test_https_credential_is_removed_before_redirect_to_plain_http():
    first = FakeSocket(
        http_response(302, headers=(("Location", "http://start.example/final"),), body=b"")
    )
    second = FakeSocket(http_response(), peer_ip=PUBLIC_A)
    connector = QueueConnector(first, second, tls_validated=True)
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,), connector=connector
    )

    response = transport.get(
        "api",
        "https://start.example/start",
        credential=OriginBoundCredential(
            "https://start.example", "Authorization", "Bearer exact-secret"
        ),
    )

    assert response.url == "http://start.example/final"
    assert b"Authorization: Bearer exact-secret" in first.sent
    assert b"exact-secret" not in second.sent


def test_redirect_loop_is_rejected():
    first = FakeSocket(http_response(302, headers=(("Location", "/again"),), body=b""))
    second = FakeSocket(http_response(302, headers=(("Location", "/start"),), body=b""))
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,),
        connector=QueueConnector(first, second),
    )

    assert_reason(
        SafeTransportReason.REDIRECT_REJECTED,
        lambda: transport.get("feed", "https://news.example/start"),
    )


def test_non_safe_method_redirect_is_not_replayed():
    sock = FakeSocket(http_response(302, headers=(("Location", "/other"),), body=b""))
    connector = QueueConnector(sock)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(
        SafeTransportReason.REDIRECT_REJECTED,
        lambda: transport.request("api", "POST", "https://news.example/a", body=b"payload"),
    )
    assert len(connector.calls) == 1


def test_wire_byte_cap_stops_oversized_response():
    sock = FakeSocket(http_response(body=b"12345"))
    policy = SafeHttpPolicy(max_wire_bytes=4, max_decoded_bytes=20)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock), policy=policy)

    assert_reason(SafeTransportReason.RESPONSE_TOO_LARGE, lambda: transport.get("feed", "https://news.example/a"))


def test_explicit_prefix_mode_reads_only_bound_and_reports_truncation():
    sock = FakeSocket(
        http_response(
            headers=(("Content-Type", "text/html"),),
            body=b"<head>" + b"x" * 100,
        )
    )
    policy = SafeHttpPolicy(max_wire_bytes=16, max_decoded_bytes=16, read_chunk_bytes=8)
    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,),
        connector=QueueConnector(sock),
        policy=policy,
    )

    response = transport.get(
        "image-meta",
        "https://news.example/a",
        allow_truncated_response=True,
    )

    assert response.body == (b"<head>" + b"x" * 10)
    assert response.body_truncated is True
    assert b"Accept-Encoding: identity\r\n" in sock.sent


def test_decoded_byte_cap_stops_gzip_bomb_shape():
    compressed = gzip.compress(b"A" * 1000)
    sock = FakeSocket(
        http_response(headers=(("Content-Type", "application/json"), ("Content-Encoding", "gzip")), body=compressed)
    )
    policy = SafeHttpPolicy(max_wire_bytes=len(compressed) + 1, max_decoded_bytes=100)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock), policy=policy)

    error = assert_reason(
        SafeTransportReason.RESPONSE_TOO_LARGE,
        lambda: transport.get("feed-secret", "https://news.example/a"),
    )
    assert error.source_id == "feed-secret"


@pytest.mark.parametrize("encoding", ("br", "gzip, deflate, gzip"))
def test_unsupported_or_over_nested_content_encoding_is_rejected(encoding):
    sock = FakeSocket(
        http_response(headers=(("Content-Type", "application/json"), ("Content-Encoding", encoding)), body=b"data")
    )
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock))

    assert_reason(
        SafeTransportReason.UNSUPPORTED_CONTENT_ENCODING,
        lambda: transport.get("feed", "https://news.example/a"),
    )


@pytest.mark.parametrize("content_type", ("text/html", "", "application/xml"))
def test_adapter_mime_allowlist_is_enforced(content_type):
    headers = (("Content-Type", content_type),) if content_type else ()
    sock = FakeSocket(http_response(headers=headers))
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=QueueConnector(sock))

    assert_reason(
        SafeTransportReason.UNSUPPORTED_MIME_TYPE,
        lambda: transport.get("feed", "https://news.example/a", allowed_mime_types=("application/json",)),
    )


def test_duplicate_location_and_ambiguous_framing_are_rejected():
    duplicate_location = FakeSocket(
        http_response(302, headers=(("Location", "/a"), ("Location", "/b")), body=b"")
    )
    ambiguous_length = FakeSocket(
        http_response(headers=(("Content-Length", "2"), ("Content-Length", "2")), body=b"{}")
    )
    connector = QueueConnector(duplicate_location, ambiguous_length)
    transport = SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector)

    assert_reason(SafeTransportReason.MALFORMED_RESPONSE, lambda: transport.get("feed", "https://news.example/a"))
    assert_reason(SafeTransportReason.MALFORMED_RESPONSE, lambda: transport.get("feed", "https://news.example/b"))


def test_idna_host_is_normalized_before_resolve_connect_and_host_header():
    sock = FakeSocket(http_response())
    connector = QueueConnector(sock)
    resolved: list[str] = []

    def resolver(host, _port):
        resolved.append(host)
        return (PUBLIC_A,)

    transport = SafeHttpTransport(resolver=resolver, connector=connector)
    response = transport.get("feed", "https://b\u00fccher.example/a")

    assert resolved == ["xn--bcher-kva.example"]
    assert connector.calls[0][0] == "xn--bcher-kva.example"
    assert b"Host: xn--bcher-kva.example\r\n" in sock.sent
    assert response.url == "https://xn--bcher-kva.example/a"


def test_total_deadline_includes_resolution():
    class ManualClock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = ManualClock()
    connector = QueueConnector()

    def slow_resolver(_host, _port):
        clock.value = 2.0
        return (PUBLIC_A,)

    policy = SafeHttpPolicy(total_timeout_seconds=1.0)
    transport = SafeHttpTransport(resolver=slow_resolver, connector=connector, clock=clock, policy=policy)

    assert_reason(
        SafeTransportReason.DEADLINE_EXCEEDED,
        lambda: transport.get("feed", "https://news.example/a"),
    )
    assert connector.calls == []


def test_connect_timeout_is_shared_so_a_later_address_can_succeed():
    class ManualClock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = ManualClock()
    calls: list[tuple[str, float]] = []
    successful_socket = FakeSocket(http_response(), peer_ip=PUBLIC_B)

    def connector(_host, _port, address, _tls, timeout):
        calls.append((address, timeout))
        if address == PUBLIC_A:
            clock.value += timeout
            raise TimeoutError
        return ConnectedPeer(successful_socket, True)

    transport = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A, PUBLIC_B),
        connector=connector,
        clock=clock,
        policy=SafeHttpPolicy(total_timeout_seconds=10.0),
    )

    response = transport.get("feed", "https://news.example/a")

    assert response.status_code == 200
    assert [address for address, _timeout in calls] == [PUBLIC_A, PUBLIC_B]
    assert calls[0][1] == pytest.approx(5.0)
    assert calls[1][1] == pytest.approx(5.0)


def test_default_resolver_timeout_is_fail_closed_and_leaves_no_connector_call(monkeypatch):
    connector = QueueConnector()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="resolver", timeout=0.01)

    monkeypatch.setattr("curator.sources.transport.subprocess.run", timeout)
    transport = SafeHttpTransport(
        connector=connector,
        policy=SafeHttpPolicy(total_timeout_seconds=0.1),
    )

    assert_reason(
        SafeTransportReason.DEADLINE_EXCEEDED,
        lambda: transport.get("feed", "https://bounded-resolver.example/a"),
    )
    assert connector.calls == []


def test_per_host_concurrency_is_global_across_transport_instances():
    gate = threading.Event()
    two_entered = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    maximum = 0

    def connector(_host, _port, _address, _tls, _timeout):
        nonlocal calls, active, maximum
        with lock:
            calls += 1
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
        assert gate.wait(2)
        with lock:
            active -= 1
        return ConnectedPeer(FakeSocket(http_response()), True)

    policy = SafeHttpPolicy(per_host_concurrency=2, total_timeout_seconds=3)
    transports = [
        SafeHttpTransport(resolver=lambda *_: (PUBLIC_A,), connector=connector, policy=policy)
        for _ in range(3)
    ]
    failures: list[BaseException] = []

    def run(transport):
        try:
            transport.get("feed", "https://parallel-limit.example/a")
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(transport,)) for transport in transports]
    for thread in threads:
        thread.start()
    assert two_entered.wait(1)
    with lock:
        assert calls == 2
        assert maximum == 2
    gate.set()
    for thread in threads:
        thread.join(2)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert calls == 3
    assert maximum == 2


def test_completed_strict_request_does_not_ratchet_future_host_limit_down():
    host = "temporary-strict-limit.example"
    strict = SafeHttpTransport(
        resolver=lambda *_: (PUBLIC_A,),
        connector=QueueConnector(FakeSocket(http_response())),
        policy=SafeHttpPolicy(per_host_concurrency=1),
    )
    strict.get("strict", f"https://{host}/first")

    gate = threading.Event()
    two_entered = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def connector(_host, _port, _address, _tls, _timeout):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
        assert gate.wait(2)
        with lock:
            active -= 1
        return ConnectedPeer(FakeSocket(http_response()), True)

    policy = SafeHttpPolicy(per_host_concurrency=2, total_timeout_seconds=3)
    transports = [
        SafeHttpTransport(
            resolver=lambda *_: (PUBLIC_A,), connector=connector, policy=policy
        )
        for _ in range(2)
    ]
    failures = []

    def run(current):
        try:
            current.get("feed", f"https://{host}/next")
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(current,)) for current in transports]
    for thread in threads:
        thread.start()
    assert two_entered.wait(1)
    gate.set()
    for thread in threads:
        thread.join(2)

    assert not failures
    assert maximum == 2
