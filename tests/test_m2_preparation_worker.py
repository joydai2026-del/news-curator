from dataclasses import dataclass

from curator.contracts.enums import RankingResultMode
from curator.contracts.ranking_request import RankingResponseReceipt
from curator.recommendation.prepared_order import request_to_payload
from curator.recommendation.service import ServicePolicy
from tests.test_m2_ranking_contract import request


@dataclass
class Prepared:
    input_tokens_bound: int = 100
    output_tokens_budget: int = 100


class Store:
    def __init__(self, *, reserve=True):
        self.request = request()
        self.job = {"job_id": "job", "claim_token": "claim", "user_id": self.request.owner.user_id,
                    "request_id": self.request.request_id, "request_payload": request_to_payload(self.request)}
        self.reserve = reserve
        self.calls = []

    def claim_prepared_order(self, **kwargs):
        self.calls.append("claim")
        return self.job

    def reserve_prepared_budget(self, **kwargs):
        self.calls.append("reserve")
        return self.reserve

    def mark_prepared_attempt(self, **kwargs):
        self.calls.append("mark")
        return True

    def settle_budget(self, **kwargs):
        self.calls.append(("settle", kwargs["status"]))

    def finish_prepared_order(self, **kwargs):
        self.calls.append("finish")
        return True

    def fail_prepared_order(self, **kwargs):
        self.calls.append("fail")
        return True


class Adapter:
    def __init__(self, *, usage=True):
        self.usage = usage
        self.calls = []

    def prepare_with_reason(self, req):
        self.calls.append("prepare")
        return Prepared(), ""

    def reservation_estimate(self, **kwargs):
        return .01

    def rank(self, req, **kwargs):
        self.calls.append("rank")
        kwargs["attempt_observer"](0, 0)
        if self.usage:
            from curator.recommendation.rankllm_adapter import ProviderOutcome
            kwargs["usage_observer"](ProviderOutcome((), 100, 10, "provider"), 0, 1)
        return RankingResponseReceipt(1, req.request_id, req.policy_version, req.model_version,
            req.history_revision, req.server_commit_revision, req.history_generation,
            req.consent_revision, req.ordered_history[-1].event_id,
            tuple(c.candidate_id for c in req.candidates),
            tuple(c.candidate_id for c in reversed(req.candidates)), RankingResultMode.MODEL)

    def settle_observed_cost(self, **kwargs):
        return .001


def policy():
    return ServicePolicy("rank-policy-2", "configured-model-revision", "provider-a", "tenant-1",
        preview_owner_ids=("user-1",), enabled=True, effective_policy_digest="a" * 64,
        next_run_preparation_enabled=True)


def test_worker_refuses_provider_when_budget_reservation_is_denied():
    from curator.recommendation.preparation_worker import process_one_preparation
    store, adapter = Store(reserve=False), Adapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "budget_denied"
    assert "rank" not in adapter.calls
    assert store.calls == ["claim", "reserve", "fail"]


def test_worker_settles_observed_usage_before_publishing_order():
    from curator.recommendation.preparation_worker import process_one_preparation
    store, adapter = Store(), Adapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "ready"
    assert store.calls == ["claim", "reserve", "mark", ("settle", "settled"), "finish"]


def test_worker_retains_ambiguous_reservation_after_provider_attempt():
    from curator.recommendation.preparation_worker import process_one_preparation
    store, adapter = Store(), Adapter(usage=False)
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "failed"
    assert ("settle", "released") not in store.calls
    assert store.calls[-1] == "fail"


def test_last_mile_authorization_refusal_prevents_provider_and_releases_budget():
    from curator.recommendation.preparation_worker import process_one_preparation

    class DenyingStore(Store):
        def mark_prepared_attempt(self, **kwargs):
            self.calls.append("mark")
            return False

    class CallbackAdapter(Adapter):
        def rank(self, req, **kwargs):
            self.calls.append("rank")
            try:
                kwargs["attempt_observer"](0, 0)
            except RuntimeError:
                return type("Fallback", (), {"result_mode": RankingResultMode.FALLBACK})()
            self.calls.append("provider")
            raise AssertionError("provider must not be called after refusal")

    store, adapter = DenyingStore(), CallbackAdapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "stale"
    assert "provider" not in adapter.calls
    assert store.calls == ["claim", "reserve", "mark", ("settle", "released"), "fail"]


def test_last_mile_refusal_is_safe_if_adapter_propagates_exception():
    from curator.recommendation.preparation_worker import process_one_preparation

    class DenyingStore(Store):
        def mark_prepared_attempt(self, **kwargs):
            self.calls.append("mark")
            return False

    store, adapter = DenyingStore(), Adapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "stale"
    assert store.calls == ["claim", "reserve", "mark", ("settle", "released"), "fail"]


def test_ambiguous_last_mile_authorization_retains_reservation():
    from curator.recommendation.preparation_worker import process_one_preparation

    class UncertainStore(Store):
        def mark_prepared_attempt(self, **kwargs):
            self.calls.append("mark")
            raise TimeoutError("unknown database commit")

    store, adapter = UncertainStore(), Adapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "failed"
    assert ("settle", "released") not in store.calls
    assert store.calls == ["claim", "reserve", "mark", "fail"]


def test_retry_reauthorizes_each_provider_attempt():
    from curator.recommendation.preparation_worker import process_one_preparation
    from curator.recommendation.rankllm_adapter import ProviderOutcome

    class RetryAdapter(Adapter):
        def rank(self, req, **kwargs):
            self.calls.append("rank")
            for attempt in (0, 1):
                kwargs["attempt_observer"](attempt, 0)
                self.calls.append("provider")
            kwargs["usage_observer"](ProviderOutcome((), 100, 10, "provider"), 0, 1)
            return RankingResponseReceipt(1, req.request_id, req.policy_version, req.model_version,
                req.history_revision, req.server_commit_revision, req.history_generation,
                req.consent_revision, req.ordered_history[-1].event_id,
                tuple(c.candidate_id for c in req.candidates),
                tuple(c.candidate_id for c in reversed(req.candidates)), RankingResultMode.MODEL)

    store, adapter = Store(), RetryAdapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "ready"
    assert store.calls.count("mark") == 2
    assert adapter.calls.count("provider") == 2


def test_retry_refusal_blocks_second_provider_and_retains_first_reservation():
    from curator.recommendation.preparation_worker import process_one_preparation

    class WithdrawnStore(Store):
        def mark_prepared_attempt(self, **kwargs):
            self.calls.append("mark")
            return self.calls.count("mark") == 1

    class RetryAdapter(Adapter):
        def rank(self, req, **kwargs):
            self.calls.append("rank")
            kwargs["attempt_observer"](0, 0)
            self.calls.append("provider")
            try:
                kwargs["attempt_observer"](1, 1)
            except RuntimeError:
                return type("Fallback", (), {"result_mode": RankingResultMode.FALLBACK})()
            self.calls.append("provider")
            raise AssertionError("second provider call must not start after withdrawal")

    store, adapter = WithdrawnStore(), RetryAdapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "failed"
    assert store.calls == ["claim", "reserve", "mark", "mark", "fail"]
    assert adapter.calls.count("provider") == 1
    assert ("settle", "released") not in store.calls


def test_uncertain_retry_authorization_blocks_retry_and_retains_reservation():
    from curator.recommendation.preparation_worker import process_one_preparation

    class UncertainRetryStore(Store):
        def mark_prepared_attempt(self, **kwargs):
            self.calls.append("mark")
            if self.calls.count("mark") == 2:
                raise TimeoutError("unknown retry authorization")
            return True

    class RetryAdapter(Adapter):
        def rank(self, req, **kwargs):
            self.calls.append("rank")
            kwargs["attempt_observer"](0, 0)
            self.calls.append("provider")
            try:
                kwargs["attempt_observer"](1, 1)
            except TimeoutError:
                return type("Fallback", (), {"result_mode": RankingResultMode.FALLBACK})()
            self.calls.append("provider")
            raise AssertionError("second provider call must not start after uncertainty")

    store, adapter = UncertainRetryStore(), RetryAdapter()
    assert process_one_preparation(store=store, adapter=adapter, policy=policy()) == "failed"
    assert store.calls == ["claim", "reserve", "mark", "mark", "fail"]
    assert adapter.calls.count("provider") == 1
    assert ("settle", "released") not in store.calls
