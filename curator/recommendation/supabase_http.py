"""Small concrete Supabase transport for the ranking service."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping


class SupabaseHTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        # The request that failed, WITHOUT its query string. "Supabase request
        # failed" on its own is unactionable: it cannot tell a 401 on one RPC
        # from a client-side timeout on the heavy candidate query, which is the
        # exact ambiguity that cost a production debugging session on
        # 2026-09-21. The query string is dropped because it carries user ids.
        self.path = path


class SupabaseAuthenticationError(SupabaseHTTPError):
    pass


DEFAULT_TIMEOUT_SECONDS = 3.0
MINIMUM_TIMEOUT_SECONDS = 1.0
MAXIMUM_TIMEOUT_SECONDS = 30.0


def validate_timeout_seconds(value) -> float:
    """The client-side budget for one Supabase call, in seconds.

    Operational, so it is policy rather than a literal: the heavy candidate
    query grows with the corpus and the old hardcoded 3.0s turned that growth
    into an opaque 503. Bounded on both sides, because 0 would mean "never wait"
    and an unbounded value would hold a Modal container open behind a dead
    database.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("supabase.timeout_seconds must be a number of seconds")
    value = float(value)
    if not MINIMUM_TIMEOUT_SECONDS <= value <= MAXIMUM_TIMEOUT_SECONDS:
        raise ValueError(
            f"supabase.timeout_seconds must be between {MINIMUM_TIMEOUT_SECONDS} "
            f"and {MAXIMUM_TIMEOUT_SECONDS} seconds")
    return value


def _failure_reason(exc) -> str:
    """"timeout" (we gave up waiting) versus "url" (we could not reach it)."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, OSError)):
        return "timeout" if isinstance(exc.reason, TimeoutError) else "url"
    return "url"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_https_origin(origin: str) -> str:
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Supabase origin must be a fixed HTTPS origin")
    return origin


class SupabaseHTTP:
    def __init__(self, *, origin: str, publishable_key: str, service_role_key: str,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        validate_https_origin(origin)
        if not publishable_key or not service_role_key:
            raise ValueError("Supabase keys must be configured")
        self._origin, self._publishable, self._service = origin, publishable_key, service_role_key
        self._timeout = validate_timeout_seconds(timeout_seconds)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _service_token(self) -> str:
        return "" if self._service.startswith("sb_secret_") else self._service

    def get_user(self, access_token: str) -> Mapping[str, object]:
        try:
            return self._request("GET", "/auth/v1/user", token=access_token, key=self._publishable)
        except SupabaseHTTPError as error:
            if error.status_code == 401:
                raise SupabaseAuthenticationError("Supabase rejected the user session",
                    status_code=401, path=error.path) from error
            raise

    def history_snapshot(self, access_token: str) -> Mapping[str, object]:
        return self._request("POST", "/rest/v1/rpc/m2_history_snapshot", token=access_token,
            key=self._publishable, body={"p_limit": None})

    def retained_candidates(self, *, category_id: str | None, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None):
        rows, before_published, before_story = [], before_published_at, before_story_id
        while len(rows) < limit:
            take = min(100, limit - len(rows))
            page = self._request("POST", "/rest/v1/rpc/m2_retained_candidates", token=self._service_token(),
                key=self._service, body={"p_category_id": category_id, "p_query": query,
                    "p_before_published_at": before_published, "p_before_story_id": before_story, "p_limit": take})
            if not isinstance(page, list):
                raise SupabaseHTTPError("candidate RPC returned a non-list")
            rows.extend(page)
            if len(page) < take:
                break
            before_published, before_story = page[-1]["published_at"], page[-1]["story_id"]
        return rows

    def retained_candidates_language_exclusive(self, *, display_language: str, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None,
                            policy_id: str | None = None):
        rows, before_published, before_story = [], before_published_at, before_story_id
        while len(rows) < limit:
            take = min(100, limit - len(rows))
            page = self._request("POST", "/rest/v1/rpc/m2_retained_candidates_language_exclusive",
                token=self._service_token(), key=self._service,
                body={"p_display_language": display_language, "p_query": query,
                    "p_before_published_at": before_published, "p_before_story_id": before_story,
                    "p_limit": take, "p_policy_id": policy_id})
            if not isinstance(page, list):
                raise SupabaseHTTPError("candidate RPC returned a non-list")
            rows.extend(page)
            if len(page) < take:
                break
            before_published, before_story = page[-1]["published_at"], page[-1]["story_id"]
        return rows

    def retained_candidates_v2(self, *, category_id: str | None, query: str | None, lane: str | None,
                            profile_categories, profile_sources, trend_window_hours: int,
                            trend_min_sources: int, max_age_hours: int | None, min_age_hours: int | None,
                            limit: int, before_published_at: str | None = None,
                            before_story_id: str | None = None, before_source_count: int | None = None):
        # One request per lane. The hot lane pages on its FULL sort key
        # (independent_source_count, published_at, story_id), because its
        # ordering leads with the count and a published_at-only keyset would skip
        # or repeat rows at the page boundary.
        page = self._request("POST", "/rest/v1/rpc/m2_retained_candidates_v2", token=self._service_token(),
            key=self._service, body={"p_category_id": category_id, "p_query": query, "p_lane": lane,
                "p_profile_categories": list(profile_categories), "p_profile_sources": list(profile_sources),
                "p_trend_window_hours": trend_window_hours, "p_trend_min_sources": trend_min_sources,
                "p_max_age_hours": max_age_hours, "p_min_age_hours": min_age_hours,
                "p_before_published_at": before_published_at,
                "p_before_story_id": before_story_id,
                "p_before_source_count": before_source_count,
                "p_limit": min(100, max(1, limit))})
        if not isinstance(page, list):
            raise SupabaseHTTPError("candidate RPC returned a non-list")
        return page

    def open_reading_run(self, *, user_id: str, idle_minutes: int, max_minutes: int, profile):
        result = self._request("POST", "/rest/v1/rpc/m2_open_or_join_reading_run_v2", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_idle_minutes": idle_minutes,
                "p_max_minutes": max_minutes, "p_profile": dict(profile)})
        if not isinstance(result, Mapping):
            raise SupabaseHTTPError("reading run RPC returned a non-object")
        return result

    def reserve_budget(self, *, user_id: str, request_id: str, amount_usd: float, daily_limit_usd: float) -> bool:
        result = self._request("POST", "/rest/v1/rpc/m2_reserve_ranker_budget", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_request_id": request_id,
                "p_amount_usd": amount_usd, "p_daily_limit_usd": daily_limit_usd})
        return result is True

    def reserve_budget_claimed(self, *, user_id: str, request_id: str, amount_usd: float,
                               daily_limit_usd: float, run_id: str, eligibility_key: str,
                               claim_token: str) -> bool:
        result = self._request("POST", "/rest/v1/rpc/m2_reserve_ranker_budget_claimed",
            token=self._service_token(), key=self._service,
            body={"p_user_id": user_id, "p_request_id": request_id, "p_amount_usd": amount_usd,
                  "p_daily_limit_usd": daily_limit_usd, "p_run_id": run_id,
                  "p_eligibility_key": eligibility_key, "p_claim_token": claim_token})
        # An OBJECT, so "you lost the claim" and "you are out of budget" are
        # different answers rather than one indistinguishable false.
        if not isinstance(result, Mapping):
            raise SupabaseHTTPError("claimed reservation RPC returned a non-object")
        return result

    def owner_states(self, access_token: str, story_ids):
        result = self._request("POST", "/rest/v1/rpc/m2_owner_story_states", token=access_token,
            key=self._publishable, body={"p_story_ids": list(story_ids)})
        if not isinstance(result, list):
            raise SupabaseHTTPError("owner state RPC returned a non-list")
        return {str(row["story_id"]): row for row in result}

    def settle_budget(self, *, user_id: str, request_id: str, actual_usd: float, status: str) -> None:
        self._request("POST", "/rest/v1/rpc/m2_settle_ranker_budget", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_request_id": request_id,
                "p_actual_usd": actual_usd, "p_status": status})

    def save_frozen_order(self, *, user_id: str, request_id: str, bindings, cards, page_size: int,
                          expires_at: int, run_id: str | None = None) -> str:
        result = self._request("POST", "/rest/v1/m2_frozen_rankings", token=self._service_token(), key=self._service,
            body={"user_id": user_id, "request_id": request_id, "bindings": bindings, "cards": cards,
                "page_size": page_size, "expires_at": self._iso_timestamp(expires_at),
                # The run this order belongs to. Inside an open run the epoch
                # trigger tolerates a behavior write that landed while the
                # provider was answering, so a paid order is never discarded.
                "run_id": run_id}, prefer="return=representation")
        if not isinstance(result, list) or len(result) != 1:
            raise SupabaseHTTPError("frozen order insert did not return one row")
        return str(result[0]["frozen_order_id"])

    def load_frozen_order(self, *, user_id: str, frozen_order_id: str):
        query = urllib.parse.urlencode({"select": "bindings,cards,page_size,expires_at",
            "user_id": f"eq.{user_id}", "frozen_order_id": f"eq.{frozen_order_id}"})
        result = self._request("GET", f"/rest/v1/m2_frozen_rankings?{query}", token=self._service_token(), key=self._service)
        if not isinstance(result, list):
            raise SupabaseHTTPError("frozen order read returned a non-list")
        if not result:
            return None
        row = result[0]
        row["expires_at"] = int(__import__("datetime").datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).timestamp())
        return row

    def record_reading_run_filter(self, *, user_id: str, run_id: str, story_ids) -> int:
        result = self._request("POST", "/rest/v1/rpc/m2_record_reading_run_filter",
            token=self._service_token(), key=self._service,
            body={"p_user_id": user_id, "p_run_id": run_id, "p_story_ids": list(story_ids)})
        return result if isinstance(result, int) else 0

    def open_run_view(self, *, user_id: str, run_id: str, eligibility_key: str):
        result = self._request("POST", "/rest/v1/rpc/m2_open_run_view", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_run_id": run_id,
                "p_eligibility_key": eligibility_key})
        if not isinstance(result, Mapping):
            raise SupabaseHTTPError("run view RPC returned a non-object")
        return result

    def bind_run_frozen_order(self, *, user_id: str, run_id: str, eligibility_key: str,
                              frozen_order_id: str, token: str | None = None) -> bool:
        result = self._request("POST", "/rest/v1/rpc/m2_bind_run_frozen_order", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_run_id": run_id,
                "p_eligibility_key": eligibility_key, "p_frozen_order_id": frozen_order_id,
                "p_token": token})
        return result is True

    def claim_run_ranking(self, *, user_id: str, run_id: str, eligibility_key: str, token: str,
                          ttl_seconds: int):
        result = self._request("POST", "/rest/v1/rpc/m2_claim_run_ranking", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_run_id": run_id,
                "p_eligibility_key": eligibility_key, "p_token": token, "p_ttl_seconds": ttl_seconds})
        if not isinstance(result, Mapping):
            raise SupabaseHTTPError("ranking claim RPC returned a non-object")
        return result

    def release_run_ranking_claim(self, *, user_id: str, run_id: str, eligibility_key: str,
                                  token: str) -> bool:
        result = self._request("POST", "/rest/v1/rpc/m2_release_run_ranking_claim",
            token=self._service_token(), key=self._service,
            body={"p_user_id": user_id, "p_run_id": run_id, "p_eligibility_key": eligibility_key,
                  "p_token": token})
        return result is True

    def record_run_page(self, *, user_id: str, run_id: str, eligibility_key: str, pages: int) -> int:
        result = self._request("POST", "/rest/v1/rpc/m2_record_run_page", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_run_id": run_id,
                "p_eligibility_key": eligibility_key, "p_pages": pages})
        return result if isinstance(result, int) else 0

    def reserve_run_response(self, *, user_id: str, run_id: str, eligibility_key: str,
                             frozen_order_id: str, response_number: int,
                             offset: int, next_offset: int):
        result = self._request("POST", "/rest/v1/rpc/m2_reserve_run_response",
            token=self._service_token(), key=self._service,
            body={"p_user_id": user_id, "p_run_id": run_id,
                "p_eligibility_key": eligibility_key,
                "p_frozen_order_id": frozen_order_id,
                "p_response_number": response_number,
                "p_offset": offset, "p_next_offset": next_offset})
        if not isinstance(result, Mapping):
            raise SupabaseHTTPError("response reservation RPC returned a non-object")
        return result

    def extend_frozen_order(self, *, user_id: str, frozen_order_id: str, cards, bindings) -> int:
        result = self._request("POST", "/rest/v1/rpc/m2_extend_frozen_ranking", token=self._service_token(),
            key=self._service, body={"p_user_id": user_id, "p_frozen_order_id": frozen_order_id,
                "p_cards": list(cards), "p_bindings": dict(bindings)})
        return result if isinstance(result, int) else 0

    def _request(self, method, path, *, token, key, body=None, prefer=None):
        headers = {"apikey": key, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode(); headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        request = urllib.request.Request(self._origin + path, data=data, headers=headers, method=method)
        # Query strings on this transport carry user ids (the frozen-order read),
        # so only the route is ever recorded or reported.
        route = path.split("?", 1)[0]
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._failure(route, method, started, reason="http", status_code=exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise self._failure(route, method, started, reason=_failure_reason(exc)) from exc
        return None if not raw else json.loads(raw)

    def _failure(self, route, method, started, *, reason, status_code=None):
        """One structured line, then the exception that carries the same facts.

        Never the body, the headers, the key or the token: a Supabase error body
        can echo the statement, and the headers hold the service-role bearer.
        """
        print(json.dumps({"event": "m2_supabase_request_failed", "path": route,
            "method": method, "status_code": status_code,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "reason": reason},
            separators=(",", ":")), file=sys.stdout, flush=True)
        return SupabaseHTTPError("Supabase request failed", status_code=status_code, path=route)

    @staticmethod
    def _iso_timestamp(epoch: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
