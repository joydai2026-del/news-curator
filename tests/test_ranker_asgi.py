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
