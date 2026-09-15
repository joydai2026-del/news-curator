from datetime import datetime, timezone

import pytest

from scripts.collect_m2_operational_receipt import _category_ids, _get_rows, _health, _origin, _receipt

def test_health_distinguishes_idle_volume_pass_and_fail_without_owner_claims():
    policy={"minimum_requests":5,"maximum_failure_rate":.5}
    assert _health([],policy)["status"]=='idle'
    row={"endpoint":"rank","outcome":"model","latency_band":"1to3s","request_count":4,"latest_input_match_count":4}
    assert _health([row],policy)["status"]=='insufficient_volume'
    assert _health([{**row,"request_count":5,"latest_input_match_count":5}],policy)["status"]=='pass'
    assert _health([{**row,"outcome":"timeout","request_count":5,"latest_input_match_count":0}],policy)["status"]=='fail'
    assert 'not per owner' in _health([row],policy)['population_scope']


def test_receipt_is_sanitized_and_refuses_to_claim_model_denominator():
    rows = [{"created_at": "2026-09-15T00:00:00Z", "bindings": {
        "request_id": "must-not-escape", "result_mode": "model", "fallback_reason": "",
        "execution": {"attempts_started": 1, "provider_elapsed_seconds": 2.1}}}]
    result = _receipt(rows, runtime_revision="a" * 40, policy_hash="b" * 64,
        checklist_hash="c" * 64, category_ids=["world"], environment="production",
        observed_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert result["operational_model_path"]["status"] == "insufficient_evidence"
    assert result["operational_model_path"]["requests_with_provider_attempt"] == 1
    assert result["row_source_revision_status"] == "unbound"
    assert "git_commit_sha" not in result
    serialized = __import__("json").dumps(result)
    assert "must-not-escape" not in serialized and "provider_elapsed_seconds" not in serialized


def test_receipt_rejects_missing_execution_binding():
    with pytest.raises(ValueError, match="incomplete"):
        _receipt([{"bindings": {"result_mode": "fallback", "fallback_reason": "budget"}}],
            runtime_revision="a" * 40, policy_hash="b" * 64, checklist_hash="c" * 64,
            category_ids=["world"], environment="production", observed_at=datetime.now(timezone.utc))


def test_receipt_buckets_untrusted_result_labels_without_emitting_them():
    private = "private-owner-text"
    result = _receipt([{"bindings": {"result_mode": private, "fallback_reason": private,
        "execution": {"attempts_started": 0}}}], runtime_revision="a" * 40, policy_hash="b" * 64,
        checklist_hash="c" * 64, category_ids=["world"], environment="production",
        observed_at=datetime.now(timezone.utc))
    serialized = __import__("json").dumps(result)
    assert private not in serialized
    assert result["operational_model_path"]["result_modes"] == {"unknown": 1}
    assert result["operational_model_path"]["fallback_reasons"] == {"unknown": 1}


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


def test_operational_policy_rejects_unbounded_or_wrong_type_values():
    from scripts.collect_m2_operational_receipt import _validate_operational_policy
    policy={"schema_version":1,"window_minutes":60,"minimum_requests":5,"maximum_failure_rate":.5,"retention_days":14}
    assert _validate_operational_policy(policy) == policy
    for key,bad in (("window_minutes",0),("minimum_requests",True),("maximum_failure_rate",float("nan")),("retention_days",91)):
        with pytest.raises(ValueError): _validate_operational_policy({**policy,key:bad})

def test_delivery_health_does_not_hide_fallback_or_latency_counts():
    row={"endpoint":"rank","outcome":"fallback","latency_band":"8to20s","request_count":5,"latest_input_match_count":0}
    report=_health([row],{"minimum_requests":5,"maximum_failure_rate":.5})
    assert report["status_scope"] == "request_delivery_only_not_recommendation_quality"
    assert report["outcome_counts"]["fallback"] == report["latency_counts"]["8to20s"] == 5
