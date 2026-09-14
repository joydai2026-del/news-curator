import json
from pathlib import Path

from curator.recommendation.supabase_http import SupabaseHTTP


def test_transport_traverses_more_than_two_rpc_pages_from_captured_public_rows():
    captured_path = Path(__file__).parent / "fixtures/m2-retained-public.json"
    capture = json.loads(captured_path.read_text())
    unique = {row["story_id"]: {"story_id": row["story_id"], "published_at": row["published_at"]}
              for row in capture["rows"]}
    ordered = sorted(unique.values(), key=lambda row: (row["published_at"], row["story_id"]), reverse=True)
    assert len(ordered) >= 201
    transport = object.__new__(SupabaseHTTP)
    transport._service = "local-test"
    calls = []
    def request(method, path, **kwargs):
        body = kwargs["body"]; calls.append(body)
        before = body["p_before_story_id"]
        start = 0 if before is None else next(i + 1 for i, row in enumerate(ordered) if row["story_id"] == before)
        return ordered[start:start + body["p_limit"]]
    transport._request = request
    result = transport.retained_candidates(category_id=None, query=None, limit=201)
    assert len(result) == 201
    assert [call["p_limit"] for call in calls] == [100, 100, 1]
    assert calls[1]["p_before_story_id"] == result[99]["story_id"]
