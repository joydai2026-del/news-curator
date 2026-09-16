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


def request(app, *, method="GET", path="/config", origin="https://reader.example", body=b"", headers=()):
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
        await app({"type": "http", "method": method, "path": path, "query_string": b"", "headers": all_headers}, receive, send)
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

def test_rank_health_records_model_and_exact_latest_binding():
    class Reporter:
        def record(self, **fields): self.fields=fields
    class ModelService(Service):
        def rank(self, **_):
            return {"result_mode":"model","history_revision":7,"server_commit_revision":7,
                "schema_version":1,"request_id":"request","cards":[],"next_cursor":None}
    reporter=Reporter()
    sent=request(RankingASGI(service=ModelService(),reader_origin="https://reader.example",health_reporter=reporter),
        method="POST",path="/rank",body=b'{}',headers=((b"authorization",b"Bearer valid"),))
    assert sent[0]["status"]==200
    assert reporter.fields["endpoint"]=="rank" and reporter.fields["outcome"]=="model"
    assert reporter.fields["latest_input_match"] is True

def test_rank_health_failure_does_not_change_selected_response(capsys):
    class Reporter:
        def record(self, **_): raise RuntimeError("telemetry secret")
    # The production reporter contains its own failure boundary. Exercise that exact boundary.
    from curator.recommendation.request_health import RequestHealthReporter
    class Store:
        def record_request_health(self,**_): raise RuntimeError("telemetry secret")
    sent=request(RankingASGI(service=Service(),reader_origin="https://reader.example",
        health_reporter=RequestHealthReporter(Store())),method="POST",path="/rank",body=b'{}')
    assert sent[0]["status"]==401
    logged=capsys.readouterr().err
    assert "telemetry secret" not in logged and 'request_health_write_failed' in logged

def test_unexpected_rank_failure_is_safely_counted():
    class Broken(Service):
        def rank(self, **_): raise LookupError('private')
    class Reporter:
        def record(self, **fields): self.fields=fields
    reporter=Reporter(); sent=request(RankingASGI(service=Broken(),reader_origin="https://reader.example",health_reporter=reporter),
        method="POST",path="/rank",body=b'{}')
    assert sent[0]["status"]==500 and json.loads(sent[1]["body"])=={"error":"server_error"}
    assert reporter.fields["outcome"]=="server_error"
