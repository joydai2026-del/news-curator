import asyncio
import json

from curator.recommendation.asgi import RankingASGI


class Policy:
    enabled = False
    policy_version = "policy-r1"
    model_version = "model-r1"


class Service:
    _policy = Policy()

    def rank(self, *, authorization, body):
        if authorization != "Bearer valid":
            from curator.recommendation.service import AuthenticationError
            raise AuthenticationError
        return {"schema_version": 1, "request_id": "request", "cards": [], "next_cursor": None}


def request(app, *, method="GET", path="/config", origin="https://reader.example", body=b"", headers=(), query=b""):
    async def run():
        sent = []
        received = False
        async def receive():
            nonlocal received
            if received: return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(event):
            sent.append(event)
        all_headers = [(b"origin", origin.encode()), *headers]
        await app({"type": "http", "method": method, "path": path, "query_string": query, "headers": all_headers}, receive, send)
        return sent
    return asyncio.run(run())


def test_config_exposes_disabled_state_without_secrets():
    sent = request(RankingASGI(service=Service(), reader_origin="https://reader.example"))
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["enabled"] is False


def test_unconfigured_origin_is_rejected():
    sent = request(RankingASGI(service=Service(), reader_origin="https://reader.example"), origin="https://evil.example")
    assert sent[0]["status"] == 403


def test_body_limit_is_enforced_before_service_dispatch():
    app = RankingASGI(service=Service(), reader_origin="https://reader.example", maximum_body_bytes=2)
    sent = request(app, method="POST", path="/rank", body=b"{}x")
    assert sent[0]["status"] == 400


def test_rank_route_forwards_bearer_and_json_through_full_asgi_dispatch():
    sent = request(RankingASGI(service=Service(), reader_origin="https://reader.example"),
        method="POST", path="/rank", body=b'{"page_size":10}',
        headers=((b"authorization", b"Bearer valid"), (b"content-type", b"application/json")))
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["request_id"] == "request"


def test_successful_rank_logs_only_route_and_duration(capsys):
    request(RankingASGI(service=Service(), reader_origin="https://reader.example"),
        method="POST", path="/rank", body=b'{"query":"private search text"}',
        headers=((b"authorization", b"Bearer valid"),))
    event = json.loads(capsys.readouterr().err)
    assert set(event) == {"event", "route", "duration_ms"}
    assert event["event"] == "m2_api_timing"
    assert event["route"] == "rank"
    assert event["duration_ms"] >= 0


def test_successful_page_logs_only_route_and_duration(capsys):
    class PagingService(Service):
        def page(self, *, authorization, cursor):
            assert authorization == "Bearer valid" and cursor == "private-cursor"
            return {"schema_version": 1, "cards": [], "end_of_run": True}

    sent = request(RankingASGI(service=PagingService(), reader_origin="https://reader.example"),
        method="GET", path="/page", query=b"cursor=private-cursor",
        headers=((b"authorization", b"Bearer valid"),))
    assert sent[0]["status"] == 200
    event = json.loads(capsys.readouterr().err)
    assert set(event) == {"event", "route", "duration_ms"}
    assert event["event"] == "m2_api_timing"
    assert event["route"] == "page"
    assert event["duration_ms"] >= 0


def test_rank_route_maps_bad_jwt_to_401():
    sent = request(RankingASGI(service=Service(), reader_origin="https://reader.example"),
        method="POST", path="/rank", body=b'{}')
    assert sent[0]["status"] == 401


def test_value_error_logs_only_safe_location_metadata(capsys):
    class InvalidService(Service):
        def rank(self, *, authorization, body):
            raise ValueError("NEWS_CURATOR_MODEL_API_KEY=must-never-appear")

    sent = request(RankingASGI(service=InvalidService(), reader_origin="https://reader.example"),
        method="POST", path="/rank", body=b'{}')
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"]) == {"error": "invalid_request"}
    logged = capsys.readouterr().err
    assert "NEWS_CURATOR_MODEL_API_KEY" not in logged and "must-never-appear" not in logged
    event = json.loads(logged)
    assert event["event"] == "ranker_invalid_request"
    assert event["exception_class"] == "ValueError"
    assert event["source_basename"] == "test_ranker_asgi.py"
    assert type(event["source_line"]) is int and event["source_line"] > 0


def test_supabase_failure_503_names_the_call_and_nothing_else():
    """503 {"error":"Supabase request failed"} on its own is unactionable.

    It cannot distinguish a permission error on one RPC from a client-side
    timeout on the heavy candidate query, which is what production returned all
    of 2026-09-21. The reply now carries the route and the status the database
    gave, and still no body, headers or key.
    """
    from curator.recommendation.supabase_http import SupabaseHTTPError

    class Failing(Service):
        def rank(self, *, authorization, body):
            raise SupabaseHTTPError("Supabase request failed", status_code=None,
                                    path="/rest/v1/rpc/m2_retained_candidates_v2")

    sent = request(RankingASGI(service=Failing(), reader_origin="https://reader.example"),
                   method="POST", path="/rank", body=b"{}",
                   headers=[(b"authorization", b"Bearer valid")])
    assert sent[0]["status"] == 503
    payload = json.loads(sent[1]["body"])
    assert payload == {"error": "Supabase request failed",
                       "path": "/rest/v1/rpc/m2_retained_candidates_v2", "status_code": None}


def test_plain_runtime_error_503_is_unchanged():
    class Failing(Service):
        def rank(self, *, authorization, body):
            raise RuntimeError("ranker unavailable")

    sent = request(RankingASGI(service=Failing(), reader_origin="https://reader.example"),
                   method="POST", path="/rank", body=b"{}",
                   headers=[(b"authorization", b"Bearer valid")])
    assert sent[0]["status"] == 503
    assert json.loads(sent[1]["body"]) == {"error": "ranker unavailable"}
