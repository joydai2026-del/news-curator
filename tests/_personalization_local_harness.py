"""Local-only Supabase API harness for personalization integration tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import pytest


MAX_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class LocalUser:
    user_id: str
    access_token: str


class LocalSupabase:
    def __init__(self, url: str, anon_key: str, service_key: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise AssertionError("The local Supabase harness refuses every non-loopback API origin.")
        if not anon_key or not service_key:
            raise AssertionError("The local Supabase harness requires both local API keys.")
        self.url = url.rstrip("/")
        self.anon_key = anon_key
        self.service_key = service_key
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        self._user_ids: list[str] = []

    @classmethod
    def from_environment(cls) -> "LocalSupabase":
        url = os.environ.get("NEWS_CURATOR_LOCAL_SUPABASE_URL", "")
        anon_key = os.environ.get("NEWS_CURATOR_LOCAL_SUPABASE_ANON_KEY", "")
        service_key = os.environ.get("NEWS_CURATOR_LOCAL_SUPABASE_SERVICE_ROLE_KEY", "")
        missing = [
            name
            for name, value in (
                ("NEWS_CURATOR_LOCAL_SUPABASE_URL", url),
                ("NEWS_CURATOR_LOCAL_SUPABASE_ANON_KEY", anon_key),
                ("NEWS_CURATOR_LOCAL_SUPABASE_SERVICE_ROLE_KEY", service_key),
            )
            if not value
        ]
        if missing:
            pytest.skip(
                "Local Supabase is unavailable: run `supabase start` and `supabase db reset`, then export "
                + ", ".join(missing)
                + " from the local status output."
            )
        return cls(url, anon_key, service_key)

    def request(
        self,
        method: str,
        path: str,
        *,
        apikey: str,
        bearer: str | None = None,
        body: Any = None,
        prefer: str | None = None,
    ) -> tuple[int, Any]:
        if not path.startswith("/"):
            raise AssertionError("Harness paths must be absolute API paths.")
        headers = {"apikey": apikey, "accept": "application/json"}
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        if prefer:
            headers["prefer"] = prefer
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
        except (urllib.error.URLError, TimeoutError) as exc:
            pytest.fail(f"Local Supabase API was configured but could not be reached: {type(exc).__name__}")
        if len(raw) > MAX_RESPONSE_BYTES:
            pytest.fail("Local Supabase returned an oversized response.")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            pytest.fail(f"Local Supabase returned non-JSON data with status {status}.")

    def create_user(self) -> LocalUser:
        marker = secrets.token_hex(12)
        email = f"news-curator-{marker}@example.invalid"
        password = f"Local-only-{secrets.token_urlsafe(24)}"
        status, payload = self.request(
            "POST",
            "/auth/v1/admin/users",
            apikey=self.service_key,
            bearer=self.service_key,
            body={"email": email, "password": password, "email_confirm": True},
        )
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            pytest.fail(f"Local Supabase could not create a test user, status {status}. Was `supabase db reset` run?")
        user_id = payload["id"]
        self._user_ids.append(user_id)
        status, session = self.request(
            "POST",
            "/auth/v1/token?grant_type=password",
            apikey=self.anon_key,
            body={"email": email, "password": password},
        )
        if status != 200 or not isinstance(session, dict) or not isinstance(session.get("access_token"), str):
            pytest.fail(f"Local Supabase could not sign in a test user, status {status}.")
        return LocalUser(user_id=user_id, access_token=session["access_token"])

    def cleanup(self) -> None:
        for user_id in reversed(self._user_ids):
            self.request(
                "DELETE",
                f"/auth/v1/admin/users/{urllib.parse.quote(user_id, safe='')}",
                apikey=self.service_key,
                bearer=self.service_key,
            )
        self._user_ids.clear()

    def expired_access_token(self, user_id: str) -> str:
        """Create an actually expired local JWT without exposing the signing secret."""
        jwt_secret = os.environ.get("NEWS_CURATOR_LOCAL_SUPABASE_JWT_SECRET", "")
        if not jwt_secret:
            pytest.skip(
                "The expired-JWT API case requires NEWS_CURATOR_LOCAL_SUPABASE_JWT_SECRET "
                "from the local `supabase status -o env` output."
            )
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "aud": "authenticated",
            "exp": now - 60,
            "iat": now - 120,
            "iss": f"{self.url}/auth/v1",
            "role": "authenticated",
            "sub": user_id,
        }

        def encode(value: dict[str, object]) -> str:
            raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        signing_input = f"{encode(header)}.{encode(payload)}"
        signature = hmac.new(
            jwt_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return f"{signing_input}.{encoded_signature}"

    def rest(
        self,
        user: LocalUser,
        method: str,
        path: str,
        *,
        body: Any = None,
        prefer: str | None = None,
    ) -> tuple[int, Any]:
        return self.request(
            method,
            path,
            apikey=self.anon_key,
            bearer=user.access_token,
            body=body,
            prefer=prefer,
        )


def preference_payload(user_id: str, *, locale: str = "en") -> dict[str, Any]:
    return {
        "user_id": user_id,
        "locale": locale,
        "interests": ["agents"],
        "saved_searches": [{"id": "daily", "query": "agent news", "enabled": True}],
    }
