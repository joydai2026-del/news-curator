from datetime import datetime, timezone

import pytest

from scripts.collect_m2_operational_receipt import _category_ids, _get_rows, _origin, _receipt


def test_receipt_is_sanitized_and_refuses_to_claim_model_denominator():
    rows = [{"created_at": "2026-09-15T00:00:00Z", "bindings": {
        "request_id": "must-not-escape", "result_mode": "model", "fallback_reason": "",
        "execution": {"attempts_started": 1, "provider_elapsed_seconds": 2.1}}}]
    result = _receipt(rows, commit="a" * 40, policy_hash="b" * 64,
        checklist_hash="c" * 64, category_ids=["world"],
        observed_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert result["operational_model_path"]["status"] == "insufficient_evidence"
    assert result["operational_model_path"]["requests_with_provider_attempt"] == 1
    serialized = __import__("json").dumps(result)
    assert "must-not-escape" not in serialized and "provider_elapsed_seconds" not in serialized


def test_receipt_rejects_missing_execution_binding():
    with pytest.raises(ValueError, match="incomplete"):
        _receipt([{"bindings": {"result_mode": "fallback", "fallback_reason": "budget"}}],
            commit="a" * 40, policy_hash="b" * 64, checklist_hash="c" * 64,
            category_ids=["world"], observed_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("value", ["http://example.test", "https://user@example.test", "https://example.test/path"])
def test_origin_is_fixed_https(value):
    with pytest.raises(ValueError):
        _origin(value)


def test_category_registry_comes_from_topics(tmp_path):
    path = tmp_path / "topics.yaml"
    path.write_text("categories:\n  - id: world\n  - id: ai\n")
    assert _category_ids(path) == ["world", "ai"]


def test_get_rows_uses_fixed_projection_and_paginates(monkeypatch):
    opened = []
    pages = [[{"bindings": {}}] * 1000, [{"bindings": {}}]]
    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self, _): return __import__("json").dumps(self.value).encode()
    class Opener:
        def open(self, request, timeout):
            opened.append((request, timeout))
            return Response(pages.pop(0))
    monkeypatch.setattr("urllib.request.build_opener", lambda handler: Opener())
    rows = _get_rows("https://db.example", "sb_secret_test", datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert len(rows) == 1001 and len(opened) == 2
    first = __import__("urllib.parse").parse.urlsplit(opened[0][0].full_url)
    query = __import__("urllib.parse").parse.parse_qs(first.query)
    assert first.path == "/rest/v1/m2_frozen_rankings"
    assert query["select"] == ["created_at,bindings"] and query["offset"] == ["0"]
    assert "user_id" not in opened[0][0].full_url
