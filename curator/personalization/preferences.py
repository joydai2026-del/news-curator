"""Validated owner-only preference access through Supabase REST and RPC APIs."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .auth import AuthConfig, AuthError, Session, _INVALID_JSON, _parse_json_without_retaining_input


MAX_RESPONSE_BYTES = 64 * 1024
MAX_INPUT_BYTES = 16 * 1024


class RestTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, Any]: ...


class JsonRestTransport:
    """Bounded JSON transport with redirects disabled and safe errors."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    def __init__(self) -> None:
        self._proxy_handler = urllib.request.ProxyHandler({})
        self._opener = urllib.request.build_opener(
            self._proxy_handler,
            self._NoRedirect,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if response.geturl() != url:
                    raise AuthError("The preference endpoint redirected unexpectedly.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AuthError("The preference response was invalid.")
                if not raw:
                    return response.status, None
                payload = _parse_json_without_retaining_input(raw)
                if payload is _INVALID_JSON:
                    raise AuthError("The preference service could not be reached.")
                return response.status, payload
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AuthError("The preference service could not be reached.") from exc


def _bounded_text(value: Any, *, chars: int, encoded_bytes: int) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= chars
        and len(value.encode("utf-8")) <= encoded_bytes
    )


@dataclass(frozen=True)
class PreferenceInput:
    expected_revision: int
    locale: str
    interests: tuple[str, ...]
    saved_searches: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PreferenceInput":
        if set(value) != {"expected_revision", "locale", "interests", "saved_searches"}:
            raise ValueError("Preference input has unknown or missing fields.")
        revision = value["expected_revision"]
        locale = value["locale"]
        interests = value["interests"]
        searches = value["saved_searches"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("expected_revision must be a non-negative integer.")
        if locale not in ("en", "zh"):
            raise ValueError("locale must be en or zh.")
        if not isinstance(interests, list) or len(interests) > 20:
            raise ValueError("interests must be a bounded array.")
        if not all(_bounded_text(item, chars=80, encoded_bytes=160) for item in interests):
            raise ValueError("interests contains an invalid item.")
        if not isinstance(searches, list) or len(searches) > 20:
            raise ValueError("saved_searches must be a bounded array.")

        validated: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for item in searches:
            if not isinstance(item, dict) or set(item) != {"id", "query", "enabled"}:
                raise ValueError("saved_searches contains invalid fields.")
            item_id = item["id"]
            query = item["query"]
            enabled = item["enabled"]
            if (
                not _bounded_text(item_id, chars=64, encoded_bytes=128)
                or not _bounded_text(query, chars=300, encoded_bytes=600)
                or not isinstance(enabled, bool)
                or item_id in seen
            ):
                raise ValueError("saved_searches contains an invalid item.")
            seen.add(item_id)
            validated.append({"id": item_id, "query": query, "enabled": enabled})
        serialized = json.dumps(validated, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(serialized) > 8192:
            raise ValueError("saved_searches is too large.")
        return cls(revision, locale, tuple(interests), tuple(validated))

    def fields(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "interests": list(self.interests),
            "saved_searches": [dict(item) for item in self.saved_searches],
        }


@dataclass(frozen=True)
class PreferenceRecord:
    user_id: str
    revision: int
    locale: str
    interests: tuple[str, ...]
    saved_searches: tuple[Mapping[str, Any], ...]
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PreferenceRecord":
        allowed = {"user_id", "revision", "locale", "interests", "saved_searches", "created_at", "updated_at"}
        required = allowed - {"created_at", "updated_at"}
        if set(value) - allowed or not required <= set(value):
            raise AuthError("The preference response was invalid.")
        user_id = value.get("user_id")
        revision = value.get("revision")
        if not isinstance(user_id, str) or not user_id or isinstance(revision, bool) or not isinstance(revision, int):
            raise AuthError("The preference response was invalid.")
        try:
            validated = PreferenceInput.from_mapping(
                {
                    "expected_revision": revision,
                    "locale": value.get("locale"),
                    "interests": value.get("interests"),
                    "saved_searches": value.get("saved_searches"),
                }
            )
        except ValueError as exc:
            raise AuthError("The preference response was invalid.") from exc
        created_at = value.get("created_at")
        updated_at = value.get("updated_at")
        if (created_at is not None and not isinstance(created_at, str)) or (
            updated_at is not None and not isinstance(updated_at, str)
        ):
            raise AuthError("The preference response was invalid.")
        return cls(
            user_id=user_id,
            revision=revision,
            locale=validated.locale,
            interests=validated.interests,
            saved_searches=validated.saved_searches,
            created_at=created_at,
            updated_at=updated_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "revision": self.revision,
            "locale": self.locale,
            "interests": list(self.interests),
            "saved_searches": [dict(item) for item in self.saved_searches],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class PreferenceClient:
    def __init__(self, config: AuthConfig, *, transport: RestTransport | None = None) -> None:
        self.config = config
        self.transport = transport or JsonRestTransport()

    def _headers(self, session: Session, *, representation: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self.config.publishable_key,
            "authorization": f"Bearer {session.access_token}",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if representation:
            headers["prefer"] = "return=representation"
        return headers

    def get(self, session: Session) -> PreferenceRecord | None:
        query = urllib.parse.urlencode(
            {
                "select": "user_id,revision,locale,interests,saved_searches,created_at,updated_at",
                "limit": "2",
            }
        )
        status, payload = self.transport.request(
            "GET",
            f"{self.config.supabase_url}/rest/v1/user_preferences?{query}",
            headers=self._headers(session),
        )
        if status != 200 or not isinstance(payload, list) or len(payload) > 1:
            raise AuthError("Preferences could not be read.")
        if not payload:
            return None
        if not isinstance(payload[0], dict):
            raise AuthError("The preference response was invalid.")
        record = PreferenceRecord.from_mapping(payload[0])
        if session.user_id is not None and record.user_id != session.user_id:
            raise AuthError("The preference response was invalid.")
        return record

    def set(self, session: Session, update: PreferenceInput) -> dict[str, Any]:
        fields = update.fields()
        status, outcome = self.transport.request(
            "POST",
            f"{self.config.supabase_url}/rest/v1/rpc/compare_and_swap_user_preferences",
            headers=self._headers(session),
            body={
                "expected_revision": update.expected_revision,
                "new_locale": fields["locale"],
                "new_interests": fields["interests"],
                "new_saved_searches": fields["saved_searches"],
            },
        )
        if status != 200 or not isinstance(outcome, dict) or outcome.get("status") not in {
            "updated",
            "conflict",
            "not_found",
        }:
            raise AuthError("Preferences could not be updated.")
        if outcome["status"] == "updated":
            record = self.get(session)
            if record is None:
                raise AuthError("The updated preference row could not be read.")
            return {"status": "updated", "preference": record.as_dict()}
        if outcome["status"] == "conflict":
            revision = outcome.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise AuthError("The preference response was invalid.")
            return {"status": "conflict", "revision": revision}
        if update.expected_revision != 0:
            return {"status": "not_found"}

        if not session.user_id:
            raise AuthError("The signed-in user identity is unavailable.")
        insert_status, inserted = self.transport.request(
            "POST",
            f"{self.config.supabase_url}/rest/v1/user_preferences",
            headers=self._headers(session, representation=True),
            body={"user_id": session.user_id, **fields},
        )
        if insert_status in (200, 201) and isinstance(inserted, list) and len(inserted) == 1 and isinstance(inserted[0], dict):
            return {"status": "created", "preference": PreferenceRecord.from_mapping(inserted[0]).as_dict()}
        if insert_status == 409:
            current = self.get(session)
            return {"status": "conflict", "revision": current.revision if current else None}
        raise AuthError("Preferences could not be created.")
