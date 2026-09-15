from datetime import datetime, timezone

import pytest

from scripts.collect_m2_operational_receipt import _origin, _receipt


def test_receipt_is_sanitized_and_refuses_to_claim_model_denominator():
    rows = [{"created_at": "2026-09-15T00:00:00Z", "bindings": {
        "request_id": "must-not-escape", "result_mode": "model", "fallback_reason": "",
        "execution": {"attempts_started": 1, "provider_elapsed_seconds": 2.1}}}]
    result = _receipt(rows, commit="a" * 40, policy_hash="b" * 64,
        checklist_hash="c" * 64, observed_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert result["operational_model_path"]["status"] == "insufficient_evidence"
    assert result["operational_model_path"]["requests_with_provider_attempt"] == 1
    serialized = __import__("json").dumps(result)
    assert "must-not-escape" not in serialized and "provider_elapsed_seconds" not in serialized


def test_receipt_rejects_missing_execution_binding():
    with pytest.raises(ValueError, match="incomplete"):
        _receipt([{"bindings": {"result_mode": "fallback", "fallback_reason": "budget"}}],
            commit="a" * 40, policy_hash="b" * 64, checklist_hash="c" * 64,
            observed_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("value", ["http://example.test", "https://user@example.test", "https://example.test/path"])
def test_origin_is_fixed_https(value):
    with pytest.raises(ValueError):
        _origin(value)
