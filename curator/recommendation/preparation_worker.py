"""One-shot, owner-scoped model preparation for a later reading run.

The queue is durable. A claimed job is never automatically retried because a
crash after a provider attempt cannot prove whether the provider charged.
"""

from __future__ import annotations

from curator.contracts.enums import RankingResultMode
from curator.contracts.ranking_request import validate_ranking_response

from .prepared_order import request_from_payload
from .rankllm_adapter import BudgetState


def process_one_preparation(*, store, adapter, policy) -> str:
    if not policy.next_run_preparation_enabled:
        return "disabled"
    job = store.claim_prepared_order(policy_digest=policy.effective_policy_digest)
    if job is None:
        return "empty"
    job_id, claim_token = str(job["job_id"]), str(job["claim_token"])
    request_id, user_id = str(job["request_id"]), str(job["user_id"])
    attempted = False
    reserved = False
    attempt_mark_uncertain = False
    settlement_attempted = False
    try:
        request = request_from_payload(job["request_payload"])
        if (request.request_id != request_id or request.owner.user_id != user_id
                or user_id not in policy.preview_owner_ids
                or request.owner.tenant_id != policy.tenant_id
                or request.policy_version != policy.policy_version
                or request.model_version != policy.model_version):
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "invalid"
        prepared, _reason = adapter.prepare_with_reason(request)
        if prepared is None:
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "unpreparable"
        estimate = adapter.reservation_estimate(
            estimated_input_tokens=prepared.input_tokens_bound,
            estimated_output_tokens=prepared.output_tokens_budget)
        if estimate is None or not store.reserve_prepared_budget(
                job_id=job_id, claim_token=claim_token, amount_usd=estimate,
                daily_limit_usd=policy.daily_cost_limit_usd):
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "budget_denied"
        reserved = True
        observed = {}
        attempt_mark_denied = False

        def on_attempt(_attempt, _elapsed):
            nonlocal attempted, attempt_mark_denied, attempt_mark_uncertain
            # The adapter invokes this immediately before each provider
            # transport, including retries. The RPC authorizes both the first
            # claimed-to-attempting transition and an already-attempting retry
            # under the owner's privacy lock. It cannot hold that lock
            # atomically across an external HTTP request.
            try:
                allowed = store.mark_prepared_attempt(
                    job_id=job_id, claim_token=claim_token)
            except Exception:
                attempt_mark_uncertain = True
                raise
            if not allowed:
                attempt_mark_denied = True
                raise RuntimeError("prepared attempt authorization denied")
            attempted = True

        def on_usage(outcome, unknown_attempts, _elapsed):
            observed.update(input_tokens=outcome.input_tokens,
                            output_tokens=outcome.output_tokens,
                            unknown_attempts=unknown_attempts)

        def settle_observed_usage():
            settled = adapter.settle_observed_cost(input_tokens=observed["input_tokens"],
                output_tokens=observed["output_tokens"],
                unknown_attempts=observed["unknown_attempts"], reserved_usd=estimate)
            store.settle_budget(user_id=user_id, request_id=request_id,
                                actual_usd=settled, status="settled")

        try:
            receipt = adapter.rank(request, provider_processing_consent=True,
                budget=BudgetState(0), estimated_input_tokens=prepared.input_tokens_bound,
                estimated_output_tokens=prepared.output_tokens_budget, prepared=prepared,
                usage_observer=on_usage, attempt_observer=on_attempt)
        except Exception:
            if not attempt_mark_denied:
                raise
            # A callback refusal can be returned as a fallback or propagated
            # by a future adapter. The same cost rule applies either way.
            receipt = None
        if attempt_mark_denied:
            if observed:
                # A refused retry cannot start another transport. Settle any
                # usage already reported by an earlier completed attempt.
                settle_observed_usage()
            elif not attempted:
                # First attempt was refused. No provider transport began.
                settlement_attempted = True
                store.settle_budget(user_id=user_id, request_id=request_id,
                                    actual_usd=0.0, status="released")
            # If an earlier attempt has no observed usage, its charge is
            # uncertain and the ceiling stays reserved for reconciliation.
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "failed" if attempted else "stale"
        if attempt_mark_uncertain:
            # A failed retry authorization cannot start another transport.
            # Account for a completed earlier attempt when its usage is known;
            # otherwise retain the ceiling for reconciliation. Never retry or
            # publish after the ambiguous authorization boundary.
            if attempted and observed:
                settle_observed_usage()
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "failed"
        if observed:
            settle_observed_usage()
        elif not attempted:
            settlement_attempted = True
            store.settle_budget(user_id=user_id, request_id=request_id,
                                actual_usd=0.0, status="released")
        # An attempted call without observed usage may have charged. Its
        # reservation remains held. A fallback answer cannot prepare an order.
        if receipt.result_mode != RankingResultMode.MODEL or not observed:
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
            return "failed"
        validate_ranking_response(receipt, request)
        if store.finish_prepared_order(job_id=job_id, claim_token=claim_token,
                                       ranked_candidate_ids=receipt.ranked_candidate_ids):
            return "ready"
        store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
        return "stale"
    except Exception:
        # Release a known reservation only when no transport started and no
        # authorization or settlement outcome is uncertain. Otherwise an
        # exception can conceal a provider charge.
        if reserved and not attempted and not attempt_mark_uncertain and not settlement_attempted:
            try:
                store.settle_budget(user_id=user_id, request_id=request_id,
                                    actual_usd=0.0, status="released")
            except Exception:
                pass
        try:
            store.fail_prepared_order(job_id=job_id, claim_token=claim_token)
        except Exception:
            pass
        return "failed"
