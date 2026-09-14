from datetime import datetime, timezone

import pytest

from curator.contracts.enums import ActorKind, RankingResultMode
from curator.contracts.ranking_request import AuthenticatedOwner, RankingCandidate, RankingRequest
from curator.recommendation.rankllm_adapter import BudgetState, ProviderOutcome, RankLLMAdapter, RankerPolicy


SID1 = "story:" + "1" * 64
SID2 = "story:" + "2" * 64


def request():
    candidates = tuple(
        RankingCandidate(sid, sid, f"doc-{i}", f"Title {i}", "Summary", "source", "en", datetime.now(timezone.utc))
        for i, sid in enumerate((SID1, SID2), 1)
    )
    return RankingRequest(
        schema_version=1,
        request_id="req-1",
        owner=AuthenticatedOwner("tenant", "user", "principal", ActorKind.HUMAN),
        candidates=candidates,
        selected_candidate_registry_ids=(SID1, SID2),
        ordered_history=(),
        history_revision=0,
        history_generation=1,
        consent_revision=1,
        server_commit_revision=1,
        policy_version="policy-r1",
        model_version="gpt-5-mini",
    )


def policy(**changes):
    values = dict(provider_id="openai", model_id="gpt-5-mini", endpoint="https://api.openai.com", prompt_revision="r1", input_cost_per_million_tokens_usd=1.0, output_cost_per_million_tokens_usd=1.0)
    values.update(changes)
    return RankerPolicy(**values)


class Engine:
    def __init__(self, ids=(SID2, SID1), error=None):
        self.ids, self.error, self.calls = ids, error, 0

    def rerank(self, model_input, *, timeout_seconds):
        self.calls += 1
        if self.error:
            raise self.error
        return ProviderOutcome(self.ids, 100, 10, "provider-request")


def test_model_result_is_exact_rankllm_permutation():
    result = RankLLMAdapter(policy=policy(), engine=Engine()).rank(request(), provider_processing_consent=True, budget=BudgetState(0), estimated_input_tokens=100, estimated_output_tokens=10)
    assert result.result_mode is RankingResultMode.MODEL
    assert result.ranked_candidate_ids == (SID2, SID1)


def test_invalid_vendor_output_falls_back_without_repairing_as_model_success():
    result = RankLLMAdapter(policy=policy(), engine=Engine((SID1, SID1))).rank(request(), provider_processing_consent=True, budget=BudgetState(0), estimated_input_tokens=100, estimated_output_tokens=10)
    assert result.result_mode is RankingResultMode.FALLBACK
    assert result.fallback_reason == "invalid_provider_permutation"
    assert result.ranked_candidate_ids == (SID1, SID2)


def test_unknown_pricing_blocks_before_engine_call():
    engine = Engine()
    result = RankLLMAdapter(policy=policy(input_cost_per_million_tokens_usd=None), engine=engine).rank(request(), provider_processing_consent=True, budget=BudgetState(0), estimated_input_tokens=100, estimated_output_tokens=10)
    assert result.fallback_reason == "unknown_provider_pricing"
    assert engine.calls == 0


def test_missing_consent_blocks_before_engine_call():
    engine = Engine()
    result = RankLLMAdapter(policy=policy(), engine=engine).rank(request(), provider_processing_consent=False, budget=BudgetState(0), estimated_input_tokens=100, estimated_output_tokens=10)
    assert result.fallback_reason == "provider_processing_consent_required"
    assert engine.calls == 0


def test_attempt_observer_marks_boundary_immediately_before_engine_invocation():
    events = []
    class ObservedEngine(Engine):
        def rerank(self, model_input, *, timeout_seconds):
            events.append("engine")
            return super().rerank(model_input, timeout_seconds=timeout_seconds)
    engine = ObservedEngine()
    RankLLMAdapter(policy=policy(), engine=engine).rank(
        request(), provider_processing_consent=True, budget=BudgetState(0),
        estimated_input_tokens=100, estimated_output_tokens=10,
        attempt_observer=lambda attempt, elapsed: events.append(("attempt", attempt)),
    )
    assert events == [("attempt", 0), "engine"]


def test_retry_reservation_must_fit_request_cap_before_engine_call():
    engine = Engine()
    result = RankLLMAdapter(
        policy=policy(request_cost_limit_usd=0.00015), engine=engine
    ).rank(
        request(), provider_processing_consent=True, budget=BudgetState(0),
        estimated_input_tokens=100, estimated_output_tokens=10,
    )
    assert result.fallback_reason == "request_cost_limit"
    assert engine.calls == 0


@pytest.mark.parametrize("field,value", [
    ("deadline_seconds", float("nan")),
    ("deadline_seconds", "6"),
    ("request_cost_limit_usd", float("inf")),
    ("daily_cost_limit_usd", True),
    ("provider_id", " "),
    ("prompt_revision", " bad "),
])
def test_policy_rejects_nonfinite_numbers_and_blank_or_padded_ids(field, value):
    with pytest.raises(ValueError):
        policy(**{field: value}).validate()
