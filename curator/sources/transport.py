"""A small HTTP transport with SSRF, credential, and resource bounds.

The implementation intentionally does not use ``requests``.  A raw, pinned
socket means environment proxies and ``.netrc`` credentials cannot be inherited
at all.  DNS and connection establishment are injectable so the security
contract can be exercised without a real network.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

from .errors import SafeTransportError, SafeTransportReason


_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_METHOD = re.compile(r"^[A-Z]+$")
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_SENSITIVE_HEADERS = frozenset(
    ("authorization", "apikey", "cookie", "proxy-authorization", "x-api-key")
)
_HOP_BY_HOP_HEADERS = frozenset(
    ("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade")
)
_SECRET_QUERY_KEYS = frozenset(
    ("access_token", "api_key", "apikey", "auth", "authorization", "key", "password", "secret", "token")
)
_SUPPORTED_ENCODINGS = frozenset(("identity", "gzip", "deflate"))
_AUTH_SECRET_SCHEMES = frozenset(
    ("bearer", "basic", "token", "apikey", "api-key", "api_key")
)
_API_KEY_HEADERS = frozenset(("apikey", "api-key", "x-api-key", "x-goog-api-key"))
_MIN_URL_SECRET_LENGTH = 8
_DEFAULT_USER_AGENT = "news-curator-safe-transport/1"


class SocketLike(Protocol):
    def sendall(self, data: bytes) -> None: ...
    def makefile(self, mode: str, buffering: int | None = ...) -> object: ...
    def getpeername(self) -> object: ...
    def settimeout(self, value: float | None) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ConnectedPeer:
    """A connected stream plus proof supplied by the connector's TLS path."""

    stream: SocketLike
    tls_hostname_validated: bool


Resolver = Callable[[str, int], Sequence[str]]
Connector = Callable[[str, int, str, bool, float], ConnectedPeer]
Clock = Callable[[], float]


@dataclass(frozen=True)
class OriginBoundCredential:
    """One credential, usable only at one normalized HTTP origin."""

    origin: str
    header_name: str
    value: str


@dataclass(frozen=True)
class SafeHttpPolicy:
    total_timeout_seconds: float = 15.0
    max_wire_bytes: int = 8 * 1024 * 1024
    max_decoded_bytes: int = 8 * 1024 * 1024
    max_request_bytes: int = 2 * 1024 * 1024
    max_header_bytes: int = 64 * 1024
    max_redirects: int = 4
    max_content_encodings: int = 2
    per_host_concurrency: int = 4
    read_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        positive = (
            self.total_timeout_seconds,
            self.max_wire_bytes,
            self.max_decoded_bytes,
            self.max_request_bytes,
            self.max_header_bytes,
            self.per_host_concurrency,
            self.read_chunk_bytes,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("transport policy bounds must be positive")
        if self.max_redirects < 0 or self.max_content_encodings < 0:
            raise ValueError("transport policy counts cannot be negative")


@dataclass(frozen=True)
class SafeHttpResponse:
    status_code: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    redirect_history: tuple[str, ...] = field(default_factory=tuple)
    body_truncated: bool = False


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path_and_query: str
    url: str
    origin: str


@dataclass
class _HostState:
    condition: threading.Condition = field(default_factory=threading.Condition)
    active: int = 0
    requested_limits: dict[int, int] = field(default_factory=dict)
    next_token: int = 0


class _HostLease:
    def __init__(self, state: _HostState, token: int) -> None:
        self._state = state
        self._token = token

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        with self._state.condition:
            self._state.active -= 1
            self._state.requested_limits.pop(self._token, None)
            self._state.condition.notify_all()


class _PeerFailure(Exception):
    pass


class _TlsFailure(Exception):
    pass


class _BodyTooLarge(Exception):
    pass


class _MalformedBody(Exception):
    pass


class SafeHttpTransport:
    """HTTP/1.1 transport that validates before transmitting request bytes."""

    _host_states: dict[str, _HostState] = {}
    _host_states_lock = threading.Lock()

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        clock: Clock = time.monotonic,
        policy: SafeHttpPolicy | None = None,
    ) -> None:
        # ``None`` is meaningful: the built-in resolver runs in a disposable
        # child process that can be killed at the request deadline. Injected
        # resolvers remain synchronous so deterministic tests and custom
        # deployments keep their explicit contract.
        self._resolver = resolver
        self._connector = connector or _default_connector
        self._clock = clock
        self.policy = policy or SafeHttpPolicy()

    def with_policy(self, policy: SafeHttpPolicy) -> "SafeHttpTransport":
        """Return a policy view that shares the safe network capabilities.

        Host concurrency is class-scoped, so independently configured sources
        still participate in one conservative per-host limiter.
        """

        return SafeHttpTransport(
            resolver=self._resolver,
            connector=self._connector,
            clock=self._clock,
            policy=policy,
        )

    def request(
        self,
        source_id: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | bytearray | memoryview | None = None,
        credential: OriginBoundCredential | None = None,
        credentials: Iterable[OriginBoundCredential] = (),
        allowed_mime_types: Iterable[str] = (),
        allow_truncated_response: bool = False,
        user_agent: str | None = None,
    ) -> SafeHttpResponse:
        """Issue a request while keeping all underlying failures private."""

        sanitized: SafeTransportError | None = None
        try:
            return self._request(
                source_id,
                method,
                url,
                headers=headers,
                body=body,
                credential=credential,
                credentials=credentials,
                allowed_mime_types=allowed_mime_types,
                allow_truncated_response=allow_truncated_response,
                user_agent=user_agent,
            )
        except SafeTransportError as exc:
            # A resolver, TLS library, or parser exception can contain a host,
            # response fragment, or credential. Do not leave it reachable as
            # ``__context__`` for a caller or traceback logger.
            sanitized = SafeTransportError(exc.source_id, exc.reason)
        # Raise after leaving the except block. Raising inside it, even with
        # ``from None``, suppresses display but still retains ``__context__``.
        raise sanitized from None

    def _request(
        self,
        source_id: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | bytearray | memoryview | None = None,
        credential: OriginBoundCredential | None = None,
        credentials: Iterable[OriginBoundCredential] = (),
        allowed_mime_types: Iterable[str] = (),
        allow_truncated_response: bool = False,
        user_agent: str | None = None,
    ) -> SafeHttpResponse:
        """Issue one bounded request and manually validate every redirect."""

        started = self._clock()
        deadline = started + self.policy.total_timeout_seconds
        current_method = str(method or "").upper()
        if not _METHOD.fullmatch(current_method) or current_method in {"CONNECT", "TRACE"}:
            self._fail(source_id, SafeTransportReason.INVALID_REQUEST)
        payload = bytes(body or b"")
        if len(payload) > self.policy.max_request_bytes:
            self._fail(source_id, SafeTransportReason.INVALID_REQUEST)
        base_headers = _validate_request_headers(source_id, headers or {}, self.policy.max_header_bytes)
        selected_user_agent = _validate_user_agent(source_id, user_agent)
        allowed_mimes = _normalize_mime_patterns(source_id, allowed_mime_types)

        target = _parse_target(source_id, url)
        supplied = tuple(credentials)
        if credential is not None:
            if supplied:
                self._fail(source_id, SafeTransportReason.INVALID_REQUEST)
            supplied = (credential,)
        active_credentials = _validate_credentials(
            source_id, supplied, target, base_headers
        )
        protected_credentials = active_credentials

        history: list[str] = []
        seen = {target.url}
        for hop in range(self.policy.max_redirects + 1):
            with self._acquire_host(source_id, target.host, deadline):
                response = self._request_once(
                    source_id,
                    current_method,
                    target,
                    base_headers,
                    payload,
                    active_credentials,
                    allowed_mimes,
                    deadline,
                    allow_truncated_response,
                    selected_user_agent,
                )

            if response.status_code not in _REDIRECT_STATUSES:
                return SafeHttpResponse(
                    status_code=response.status_code,
                    url=target.url,
                    headers=response.headers,
                    body=response.body,
                    redirect_history=tuple(history),
                    body_truncated=response.body_truncated,
                )
            if hop >= self.policy.max_redirects:
                self._fail(source_id, SafeTransportReason.TOO_MANY_REDIRECTS)
            if current_method not in {"GET", "HEAD"}:
                self._fail(source_id, SafeTransportReason.REDIRECT_REJECTED)
            location = response.headers.get("location", "")
            if not location:
                self._fail(source_id, SafeTransportReason.REDIRECT_REJECTED)
            next_target = _parse_target(source_id, urljoin(target.url, location))
            _reject_credentials_in_url(source_id, protected_credentials, next_target.url)
            if next_target.url in seen:
                self._fail(source_id, SafeTransportReason.REDIRECT_REJECTED)
            seen.add(next_target.url)
            history.append(target.url)
            if next_target.origin != target.origin:
                active_credentials = ()
            target = next_target

        self._fail(source_id, SafeTransportReason.TOO_MANY_REDIRECTS)

    def get(
        self,
        source_id: str,
        url: str,
        **kwargs: object,
    ) -> SafeHttpResponse:
        return self.request(source_id, "GET", url, **kwargs)

    def _request_once(
        self,
        source_id: str,
        method: str,
        target: _Target,
        headers: Mapping[str, str],
        body: bytes,
        credentials: Sequence[OriginBoundCredential],
        allowed_mimes: Sequence[str],
        deadline: float,
        allow_truncated_response: bool,
        user_agent: str,
    ) -> SafeHttpResponse:
        addresses = self._resolve(source_id, target, deadline)
        peer = self._connect(source_id, target, addresses, deadline)
        response: http.client.HTTPResponse | None = None
        try:
            peer_ip = _peer_ip(peer.stream)
            if peer_ip not in addresses or not ipaddress.ip_address(peer_ip).is_global:
                self._fail(source_id, SafeTransportReason.PEER_MISMATCH)
            if target.scheme == "https" and not peer.tls_hostname_validated:
                self._fail(source_id, SafeTransportReason.TLS_VALIDATION_FAILED)

            # No HTTP request byte is assembled or sent until the peer and TLS
            # checks immediately above have succeeded.
            request_headers = dict(headers)
            for credential in credentials:
                request_headers[credential.header_name] = credential.value
            request_bytes = _serialize_request(
                source_id,
                method,
                target,
                request_headers,
                body,
                accept_compression=not allow_truncated_response,
                user_agent=user_agent,
            )
            self._set_remaining_timeout(source_id, peer.stream, deadline)
            peer.stream.sendall(request_bytes)

            self._set_remaining_timeout(source_id, peer.stream, deadline)
            response = http.client.HTTPResponse(peer.stream, method=method)  # type: ignore[arg-type]
            response.begin()
            response_headers = _response_headers(source_id, response, self.policy.max_header_bytes)
            self._validate_response_framing(source_id, response)
            if response.status in _REDIRECT_STATUSES:
                return SafeHttpResponse(
                    status_code=response.status,
                    url=target.url,
                    headers=MappingProxyType(response_headers),
                    body=b"",
                )
            encodings = _content_encodings(source_id, response_headers, self.policy.max_content_encodings)
            _validate_response_mime(source_id, response_headers, allowed_mimes)

            content_length = _content_length(source_id, response)
            if (
                content_length is not None
                and content_length > self.policy.max_wire_bytes
                and not allow_truncated_response
            ):
                self._fail(source_id, SafeTransportReason.RESPONSE_TOO_LARGE)
            if allow_truncated_response:
                if any(encoding != "identity" for encoding in encodings):
                    self._fail(source_id, SafeTransportReason.UNSUPPORTED_CONTENT_ENCODING)
                wire_body, truncated = self._read_wire_prefix(
                    source_id,
                    response,
                    peer.stream,
                    deadline,
                    content_length,
                )
            else:
                wire_body = self._read_wire_body(source_id, response, peer.stream, deadline)
                truncated = False
            decoded = _decode_body(source_id, wire_body, encodings, self.policy.max_decoded_bytes)
            return SafeHttpResponse(
                status_code=response.status,
                url=target.url,
                headers=MappingProxyType(response_headers),
                body=decoded,
                body_truncated=truncated,
            )
        except SafeTransportError:
            raise
        except (TimeoutError, socket.timeout):
            self._fail(source_id, SafeTransportReason.DEADLINE_EXCEEDED)
        except (http.client.HTTPException, OSError, ValueError):
            self._fail(source_id, SafeTransportReason.MALFORMED_RESPONSE)
        finally:
            if response is not None:
                response.close()
            peer.stream.close()

    def _resolve(self, source_id: str, target: _Target, deadline: float) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(target.host)
        except ValueError:
            self._ensure_time(source_id, deadline)
            try:
                if self._resolver is None:
                    raw_addresses = _bounded_default_resolver(
                        target.host,
                        target.port,
                        self._remaining(source_id, deadline),
                    )
                else:
                    raw_addresses = self._resolver(target.host, target.port)
            except (TimeoutError, subprocess.TimeoutExpired):
                self._fail(source_id, SafeTransportReason.DEADLINE_EXCEEDED)
            except Exception:
                self._fail(source_id, SafeTransportReason.RESOLUTION_FAILED)
            self._ensure_time(source_id, deadline)
        else:
            raw_addresses = (literal.compressed,)

        addresses: list[str] = []
        for raw in raw_addresses:
            try:
                address = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except ValueError:
                self._fail(source_id, SafeTransportReason.RESOLUTION_FAILED)
            if not address.is_global:
                self._fail(source_id, SafeTransportReason.UNSAFE_HOST)
            if address.compressed not in addresses:
                addresses.append(address.compressed)
        if not addresses:
            self._fail(source_id, SafeTransportReason.RESOLUTION_FAILED)
        return tuple(addresses)

    def _connect(
        self,
        source_id: str,
        target: _Target,
        addresses: Sequence[str],
        deadline: float,
    ) -> ConnectedPeer:
        for index, address in enumerate(addresses):
            remaining = self._remaining(source_id, deadline)
            addresses_left = len(addresses) - index
            attempt_timeout = remaining / addresses_left
            try:
                peer = self._connector(
                    target.host,
                    target.port,
                    address,
                    target.scheme == "https",
                    attempt_timeout,
                )
            except _PeerFailure:
                self._fail(source_id, SafeTransportReason.PEER_MISMATCH)
            except _TlsFailure:
                self._fail(source_id, SafeTransportReason.TLS_VALIDATION_FAILED)
            except (TimeoutError, socket.timeout):
                if self._clock() >= deadline:
                    self._fail(source_id, SafeTransportReason.DEADLINE_EXCEEDED)
                continue
            except Exception:
                continue
            return peer
        self._fail(source_id, SafeTransportReason.CONNECT_FAILED)

    def _read_wire_body(
        self,
        source_id: str,
        response: http.client.HTTPResponse,
        stream: SocketLike,
        deadline: float,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            self._set_remaining_timeout(source_id, stream, deadline)
            chunk = response.read(min(self.policy.read_chunk_bytes, self.policy.max_wire_bytes - total + 1))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > self.policy.max_wire_bytes:
                self._fail(source_id, SafeTransportReason.RESPONSE_TOO_LARGE)
            chunks.append(chunk)

    def _read_wire_prefix(
        self,
        source_id: str,
        response: http.client.HTTPResponse,
        stream: SocketLike,
        deadline: float,
        content_length: int | None,
    ) -> tuple[bytes, bool]:
        """Read a bounded identity-encoded prefix and close the connection."""

        limit = min(self.policy.max_wire_bytes, self.policy.max_decoded_bytes)
        chunks: list[bytes] = []
        total = 0
        while total < limit:
            self._set_remaining_timeout(source_id, stream, deadline)
            chunk = response.read(min(self.policy.read_chunk_bytes, limit - total))
            if not chunk:
                return b"".join(chunks), False
            chunks.append(chunk)
            total += len(chunk)
        if content_length is not None:
            return b"".join(chunks), content_length > limit
        self._set_remaining_timeout(source_id, stream, deadline)
        return b"".join(chunks), bool(response.read(1))

    def _validate_response_framing(self, source_id: str, response: http.client.HTTPResponse) -> None:
        content_lengths = response.headers.get_all("Content-Length", [])
        transfers = response.headers.get_all("Transfer-Encoding", [])
        if len(content_lengths) > 1 or (content_lengths and transfers):
            self._fail(source_id, SafeTransportReason.MALFORMED_RESPONSE)
        if transfers:
            tokens = [part.strip().lower() for value in transfers for part in value.split(",")]
            if tokens != ["chunked"]:
                self._fail(source_id, SafeTransportReason.MALFORMED_RESPONSE)

    def _acquire_host(self, source_id: str, host: str, deadline: float) -> _HostLease:
        with self._host_states_lock:
            state = self._host_states.setdefault(host, _HostState())
        with state.condition:
            token = state.next_token
            state.next_token += 1
            state.requested_limits[token] = self.policy.per_host_concurrency
            try:
                while state.active >= min(state.requested_limits.values()):
                    remaining = self._remaining(source_id, deadline)
                    state.condition.wait(timeout=remaining)
                state.active += 1
            except BaseException:
                state.requested_limits.pop(token, None)
                state.condition.notify_all()
                raise
        return _HostLease(state, token)

    def _set_remaining_timeout(self, source_id: str, stream: SocketLike, deadline: float) -> None:
        stream.settimeout(self._remaining(source_id, deadline))

    def _remaining(self, source_id: str, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            self._fail(source_id, SafeTransportReason.DEADLINE_EXCEEDED)
        return remaining

    def _ensure_time(self, source_id: str, deadline: float) -> None:
        self._remaining(source_id, deadline)

    @staticmethod
    def _fail(source_id: str, reason: SafeTransportReason) -> None:
        raise SafeTransportError(source_id, reason) from None


_RESOLVER_CHILD = """
import socket
import sys

try:
    rows = socket.getaddrinfo(
        sys.argv[1],
        int(sys.argv[2]),
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
except (socket.gaierror, UnicodeError, ValueError):
    raise SystemExit(2)

seen = set()
for row in rows:
    address = str(row[4][0]).split("%", 1)[0]
    if address not in seen:
        seen.add(address)
        print(address)
"""


def _bounded_default_resolver(host: str, port: int, timeout: float) -> Sequence[str]:
    """Resolve in a disposable process so a stuck libc resolver is killable.

    Python does not expose a cancellable ``getaddrinfo`` call. A thread timeout
    would return to the caller while leaving unbounded resolver work behind.
    ``subprocess.run`` kills and waits for its child on timeout, so the request
    deadline covers DNS wall time without leaking a thread or process.
    """

    completed = subprocess.run(
        [sys.executable, "-I", "-c", _RESOLVER_CHILD, host, str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise OSError("resolution failed")
    try:
        values = completed.stdout.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise OSError("resolution failed") from None
    if not values or len(values) > 64 or any(len(value) > 64 for value in values):
        raise OSError("resolution failed")
    return tuple(values)


def _default_connector(host: str, port: int, address: str, use_tls: bool, timeout: float) -> ConnectedPeer:
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    raw = socket.socket(family, socket.SOCK_STREAM)
    try:
        raw.settimeout(timeout)
        destination = (address, port, 0, 0) if parsed.version == 6 else (address, port)
        raw.connect(destination)
        actual = ipaddress.ip_address(str(raw.getpeername()[0]).split("%", 1)[0])
        if actual != parsed or not actual.is_global:
            raise _PeerFailure
        if not use_tls:
            return ConnectedPeer(raw, False)
        context = ssl.create_default_context()
        try:
            secured = context.wrap_socket(raw, server_hostname=host)
        except Exception as exc:
            raise _TlsFailure from exc
        return ConnectedPeer(secured, True)
    except Exception:
        raw.close()
        raise


def _parse_target(source_id: str, raw_url: object) -> _Target:
    text = str(raw_url or "")
    if not text or len(text) > 8192 or any(ch.isspace() or ord(ch) == 127 for ch in text):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
    if "\\" in text:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
    try:
        split = urlsplit(text)
        port = split.port
    except (TypeError, ValueError):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL) from None
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"} or not split.netloc or split.username is not None or split.password is not None:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
    raw_host = split.hostname or ""
    if not raw_host or "%" in raw_host or raw_host.endswith(".."):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
    host = _normalize_host(source_id, raw_host.rstrip("."))
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)

    for key, _value in parse_qsl(split.query, keep_blank_values=True):
        if key.strip().lower() in _SECRET_QUERY_KEYS:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
    path = quote(split.path or "/", safe="/%:@-._~!$&'()*+,;=")
    query = quote(split.query, safe="/%?:@-._~!$&'()*+,;=")
    path_and_query = path + (("?" + query) if query else "")
    default_port = 443 if scheme == "https" else 80
    display_host = f"[{host}]" if ":" in host else host
    authority = display_host if port == default_port else f"{display_host}:{port}"
    normalized = f"{scheme}://{authority}{path_and_query}"
    return _Target(scheme, host, port, path_and_query, normalized, f"{scheme}://{authority}")


def _normalize_host(source_id: str, raw_host: str) -> str:
    lowered = raw_host.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        raise SafeTransportError(source_id, SafeTransportReason.UNSAFE_HOST)
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        if not lowered or len(lowered) > 253:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
        try:
            ascii_host = lowered.encode("idna").decode("ascii")
        except UnicodeError:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL) from None
        labels = ascii_host.split(".")
        if any(not label or len(label) > 63 for label in labels):
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
        if any(label.startswith("-") or label.endswith("-") for label in labels):
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
        if any(not re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)
        # Numeric spellings are interpreted differently across URL parsers and
        # C resolvers. A normal DNS name never has an all-numeric final label.
        if labels[-1].isdigit() or labels[-1].startswith("0x"):
            raise SafeTransportError(source_id, SafeTransportReason.UNSAFE_HOST)
        return ascii_host
    if not literal.is_global:
        raise SafeTransportError(source_id, SafeTransportReason.UNSAFE_HOST)
    return literal.compressed


def _validate_request_headers(source_id: str, values: Mapping[str, str], cap: int) -> Mapping[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    if len(values) > 100:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    for raw_name, raw_value in values.items():
        name = str(raw_name)
        value = str(raw_value)
        lowered = name.lower()
        if not _HEADER_NAME.fullmatch(name) or lowered in seen or lowered in _SENSITIVE_HEADERS or lowered in _HOP_BY_HOP_HEADERS or lowered in {"host", "content-length", "accept-encoding", "user-agent"}:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
        total += len(name) + len(value) + 4
        if total > cap:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
        seen.add(lowered)
        result[name] = value
    return MappingProxyType(result)


def _validate_credential(source_id: str, credential: OriginBoundCredential, target: _Target) -> OriginBoundCredential:
    try:
        raw_origin = urlsplit(credential.origin)
        origin_target = _parse_target(source_id, credential.origin)
    except SafeTransportError:
        raise SafeTransportError(source_id, SafeTransportReason.CREDENTIAL_ORIGIN_MISMATCH) from None
    if (
        origin_target.scheme != "https"
        or raw_origin.query
        or raw_origin.fragment
        or raw_origin.path not in {"", "/"}
        or origin_target.origin != target.origin
    ):
        raise SafeTransportError(source_id, SafeTransportReason.CREDENTIAL_ORIGIN_MISMATCH)
    if not _HEADER_NAME.fullmatch(credential.header_name) or credential.header_name.lower() in _HOP_BY_HOP_HEADERS | {"host", "content-length", "cookie", "proxy-authorization", "accept-encoding", "user-agent"}:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    if not credential.value or len(credential.value) > 8192 or any(ord(ch) < 32 or ord(ch) == 127 for ch in credential.value):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    return credential


def _validate_user_agent(source_id: str, value: str | None) -> str:
    if value is None:
        return _DEFAULT_USER_AGENT
    selected = str(value)
    if (
        not selected
        or len(selected) > 256
        or any(ord(ch) < 32 or ord(ch) > 126 for ch in selected)
    ):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    return selected


def _credential_url_candidates(credential: OriginBoundCredential) -> tuple[str, ...]:
    """Return non-trivial credential strings that must never occur in a URL."""

    value = credential.value
    stripped = value.strip()
    candidates = [value, stripped]
    lowered_name = credential.header_name.lower()
    if lowered_name == "authorization":
        match = re.fullmatch(r"([^ ]+) +(.+)", stripped)
        if match and match.group(1).lower() in _AUTH_SECRET_SCHEMES:
            candidates.append(match.group(2).strip())
    elif lowered_name in _API_KEY_HEADERS:
        candidates.append(stripped)
    return tuple(
        dict.fromkeys(
            candidate
            for candidate in candidates
            if len(candidate) >= _MIN_URL_SECRET_LENGTH
        )
    )


def _reject_credentials_in_url(
    source_id: str,
    credentials: Sequence[OriginBoundCredential],
    url: str,
) -> None:
    decoded_url = unquote(url)
    for credential in credentials:
        if any(
            candidate in url or candidate in decoded_url
            for candidate in _credential_url_candidates(credential)
        ):
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_URL)


def _validate_credentials(
    source_id: str,
    credentials: Sequence[OriginBoundCredential],
    target: _Target,
    base_headers: Mapping[str, str],
) -> tuple[OriginBoundCredential, ...]:
    if len(credentials) > 8:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    reserved = {name.lower() for name in base_headers}
    validated: list[OriginBoundCredential] = []
    for credential in credentials:
        current = _validate_credential(source_id, credential, target)
        name = current.header_name.lower()
        if name in reserved:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
        reserved.add(name)
        validated.append(current)
    result = tuple(validated)
    _reject_credentials_in_url(source_id, result, target.url)
    return result


def _serialize_request(
    source_id: str,
    method: str,
    target: _Target,
    headers: Mapping[str, str],
    body: bytes,
    *,
    accept_compression: bool = True,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> bytes:
    default_port = 443 if target.scheme == "https" else 80
    host_header = f"[{target.host}]" if ":" in target.host else target.host
    if target.port != default_port:
        host_header += f":{target.port}"
    lines = [
        f"{method} {target.path_and_query} HTTP/1.1",
        f"Host: {host_header}",
        "Connection: close",
        f"Accept-Encoding: {'gzip, deflate' if accept_compression else 'identity'}",
        f"User-Agent: {user_agent}",
    ]
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    if body or method in {"POST", "PUT", "PATCH"}:
        lines.append(f"Content-Length: {len(body)}")
    try:
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
    except UnicodeEncodeError:
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST) from None


def _peer_ip(stream: SocketLike) -> str:
    peer = stream.getpeername()
    if not isinstance(peer, tuple) or not peer:
        raise ValueError("invalid peer")
    return ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).compressed


def _response_headers(source_id: str, response: http.client.HTTPResponse, cap: int) -> dict[str, str]:
    result: dict[str, str] = {}
    total = 0
    for name, value in response.getheaders():
        total += len(name) + len(value) + 4
        if total > cap:
            raise SafeTransportError(source_id, SafeTransportReason.RESPONSE_TOO_LARGE)
        lowered = name.lower()
        if lowered in {"content-encoding", "content-type", "location"} and lowered in result:
            raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE)
        if lowered not in result:
            result[lowered] = value.strip()
    return result


def _content_length(source_id: str, response: http.client.HTTPResponse) -> int | None:
    values = response.headers.get_all("Content-Length", [])
    if not values:
        return None
    try:
        value = int(values[0], 10)
    except ValueError:
        raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE) from None
    if value < 0:
        raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE)
    return value


def _content_encodings(source_id: str, headers: Mapping[str, str], max_count: int) -> tuple[str, ...]:
    raw = headers.get("content-encoding", "identity")
    values = tuple(part.strip().lower() for part in raw.split(",") if part.strip()) or ("identity",)
    active = tuple(value for value in values if value != "identity")
    if len(active) > max_count or any(value not in _SUPPORTED_ENCODINGS for value in values):
        raise SafeTransportError(source_id, SafeTransportReason.UNSUPPORTED_CONTENT_ENCODING)
    return active


def _normalize_mime_pattern(source_id: str, raw: object) -> str:
    value = str(raw).split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+*-]+/[a-z0-9!#$&^_.+*-]+", value):
        raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
    return value


def _normalize_mime_patterns(source_id: str, values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for index, value in enumerate(values):
        if index >= 32:
            raise SafeTransportError(source_id, SafeTransportReason.INVALID_REQUEST)
        normalized = _normalize_mime_pattern(source_id, value)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _validate_response_mime(source_id: str, headers: Mapping[str, str], allowed: Sequence[str]) -> None:
    if not allowed:
        return
    actual = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not actual or not any(_mime_matches(actual, pattern) for pattern in allowed):
        raise SafeTransportError(source_id, SafeTransportReason.UNSUPPORTED_MIME_TYPE)


def _mime_matches(actual: str, pattern: str) -> bool:
    if actual == pattern:
        return True
    major, _minor = actual.split("/", 1) if "/" in actual else (actual, "")
    return pattern == f"{major}/*"


def _decode_body(source_id: str, body: bytes, encodings: Sequence[str], cap: int) -> bytes:
    decoded = body
    for encoding in reversed(encodings):
        wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
        try:
            decoded = _inflate_limited(decoded, wbits, cap)
        except _BodyTooLarge:
            raise SafeTransportError(source_id, SafeTransportReason.RESPONSE_TOO_LARGE) from None
        except _MalformedBody:
            raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE) from None
        except zlib.error:
            if encoding != "deflate":
                raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE) from None
            try:
                decoded = _inflate_limited(decoded, -zlib.MAX_WBITS, cap)
            except _BodyTooLarge:
                raise SafeTransportError(source_id, SafeTransportReason.RESPONSE_TOO_LARGE) from None
            except _MalformedBody:
                raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE) from None
            except zlib.error:
                raise SafeTransportError(source_id, SafeTransportReason.MALFORMED_RESPONSE) from None
        if len(decoded) > cap:
            raise SafeTransportError(source_id, SafeTransportReason.RESPONSE_TOO_LARGE)
    if len(decoded) > cap:
        raise SafeTransportError(source_id, SafeTransportReason.RESPONSE_TOO_LARGE)
    return decoded


def _inflate_limited(payload: bytes, wbits: int, cap: int) -> bytes:
    decoder = zlib.decompressobj(wbits)
    output = bytearray()
    position = 0
    while position < len(payload):
        chunk = payload[position : position + 64 * 1024]
        position += len(chunk)
        while chunk:
            part = decoder.decompress(chunk, cap - len(output) + 1)
            output.extend(part)
            if len(output) > cap:
                raise _BodyTooLarge
            chunk = decoder.unconsumed_tail
    output.extend(decoder.flush(cap - len(output) + 1))
    if len(output) > cap:
        raise _BodyTooLarge
    if not decoder.eof or decoder.unused_data:
        raise _MalformedBody
    return bytes(output)
