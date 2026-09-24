import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from curator.recommendation import supabase_http as supabase_module
from curator.recommendation.supabase_http import (
    DEFAULT_TIMEOUT_SECONDS,
    SupabaseAuthenticationError,
    SupabaseHTTP,
    SupabaseHTTPError,
    _NoRedirect,
    validate_timeout_seconds,
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


def test_response_reservation_uses_the_atomic_rpc_with_exact_progress():
    client = _client()
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b'{"reserved":true,"previous":1}'

    class Capture:
        def open(self, request, timeout):
            seen.update(method=request.method, url=request.full_url,
                        body=json.loads(request.data), headers=dict(request.header_items()))
            return Response()

    client._opener = Capture()
    result = client.reserve_run_response(user_id="owner-1", run_id="run-1",
        eligibility_key="a" * 64, frozen_order_id="frozen-1",
        response_number=2, offset=25, next_offset=50)
    assert result == {"reserved": True, "previous": 1}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/rest/v1/rpc/m2_reserve_run_response")
    assert seen["body"] == {"p_user_id": "owner-1", "p_run_id": "run-1",
        "p_eligibility_key": "a" * 64, "p_frozen_order_id": "frozen-1",
        "p_response_number": 2, "p_offset": 25, "p_next_offset": 50}


def test_successful_rpc_timing_log_omits_query_and_credentials(capsys):
    client = _client()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b"[]"

    class Capture:
        def open(self, request, timeout): return Response()

    client._opener = Capture()
    assert client._request("GET", "/rest/v1/m2_frozen_rankings?user_id=private-owner",
                           token="private-token", key="private-key") == []
    event = json.loads(capsys.readouterr().err)
    assert set(event) == {"event", "path", "method", "elapsed_ms"}
    assert event["event"] == "m2_supabase_request_timing"
    assert event["path"] == "/rest/v1/m2_frozen_rankings"
    assert event["method"] == "GET"
    assert event["elapsed_ms"] >= 0


@pytest.mark.parametrize("failed_operation", ["write", "flush"])
def test_successful_rpc_survives_a_broken_timing_sink(monkeypatch, failed_operation):
    client = _client()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b"[]"

    class Capture:
        def open(self, request, timeout): return Response()

    class BrokenSink:
        def write(self, value):
            if failed_operation == "write": raise OSError("sink_down")
            return len(value)

        def flush(self):
            if failed_operation == "flush": raise OSError("sink_down")

    client._opener = Capture()
    monkeypatch.setattr(supabase_module, "sys", SimpleNamespace(stderr=BrokenSink()))
    assert client._request("GET", "/rest/v1/m2_frozen_rankings?user_id=private-owner",
                           token="private-token", key="private-key") == []


def test_failed_rpc_keeps_its_original_error_when_log_sink_fails(monkeypatch):
    client = _client()

    class Reject:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 500, "failed", {}, io.BytesIO())

    class BrokenSink:
        def write(self, value): raise OSError("sink_down")
        def flush(self): raise OSError("sink_down")

    client._opener = Reject()
    monkeypatch.setattr(supabase_module, "sys", SimpleNamespace(stdout=BrokenSink()))
    with pytest.raises(SupabaseHTTPError):
        client._request("GET", "/rest/v1/m2_frozen_rankings?user_id=private-owner",
                        token="private-token", key="private-key")


def _client(timeout_seconds=None):
    kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    return SupabaseHTTP(origin="https://example.test", publishable_key="public",
                        service_role_key="sb_secret_canary", **kwargs)


def _candidate_args():
    return dict(category_id=None, query=None, lane="hot", profile_categories=(),
                profile_sources=(), trend_window_hours=48, trend_min_sources=2,
                max_age_hours=None, min_age_hours=None, limit=12)


@pytest.mark.parametrize("owner", [None, "", "malformed", 123])
def test_owner_candidate_rpc_rejects_invalid_owner_before_transport(owner):
    client = _client()
    calls = []
    client._request = lambda *args, **kwargs: calls.append((args, kwargs)) or []
    with pytest.raises((ValueError, SupabaseHTTPError)):
        client.retained_candidates_v2(**_candidate_args(), owner_id=owner,
                                      hide_already_opened=True)
    assert calls == []


@pytest.mark.parametrize("hide", [None, 0, 1, "false"])
def test_owner_candidate_rpc_rejects_invalid_opened_policy_before_transport(hide):
    client = _client()
    calls = []
    client._request = lambda *args, **kwargs: calls.append((args, kwargs)) or []
    with pytest.raises(ValueError, match="verified owner required"):
        client.retained_candidates_v2(**_candidate_args(),
            owner_id="11111111-1111-1111-1111-111111111111", hide_already_opened=hide)
    assert calls == []


def test_owner_candidate_rpc_has_no_unfiltered_fallback_on_failure():
    client = _client()
    paths = []
    def unavailable(_method, path, **kwargs):
        paths.append(path)
        assert kwargs["body"]["p_owner_id"] == "11111111-1111-1111-1111-111111111111"
        assert kwargs["body"]["p_hide_already_opened"] is True
        raise SupabaseHTTPError("supabase request failed")
    client._request = unavailable
    with pytest.raises(SupabaseHTTPError, match="supabase request failed"):
        client.retained_candidates_v2(**_candidate_args(),
            owner_id="11111111-1111-1111-1111-111111111111", hide_already_opened=True)
    assert paths == ["/rest/v1/rpc/m2_retained_candidates_for_owner"]


def test_narrow_general_route_keeps_verified_owner_and_only_supported_arguments():
    client = SupabaseHTTP(origin="https://example.test", publishable_key="public",
                          service_role_key="sb_secret_canary", general_candidate_query="owner_narrow")
    calls = []
    client._request = lambda _method, path, **kwargs: calls.append((path, kwargs["body"])) or []
    args = dict(_candidate_args(), lane=None, owner_id="11111111-1111-1111-1111-111111111111",
                hide_already_opened=True, before_published_at="2026-09-23T00:00:00Z",
                before_story_id="story:before", excluded_story_ids=("story:excluded",),
                suppressed_sources=("source",), suppressed_topics=("topic",))
    client.retained_candidates_v2(**args)
    path, body = calls.pop()
    assert path == "/rest/v1/rpc/m2_retained_candidates_general_narrow_for_owner"
    assert body == {"p_owner_id": args["owner_id"], "p_hide_already_opened": True,
                    "p_trend_window_hours": 48, "p_before_published_at": args["before_published_at"],
                    "p_before_story_id": args["before_story_id"], "p_excluded_story_ids": ["story:excluded"],
                    "p_suppressed_sources": ["source"], "p_suppressed_topics": ["topic"], "p_limit": 12}
    for change in ({"lane": "hot"}, {"category_id": "ai"}, {"query": "AI"},
                   {"max_age_hours": 24}, {"min_age_hours": 6}, {"before_source_count": 2}):
        client.retained_candidates_v2(**(args | change))
        assert calls.pop()[0] == "/rest/v1/rpc/m2_retained_candidates_for_owner"



def test_narrow_interested_route_only_for_all_without_search_or_age_ceiling():
    client = SupabaseHTTP(origin="https://example.test", publishable_key="public",
                          service_role_key="sb_secret_canary", general_candidate_query="owner_narrow")
    calls = []
    client._request = lambda _method, path, **kwargs: calls.append((path, kwargs["body"])) or []
    args = dict(_candidate_args(), lane="interested", profile_categories=("ai",),
                profile_sources=("source",), min_age_hours=6,
                owner_id="11111111-1111-1111-1111-111111111111", hide_already_opened=True)
    client.retained_candidates_v2(**args)
    path, body = calls.pop()
    assert path == "/rest/v1/rpc/m2_retained_candidates_interested_narrow_for_owner"
    assert body == {"p_owner_id": args["owner_id"], "p_hide_already_opened": True,
                    "p_profile_categories": ["ai"], "p_profile_sources": ["source"],
                    "p_trend_window_hours": 48, "p_min_age_hours": 6,
                    "p_before_published_at": None, "p_before_story_id": None,
                    "p_excluded_story_ids": [], "p_suppressed_sources": [],
                    "p_suppressed_topics": [], "p_limit": 12}
    for change in ({"category_id": "ai"}, {"query": "AI"}, {"max_age_hours": 24},
                   {"min_age_hours": None}, {"before_source_count": 2}):
        client.retained_candidates_v2(**(args | change))
        assert calls.pop()[0] == "/rest/v1/rpc/m2_retained_candidates_for_owner"


def test_narrow_general_query_rejects_unknown_selector():
    with pytest.raises(ValueError, match="invalid general candidate query"):
        SupabaseHTTP(origin="https://example.test", publishable_key="public",
                     service_role_key="sb_secret_canary", general_candidate_query="unfiltered")


def test_candidate_rpc_uses_a_private_no_redirect_opener_per_lane_call(monkeypatch):
    client = _client()
    built = []
    opened = []

    class CandidateOpener:
        def open(self, request, timeout):
            opened.append((self, request.full_url))
            return io.BytesIO(b"[]")

    def build_opener(handler):
        assert handler is _NoRedirect
        opener = CandidateOpener()
        built.append(opener)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    for lane in ("updates", "hot"):
        assert client.retained_candidates_v2(
            category_id=None, query=None, lane=lane, profile_categories=(), profile_sources=(),
            trend_window_hours=48, trend_min_sources=2, max_age_hours=None, min_age_hours=None,
            limit=1, owner_id="11111111-1111-1111-1111-111111111111",
            hide_already_opened=True) == []
    assert len(built) == 2 and built[0] is not built[1]
    assert [opener for opener, _ in opened] == built
    assert all(url.endswith("/rest/v1/rpc/m2_retained_candidates_for_owner") for _, url in opened)


def test_general_candidate_rpc_can_read_two_hundred_while_lanes_stay_at_one_hundred():
    client = _client()
    limits = []
    filters = []

    def capture(_method, _path, **kwargs):
        limits.append(kwargs["body"]["p_limit"])
        filters.append((kwargs["body"]["p_excluded_story_ids"],
                        kwargs["body"]["p_suppressed_sources"],
                        kwargs["body"]["p_suppressed_topics"]))
        return []

    client._request = capture
    for lane in (None, "updates"):
        assert client.retained_candidates_v2(
            category_id=None, query=None, lane=lane, profile_categories=(), profile_sources=(),
            trend_window_hours=48, trend_min_sources=2, max_age_hours=None, min_age_hours=None,
            limit=250, excluded_story_ids=("story:seen",),
            suppressed_sources=("blocked-wire",), suppressed_topics=("blocked-topic",),
            owner_id="11111111-1111-1111-1111-111111111111", hide_already_opened=True) == []
    assert limits == [200, 100]
    assert filters == [(["story:seen"], ["blocked-wire"], ["blocked-topic"])] * 2


def test_owner_state_read_retries_one_configured_timeout_then_returns_live_state():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self):
            return b'[{"story_id":"story:one","saved_at":"2026-09-22T00:00:00Z"}]'

    class TimeoutThenSuccess:
        def __init__(self): self.calls = 0
        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("transient owner-state timeout")
            return Response()

    client = SupabaseHTTP(origin="https://example.test", publishable_key="public",
                          service_role_key="sb_secret_canary", timeout_retries=1)
    opener = TimeoutThenSuccess()
    client._opener = opener

    assert client.owner_states("owner-token", ["story:one"])["story:one"]["saved_at"]
    assert opener.calls == 2


def test_owner_state_read_does_not_retry_an_http_failure():
    class Reject:
        def __init__(self): self.calls = 0
        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO())

    client = SupabaseHTTP(origin="https://example.test", publishable_key="public",
                          service_role_key="sb_secret_canary", timeout_retries=2)
    opener = Reject()
    client._opener = opener

    with pytest.raises(SupabaseHTTPError):
        client.owner_states("owner-token", ["story:one"])
    assert opener.calls == 1


def test_opened_lookup_is_one_authenticated_ids_only_call_above_old_limit():
    client = _client()
    ids = [f"story:{index:064x}" for index in range(233)]
    calls = []
    def capture(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return [ids[0]]
    client._request = capture
    assert client.opened_candidate_ids("reader-token", ids + [ids[0]]) == {ids[0]}
    assert calls == [("POST", "/rest/v1/rpc/m2_opened_candidate_ids", {
        "token": "reader-token", "key": client._publishable,
        "body": {"p_story_ids": ids + [ids[0]]}})]


@pytest.mark.parametrize("response", [None, {}, [None], [{"story_id": "private"}], ["unknown"]])
def test_opened_lookup_rejects_malformed_or_unrequested_response(response):
    client = _client()
    client._request = lambda *_args, **_kwargs: response
    with pytest.raises(SupabaseHTTPError, match="invalid IDs"):
        client.opened_candidate_ids("reader-token", [f"story:{1:064x}"])


def test_opened_lookup_bound_and_failure_never_retry():
    client = _client()
    calls = []
    def timeout(*_args, **_kwargs):
        calls.append(1)
        raise SupabaseHTTPError("timeout")
    client._request = timeout
    with pytest.raises(ValueError, match="protocol limit"):
        client.opened_candidate_ids("reader-token", [f"story:{1:064x}"] * 10001)
    assert calls == []
    with pytest.raises(SupabaseHTTPError):
        client.opened_candidate_ids("reader-token", [f"story:{1:064x}"] * 10000)
    assert calls == [1]


class _Raise:
    """An opener that fails the way one specific network condition fails."""

    def __init__(self, error):
        self._error = error
        self.seen_timeout = None

    def open(self, request, timeout):
        self.seen_timeout = timeout
        raise self._error


@pytest.mark.parametrize(("error", "reason", "status_code"), [
    (urllib.error.HTTPError("https://example.test/x", 500, "boom", {}, io.BytesIO()), "http", 500),
    (TimeoutError("timed out"), "timeout", None),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", None),
    (urllib.error.URLError(OSError("no route to host")), "url", None),
])
def test_failed_request_prints_one_structured_line_and_carries_path(capsys, error, reason, status_code):
    """"Supabase request failed" alone cannot tell a 500 from a client timeout.

    Production returned exactly that string for every POST /rank on 2026-09-21
    and the Modal log held only the access line, so the failing RPC, its status
    and how long it ran were all unrecoverable.
    """
    client = _client()
    client._opener = _Raise(error)
    with pytest.raises(SupabaseHTTPError) as raised:
        client.open_run_view(user_id="owner", run_id="run", eligibility_key="key")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    record = json.loads(lines[0])
    assert record["event"] == "m2_supabase_request_failed"
    assert record["path"] == "/rest/v1/rpc/m2_open_run_view"
    assert record["method"] == "POST"
    assert record["status_code"] == status_code
    assert record["reason"] == reason
    assert isinstance(record["elapsed_ms"], int) and record["elapsed_ms"] >= 0
    assert set(record) == {"event", "path", "method", "status_code", "elapsed_ms", "reason"}
    assert raised.value.path == "/rest/v1/rpc/m2_open_run_view"
    assert raised.value.status_code == status_code


def test_failure_line_never_carries_the_query_string_or_any_credential(capsys):
    """The frozen-order read puts a user id in the query string, and every call
    carries the service-role key in a header. Neither may reach a log line."""
    client = _client()
    client._opener = _Raise(urllib.error.URLError(OSError("down")))
    with pytest.raises(SupabaseHTTPError):
        client.load_frozen_order(user_id="11111111-1111-1111-1111-111111111111",
                                 frozen_order_id="order-canary")

    printed = capsys.readouterr().out
    record = json.loads(printed.strip())
    assert record["path"] == "/rest/v1/m2_frozen_rankings"
    assert "?" not in record["path"]
    for secret in ("11111111-1111-1111-1111-111111111111", "order-canary",
                   "sb_secret_canary", "public", "Authorization", "apikey"):
        assert secret not in printed


def test_configured_timeout_reaches_the_socket():
    client = _client(12.5)
    opener = _Raise(urllib.error.URLError(OSError("down")))
    client._opener = opener
    with pytest.raises(SupabaseHTTPError):
        client.open_run_view(user_id="owner", run_id="run", eligibility_key="key")
    assert opener.seen_timeout == 12.5


def test_default_timeout_is_unchanged_when_the_policy_says_nothing():
    assert DEFAULT_TIMEOUT_SECONDS == 3.0
    client = _client()
    opener = _Raise(urllib.error.URLError(OSError("down")))
    client._opener = opener
    with pytest.raises(SupabaseHTTPError):
        client.open_run_view(user_id="owner", run_id="run", eligibility_key="key")
    assert opener.seen_timeout == 3.0


@pytest.mark.parametrize("value", [0, 0.9, 30.1, 120, -5, "10", None, True])
def test_out_of_range_timeout_is_refused(value):
    with pytest.raises(ValueError, match="timeout_seconds"):
        validate_timeout_seconds(value)


@pytest.mark.parametrize("value", [1, 1.0, 3.0, 10, 30])
def test_in_range_timeout_is_accepted(value):
    assert validate_timeout_seconds(value) == float(value)


def test_prepared_scrub_uses_service_role_and_exact_100_limit(capsys):
    client = SupabaseHTTP(origin="https://example.test", publishable_key="public",
        service_role_key="sb_secret_canary")
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b"2"
    class Capture:
        def open(self, request, timeout):
            seen.update(method=request.method, url=request.full_url,
                        body=json.loads(request.data), headers=dict(request.header_items()))
            return Response()
    client._opener = Capture()
    assert client.scrub_expired_prepared_orders() == 2
    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.test/rest/v1/rpc/m2_scrub_expired_prepared_orders"
    assert seen["body"] == {"p_limit": 100}
    assert seen["headers"]["Apikey"] == "sb_secret_canary"
    assert "Authorization" not in seen["headers"]
    assert "sb_secret_canary" not in capsys.readouterr().err


@pytest.mark.parametrize("result", [b"true", b"-1", b"101", b'"2"'])
def test_prepared_scrub_rejects_invalid_count(result):
    client = _client()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return result
    class Capture:
        def open(self, request, timeout): return Response()
    client._opener = Capture()
    with pytest.raises(SupabaseHTTPError, match="invalid count"):
        client.scrub_expired_prepared_orders()


def test_continuation_claim_converts_utc_expiry_and_pins_rpc_body():
    client = _client()
    seen = {}
    result = {"granted": True, "frozen_order": {
        "bindings": {}, "cards": [], "page_size": 25,
        "expires_at": "2026-09-24T09:30:00+00:00"}}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return json.dumps(result).encode()

    class Capture:
        def open(self, request, timeout):
            seen.update(method=request.method, url=request.full_url,
                        body=json.loads(request.data), headers=dict(request.header_items()))
            return Response()

    client._opener = Capture()
    claimed = client.claim_continuation_snapshot(
        user_id="owner", run_id="run", eligibility_key="a" * 64,
        frozen_order_id="frozen", token="claim", ttl_seconds=60)
    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.test/rest/v1/rpc/m2_claim_continuation_snapshot"
    assert seen["body"] == {"p_user_id": "owner", "p_run_id": "run",
                            "p_eligibility_key": "a" * 64, "p_frozen_order_id": "frozen",
                            "p_token": "claim", "p_ttl_seconds": 60}
    assert seen["headers"]["Apikey"] == "sb_secret_canary"
    assert "Authorization" not in seen["headers"]
    assert claimed["frozen_order"]["expires_at"] == 1790242200


def test_consume_prepared_order_uses_exact_service_rpc_and_body():
    client = _client()
    seen = {}
    result = {"status": "ready", "ranked_candidate_ids": []}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return json.dumps(result).encode()

    class Capture:
        def open(self, request, timeout):
            seen.update(method=request.method, url=request.full_url,
                        body=json.loads(request.data), headers=dict(request.header_items()))
            return Response()

    client._opener = Capture()
    consumed = client.consume_prepared_order(
        user_id="owner", target_run_id="target-run",
        eligibility_key="b" * 64, policy_digest="c" * 64,
        candidate_ids=["story:" + "d" * 64], minimum_overlap=5)
    assert consumed == result
    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.test/rest/v1/rpc/m2_consume_prepared_order"
    assert seen["body"] == {"p_user_id": "owner", "p_target_run_id": "target-run",
                            "p_eligibility_key": "b" * 64, "p_policy_digest": "c" * 64,
                            "p_candidate_ids": ["story:" + "d" * 64],
                            "p_minimum_overlap": 5}
    assert seen["headers"]["Apikey"] == "sb_secret_canary"
    assert "Authorization" not in seen["headers"]


def test_prepared_history_compatibility_uses_exact_service_rpc_and_body():
    client = _client()
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b"true"

    class Capture:
        def open(self, request, timeout):
            seen.update(method=request.method, url=request.full_url,
                        body=json.loads(request.data), headers=dict(request.header_items()))
            return Response()

    client._opener = Capture()
    assert client.prepared_history_is_compatible(user_id="owner-1",
        history_generation=3, behavior_revision=17) is True
    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.test/rest/v1/rpc/m2_prepared_history_is_compatible"
    assert seen["body"] == {"p_user_id": "owner-1", "p_history_generation": 3,
                            "p_behavior_revision": 17}
    assert seen["headers"]["Apikey"] == "sb_secret_canary"
    assert "Authorization" not in seen["headers"]


@pytest.mark.parametrize("result", [b"null", b"1", b'"true"', b"{}"])
def test_prepared_history_compatibility_rejects_non_boolean(result):
    client = _client()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return result

    class Capture:
        def open(self, request, timeout): return Response()

    client._opener = Capture()
    with pytest.raises(SupabaseHTTPError, match="non-boolean"):
        client.prepared_history_is_compatible(user_id="owner-1",
            history_generation=3, behavior_revision=17)
