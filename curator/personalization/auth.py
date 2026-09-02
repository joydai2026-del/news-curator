"""Authorization Code with PKCE for the agent personalization client.

The module deliberately accepts an injected transport, clock, and token store.
This keeps auth lifecycle tests local and prevents credentials from entering logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


class AuthError(RuntimeError):
    """A deliberately non-sensitive authentication failure."""


_INVALID_JSON = object()
_MAX_TOKEN_CHARS = 16_384
_MAX_USER_ID_CHARS = 256
_MAX_SESSION_SECONDS = 86_400


def _is_bounded_string(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _parse_json_without_retaining_input(value: str | bytes) -> Any:
    """Parse JSON without attaching the token-bearing input to later errors."""

    parsed: Any = _INVALID_JSON
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return parsed


class Transport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, Mapping[str, Any]]: ...


class TokenStorage(Protocol):
    def load(self) -> "Session | None": ...

    def save(self, session: "Session") -> None: ...

    def clear(self) -> None: ...


def _decode_jwt_payload(value: str) -> Mapping[str, Any] | None:
    if not value or len(value) > 16_384:
        return None
    parts = value.split(".")
    if len(parts) != 3 or not parts[1] or len(parts[1]) > 12_000:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(parts[1] + padding)
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_publishable_key(value: str) -> bool:
    if not value or len(value) > 8192 or value.startswith("sb_secret_"):
        return False
    if value.startswith("sb_publishable_"):
        return len(value) > len("sb_publishable_")
    payload = _decode_jwt_payload(value)
    return payload is not None and payload.get("role") == "anon"


@dataclass(frozen=True)
class AuthConfig:
    supabase_url: str
    publishable_key: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.supabase_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "*" in parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Supabase URL must be an HTTPS origin.")
        if not _is_publishable_key(self.publishable_key):
            raise ValueError("A public publishable or anon key is required.")
        object.__setattr__(self, "supabase_url", self.supabase_url.rstrip("/"))


@dataclass(frozen=True)
class Session:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    user_id: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, now: float) -> "Session":
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        expires_at = payload.get("expires_at")
        expires_in = payload.get("expires_in")
        if (
            not _is_bounded_string(access, _MAX_TOKEN_CHARS)
            or not _is_bounded_string(refresh, _MAX_TOKEN_CHARS)
        ):
            raise AuthError("The authentication response was invalid.")
        if (
            not isinstance(expires_at, bool)
            and isinstance(expires_at, (int, float))
            and math.isfinite(float(expires_at))
            and float(expires_at).is_integer()
            and now < float(expires_at) <= now + _MAX_SESSION_SECONDS
        ):
            expiry = float(expires_at)
        elif (
            not isinstance(expires_in, bool)
            and isinstance(expires_in, (int, float))
            and math.isfinite(float(expires_in))
            and 0 < float(expires_in) <= _MAX_SESSION_SECONDS
        ):
            expiry = now + float(expires_in)
        else:
            raise AuthError("The authentication response was invalid.")
        user = payload.get("user")
        user_id = user.get("id") if isinstance(user, dict) and isinstance(user.get("id"), str) else None
        if not _is_bounded_string(user_id, _MAX_USER_ID_CHARS):
            raise AuthError("The authentication response was invalid.")
        identity = _decode_jwt_payload(access)
        subject = identity.get("sub") if identity else None
        if not _is_bounded_string(subject, _MAX_USER_ID_CHARS) or subject != user_id:
            raise AuthError("The authentication response was invalid.")
        return cls(access_token=access, refresh_token=refresh, expires_at=expiry, user_id=user_id)

    @classmethod
    def from_refresh_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        current: "Session",
        now: float,
    ) -> "Session":
        """Project a refresh response onto the minimal, identity-bound session."""

        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        user = payload.get("user")
        returned_user_id = user.get("id") if isinstance(user, dict) else None
        if (
            not _is_bounded_string(current.user_id, _MAX_USER_ID_CHARS)
            or not _is_bounded_string(current.refresh_token, _MAX_TOKEN_CHARS)
            or not _is_bounded_string(access, _MAX_TOKEN_CHARS)
            or not _is_bounded_string(refresh, _MAX_TOKEN_CHARS)
            or refresh == current.refresh_token
            or not _is_bounded_string(returned_user_id, _MAX_USER_ID_CHARS)
            or returned_user_id != current.user_id
        ):
            raise AuthError("The refreshed session was invalid.")

        expires_at = payload.get("expires_at")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(expires_at, bool)
            and isinstance(expires_at, (int, float))
            and float("-inf") < float(expires_at) < float("inf")
            and float(expires_at).is_integer()
            and now < float(expires_at) <= now + _MAX_SESSION_SECONDS
        ):
            expiry = float(expires_at)
        elif (
            not isinstance(expires_in, bool)
            and isinstance(expires_in, (int, float))
            and float("-inf") < float(expires_in) < float("inf")
            and 0 < float(expires_in) <= _MAX_SESSION_SECONDS
        ):
            expiry = now + float(expires_in)
        else:
            raise AuthError("The refreshed session was invalid.")

        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=expiry,
            user_id=current.user_id,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "user_id": self.user_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> "Session":
        payload = _parse_json_without_retaining_input(value)
        if (
            payload is _INVALID_JSON
            or not isinstance(payload, dict)
            or set(payload) != {
                "access_token",
                "refresh_token",
                "expires_at",
                "user_id",
            }
        ):
            raise AuthError("Protected session data was invalid.")
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        expires_at = payload.get("expires_at")
        user_id = payload.get("user_id")
        if (
            not _is_bounded_string(access, _MAX_TOKEN_CHARS)
            or not _is_bounded_string(refresh, _MAX_TOKEN_CHARS)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= 0
            or not _is_bounded_string(user_id, _MAX_USER_ID_CHARS)
        ):
            raise AuthError("Protected session data was invalid.")
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=float(expires_at),
            user_id=user_id,
        )


class MemoryTokenStorage:
    """Explicit process-memory-only storage. Nothing survives process exit."""

    def __init__(self) -> None:
        self._session: Session | None = None

    def load(self) -> Session | None:
        return self._session

    def save(self, session: Session) -> None:
        self._session = session

    def clear(self) -> None:
        self._session = None


class MacOSKeychainStorage:
    """Persist the session only in the current user's macOS Keychain."""

    SECURITY = "/usr/bin/security"

    def __init__(self, *, account: str, service: str = "news-curator.personalization") -> None:
        if sys.platform != "darwin":
            raise AuthError("Protected token storage is unavailable; use --memory-only explicitly.")
        if not account or not service:
            raise ValueError("Keychain account and service are required.")
        self.account = account
        self.service = service

    def _run(self, args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.SECURITY, *args],
                input=input_text,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuthError("Protected token storage failed.") from exc

    def load(self) -> Session | None:
        result = self._run(["find-generic-password", "-a", self.account, "-s", self.service, "-w"])
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise AuthError("Protected token storage failed.")
        return Session.from_json(result.stdout.rstrip("\n"))

    def save(self, session: Session) -> None:
        # Keeping -w last makes the security tool read the secret from stdin,
        # instead of exposing it in the process argument list.
        result = self._run(
            ["add-generic-password", "-a", self.account, "-s", self.service, "-U", "-w"],
            input_text=session.to_json(),
        )
        if result.returncode != 0:
            raise AuthError("Protected token storage failed.")

    def clear(self) -> None:
        result = self._run(["delete-generic-password", "-a", self.account, "-s", self.service])
        if result.returncode not in (0, 44):
            raise AuthError("Protected token storage failed.")


class UrlLibTransport:
    """Small JSON POST transport that does not expose response bodies in errors."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    def __init__(self) -> None:
        self._proxy_handler = urllib.request.ProxyHandler({})
        self._opener = urllib.request.build_opener(
            self._proxy_handler,
            self._NoRedirect,
        )

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, Mapping[str, Any]]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body or {}).encode("utf-8"),
            method="POST",
            headers=dict(headers),
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.geturl() != url:
                    raise AuthError("The authentication endpoint redirected unexpectedly.")
                raw = response.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise AuthError("The authentication response was invalid.")
                payload = _parse_json_without_retaining_input(raw)
                if payload is _INVALID_JSON:
                    raise AuthError("The authentication service could not be reached.")
                return response.status, payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AuthError("The authentication service could not be reached.") from exc


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_loopback_redirect(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid loopback redirect.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.path != "/callback"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Redirect must be an exact 127.0.0.1 loopback callback.")
    return f"http://127.0.0.1:{port}/callback"


@dataclass
class LoginAttempt:
    redirect_uri: str
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    consumed: bool = False

    def consume_callback(self, callback_url: str) -> str:
        if self.consumed:
            raise AuthError("The sign-in response was already used.")
        self.consumed = True
        callback = urllib.parse.urlsplit(callback_url)
        expected = urllib.parse.urlsplit(self.redirect_uri)
        if (callback.scheme, callback.hostname, callback.port, callback.path) != (
            expected.scheme,
            expected.hostname,
            expected.port,
            expected.path,
        ):
            raise AuthError("The sign-in redirect was invalid.")
        values = urllib.parse.parse_qs(callback.query, keep_blank_values=True)
        expected_values = urllib.parse.parse_qs(expected.query, keep_blank_values=True)
        if (
            set(values) - {"code", "client_state"}
            or len(values.get("client_state", [])) != 1
            or len(values.get("code", [])) != 1
            or expected_values != {"client_state": [self.state]}
        ):
            raise AuthError("The sign-in response was invalid.")
        if not secrets.compare_digest(values["client_state"][0], self.state):
            raise AuthError("The sign-in response could not be verified.")
        code = values["code"][0]
        if not code or len(code) > 4096:
            raise AuthError("The sign-in response was invalid.")
        return code


class AgentAuth:
    def __init__(
        self,
        config: AuthConfig,
        storage: TokenStorage,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.storage = storage
        self.transport = transport or UrlLibTransport()
        self.clock = clock

    def begin_login(self, redirect_uri: str) -> tuple[LoginAttempt, str]:
        redirect = _validate_loopback_redirect(redirect_uri)
        verifier = _base64url(secrets.token_bytes(48))
        state = _base64url(secrets.token_bytes(32))
        state_redirect = f"{redirect}?{urllib.parse.urlencode({'client_state': state})}"
        attempt = LoginAttempt(redirect_uri=state_redirect, state=state, verifier=verifier)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        params = urllib.parse.urlencode(
            {
                "provider": "google",
                "redirect_to": state_redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return attempt, f"{self.config.supabase_url}/auth/v1/authorize?{params}"

    def finish_login(self, attempt: LoginAttempt, callback_url: str) -> Session:
        code = attempt.consume_callback(callback_url)
        status, payload = self.transport.post(
            f"{self.config.supabase_url}/auth/v1/token?grant_type=pkce",
            headers={"apikey": self.config.publishable_key, "content-type": "application/json"},
            body={"auth_code": code, "code_verifier": attempt.verifier},
        )
        if status != 200:
            raise AuthError("Sign in failed.")
        session = Session.from_payload(payload, now=self.clock())
        self.storage.save(session)
        return session

    def refresh(self) -> Session:
        load_failed = False
        current: Session | None = None
        try:
            current = self.storage.load()
        except Exception:
            load_failed = True
        if load_failed:
            self._raise_failed_refresh()
        if current is None:
            raise AuthError("No saved session exists.")

        failed = False
        rotated: Session | None = None
        try:
            if (
                not _is_bounded_string(current.access_token, _MAX_TOKEN_CHARS)
                or not _is_bounded_string(current.refresh_token, _MAX_TOKEN_CHARS)
                or not _is_bounded_string(current.user_id, _MAX_USER_ID_CHARS)
                or isinstance(current.expires_at, bool)
                or not isinstance(current.expires_at, (int, float))
                or not float("-inf") < float(current.expires_at) < float("inf")
                or current.expires_at <= 0
            ):
                raise AuthError("The saved session was invalid.")
            status, payload = self.transport.post(
                f"{self.config.supabase_url}/auth/v1/token?grant_type=refresh_token",
                headers={"apikey": self.config.publishable_key, "content-type": "application/json"},
                body={"refresh_token": current.refresh_token},
            )
            if status != 200 or not isinstance(payload, Mapping):
                raise AuthError("The refreshed session was invalid.")
            rotated = Session.from_refresh_payload(payload, current=current, now=self.clock())
            self.storage.save(rotated)
        except Exception:
            failed = True
        if failed or rotated is None:
            self._raise_failed_refresh()
        return rotated

    def _raise_failed_refresh(self) -> None:
        """Erase refresh state and raise without retaining a token-bearing exception."""

        cleared = True
        try:
            self.storage.clear()
        except Exception:
            cleared = False
        message = (
            "Session refresh failed; the saved session was erased."
            if cleared
            else "Session refresh failed; protected storage could not be cleared."
        )
        raise AuthError(message) from None

    def valid_session(self, *, leeway: float = 30.0) -> Session:
        session = self.storage.load()
        if session is None:
            raise AuthError("No saved session exists.")
        if session.expires_at <= self.clock() + leeway:
            return self.refresh()
        return session

    def logout(self) -> None:
        session: Session | None = None
        load_failed = False
        remote_failed = False
        clear_failed = False
        try:
            try:
                session = self.storage.load()
            except Exception:
                load_failed = True
            if session is not None:
                try:
                    status, _ = self.transport.post(
                        f"{self.config.supabase_url}/auth/v1/logout",
                        headers={
                            "apikey": self.config.publishable_key,
                            "authorization": f"Bearer {session.access_token}",
                            "content-type": "application/json",
                        },
                    )
                    remote_failed = status not in (200, 204, 401)
                except Exception:
                    remote_failed = True
        finally:
            try:
                self.storage.clear()
            except Exception:
                clear_failed = True

        if clear_failed:
            raise AuthError("Logout failed; protected storage could not be cleared.") from None
        if load_failed:
            raise AuthError("Logout could not read the saved session; protected storage was cleared.") from None
        if remote_failed:
            raise AuthError("Remote logout failed; the saved session was erased.") from None
