"""News Curator-owned safety boundary around RankLLM listwise ranking.

The vendor package is injected behind ``RankLLMEngine``. This module never
imports it, reads credentials, chooses an endpoint, or opens a socket.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import urlsplit

from curator.contracts.enums import EventType, RankingResultMode
from curator.contracts.ranking_request import (
    ModelRankingInput,
    RankingRequest,
    RankingResponseReceipt,
    validate_ranking_request,
    validate_ranking_response,
)
from .async_provider import (ProviderHTTPError, ProviderResponseError, ProviderResponseInvalid,
    ProviderTimeout, ProviderTransportFailure)
from .diagnostics import log_suppressed_exception


class RankLLMEngine(Protocol):
    """Pinned RankLLM listwise engine, supplied only after dependency approval."""

    def rerank(self, model_input: ModelRankingInput, *, timeout_seconds: float) -> "ProviderOutcome": ...


@dataclass(frozen=True)
class ProviderOutcome:
    ranked_candidate_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    provider_request_id: str


@dataclass(frozen=True)
class RankerPolicy:
    provider_id: str
    model_id: str
    endpoint: str
    prompt_revision: str
    deadline_seconds: float = 6.0
    max_retries: int = 1
    request_cost_limit_usd: float = 0.02
    daily_cost_limit_usd: float = 2.0
    input_cost_per_million_tokens_usd: float | None = None
    output_cost_per_million_tokens_usd: float | None = None

    def validate(self) -> None:
        def finite_number(value: object) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        for field_name in ("provider_id", "model_id", "prompt_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} must be non-blank and unpadded")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("ranker endpoint must be a credential-free HTTPS origin")
        if parsed.path not in ("", "/", "/v1") or parsed.query or parsed.fragment:
            raise ValueError("ranker endpoint must be a fixed API base")
        if not finite_number(self.deadline_seconds) or self.deadline_seconds <= 0 or self.deadline_seconds > 6:
            raise ValueError("ranker deadline must be within six seconds")
        if self.max_retries not in (0, 1):
            raise ValueError("ranker permits at most one retry")
        if any(
            not finite_number(value) or value <= 0
            for value in (self.request_cost_limit_usd, self.daily_cost_limit_usd)
        ):
            raise ValueError("ranker cost limits must be positive")
        for value in (self.input_cost_per_million_tokens_usd, self.output_cost_per_million_tokens_usd):
            if value is not None and (not finite_number(value) or value < 0):
                raise ValueError("ranker token prices must be finite non-negative numbers")


@dataclass(frozen=True)
class BudgetState:
    spent_today_usd: float
    reserved_today_usd: float = 0.0


class RetryableProviderError(RuntimeError):
    """Low-information provider failure eligible for one bounded retry."""


class RankLLMAdapter:
    def __init__(self, *, policy: RankerPolicy, engine: RankLLMEngine, clock=time.monotonic) -> None:
        policy.validate()
        self._policy = policy
        self._engine = engine
        self._clock = clock

    def rank(
        self,
        request: RankingRequest,
        *,
        provider_processing_consent: bool,
        budget: BudgetState,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        prepared=None,
        usage_observer=None,
        attempt_observer=None,
    ) -> RankingResponseReceipt:
        validate_ranking_request(request)
        if request.model_version != self._policy.model_id:
            return self._fallback(request, "model_policy_mismatch")
        if not provider_processing_consent:
            return self._fallback(request, "provider_processing_consent_required")
        estimate = self._estimated_cost(estimated_input_tokens, estimated_output_tokens)
        if estimate is None:
            return self._fallback(request, "unknown_provider_pricing")
        retry_reservation = estimate * (1 + self._policy.max_retries)
        if retry_reservation > self._policy.request_cost_limit_usd:
            return self._fallback(request, "request_cost_limit")
        if budget.spent_today_usd + budget.reserved_today_usd + retry_reservation > self._policy.daily_cost_limit_usd:
            return self._fallback(request, "daily_cost_limit")

        started = self._clock()
        outcome: ProviderOutcome | None = None
        for attempt in range(self._policy.max_retries + 1):
            remaining = self._policy.deadline_seconds - (self._clock() - started)
            if remaining <= 0:
                return self._fallback(request, "provider_deadline")
            try:
                # This is the exact uncertainty boundary: before this callback
                # no provider attempt exists; after it, any unconfirmed call may
                # have been charged and must retain its reservation.
                if attempt_observer is not None:
                    attempt_observer(attempt, self._clock() - started)
                outcome = (self._engine.rerank_prepared(prepared, timeout_seconds=remaining) if prepared is not None
                    else self._engine.rerank(request.model_input(), timeout_seconds=remaining))
                break
            except RetryableProviderError:
                if attempt >= self._policy.max_retries:
                    return self._fallback(request, "provider_retry_exhausted")
            except ProviderTimeout:
                return self._fallback(request, "provider_deadline")
            except ProviderHTTPError as error:
                return self._fallback(request, error.reason)
            except ProviderTransportFailure:
                return self._fallback(request, "provider_transport_failure")
            except ProviderResponseInvalid:
                return self._fallback(request, "provider_response_invalid")
            except ProviderResponseError as error:
                if usage_observer is not None:
                    usage_observer(ProviderOutcome((), error.input_tokens, error.output_tokens,
                        error.request_id), attempt, self._clock() - started)
                return self._fallback(request, "invalid_provider_permutation")
            except Exception as error:
                log_suppressed_exception("m2_provider_call_failed", error,
                    candidates=len(request.candidates), history_events=len(request.ordered_history))
                return self._fallback(request, "provider_failure")
        if outcome is None:
            return self._fallback(request, "provider_failure")
        if usage_observer is not None:
            usage_observer(outcome, attempt, self._clock() - started)
        if self._clock() - started > self._policy.deadline_seconds:
            return self._fallback(request, "provider_deadline")
        observed = self._estimated_cost(outcome.input_tokens, outcome.output_tokens)
        if observed is None or observed > self._policy.request_cost_limit_usd:
            return self._fallback(request, "observed_cost_limit")
        receipt = self._receipt(
            request,
            ranked_ids=outcome.ranked_candidate_ids,
            mode=RankingResultMode.MODEL,
            reason="",
        )
        try:
            validate_ranking_response(receipt, request)
        except ValueError:
            return self._fallback(request, "invalid_provider_permutation")
        return receipt

    def reservation_estimate(self, *, estimated_input_tokens: int, estimated_output_tokens: int) -> float | None:
        estimate = self._estimated_cost(estimated_input_tokens, estimated_output_tokens)
        if estimate is None:
            return None
        reserved = estimate * (1 + self._policy.max_retries)
        return reserved if reserved <= self._policy.request_cost_limit_usd else None

    def settle_observed_cost(self, *, input_tokens: int, output_tokens: int, unknown_attempts: int,
                             reserved_usd: float) -> float:
        observed = self.observed_cost(input_tokens=input_tokens, output_tokens=output_tokens)
        if observed is None or not 0 <= unknown_attempts <= self._policy.max_retries:
            raise ValueError("invalid observed usage settlement")
        amount = observed + unknown_attempts * reserved_usd / (1 + self._policy.max_retries)
        if amount > reserved_usd:
            raise ValueError("observed usage exceeded its reservation")
        return amount

    def prepare(self, request: RankingRequest):
        """Prepare the largest policy-safe history selection that fits the request cap.

        Whole oldest events are omitted. This preserves order, repetitions, and
        explicit feedback actions while retaining relative order and repetitions.
        """
        if self._policy.input_cost_per_million_tokens_usd is None or self._policy.output_cost_per_million_tokens_usd is None:
            return None
        prepare = getattr(self._engine, "prepare", None)
        if prepare is None:
            return None
        model_input = request.model_input()
        full = prepare(model_input)
        if self.reservation_estimate(estimated_input_tokens=full.input_tokens_bound,
                estimated_output_tokens=full.output_tokens_budget) is not None:
            return full
        history_count = len(model_input.ordered_history)
        if history_count == 0:
            return None

        # Output and retry reservations are fixed. Derive the maximum remaining
        # input allowance first, then use a bounded search over whole events.
        per_attempt_cap = self._policy.request_cost_limit_usd / (1 + self._policy.max_retries)
        output_cost = full.output_tokens_budget * self._policy.output_cost_per_million_tokens_usd / 1_000_000
        remaining = per_attempt_cap - output_cost
        input_price = self._policy.input_cost_per_million_tokens_usd
        if remaining < 0 or input_price == 0:
            maximum_input_tokens = math.inf if input_price == 0 and remaining >= 0 else -1
        else:
            maximum_input_tokens = math.floor(remaining * 1_000_000 / input_price)

        # Explicit negatives are never discarded to buy model capacity. Only
        # oldest non-negative events are eligible, and the newest event is kept
        # regardless of type. Relative order and repetitions remain unchanged.
        explicit_feedback_types = (EventType.MORE_LIKE_THIS, EventType.LESS_LIKE_THIS)
        removable = tuple(index for index, event in enumerate(model_input.ordered_history[:-1])
            if event.action_value is None and event.event_type not in explicit_feedback_types)
        protected = tuple(event for index, event in enumerate(model_input.ordered_history)
            if index not in removable)
        protected_only = prepare(replace(model_input, ordered_history=protected))
        if (protected_only.input_tokens_bound > maximum_input_tokens or
                self.reservation_estimate(estimated_input_tokens=protected_only.input_tokens_bound,
                    estimated_output_tokens=protected_only.output_tokens_budget) is None):
            return None

        low, high = 1, len(removable)
        best_omitted, best = len(removable), protected_only
        while low <= high:
            omitted = (low + high) // 2
            omitted_indexes = set(removable[:omitted])
            retained = tuple(event for index, event in enumerate(model_input.ordered_history)
                if index not in omitted_indexes)
            candidate = prepare(replace(model_input, ordered_history=retained))
            fits = (candidate.input_tokens_bound <= maximum_input_tokens and
                self.reservation_estimate(estimated_input_tokens=candidate.input_tokens_bound,
                    estimated_output_tokens=candidate.output_tokens_budget) is not None)
            if fits:
                best_omitted, best, high = omitted, candidate, omitted - 1
            else:
                low = omitted + 1
        if hasattr(best, "history_events_omitted"):
            best = replace(best, history_events_omitted=best_omitted)
        return best

    def prepare_with_reason(self, request: RankingRequest):
        """Return a prepared request or a stable, known pre-call fallback reason."""
        if (self._policy.input_cost_per_million_tokens_usd is None or
                self._policy.output_cost_per_million_tokens_usd is None):
            return None, "unknown_provider_pricing"
        if not callable(getattr(self._engine, "prepare", None)):
            return None, "provider_preparation_unavailable"
        try:
            prepared = self.prepare(request)
        except Exception as error:
            log_suppressed_exception("m2_provider_preparation_failed", error,
                candidates=len(request.candidates), history_events=len(request.ordered_history))
            return None, "provider_preparation_failed"
        return (prepared, "" if prepared is not None else "request_cost_limit")

    def observed_cost(self, *, input_tokens: int, output_tokens: int) -> float | None:
        return self._estimated_cost(input_tokens, output_tokens)

    def fallback(self, request: RankingRequest, reason: str) -> RankingResponseReceipt:
        return self._fallback(request, reason)

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens < 0
            or output_tokens < 0
        ):
            raise ValueError("token estimates must be non-negative")
        if self._policy.input_cost_per_million_tokens_usd is None or self._policy.output_cost_per_million_tokens_usd is None:
            return None
        return (
            input_tokens * self._policy.input_cost_per_million_tokens_usd
            + output_tokens * self._policy.output_cost_per_million_tokens_usd
        ) / 1_000_000

    @staticmethod
    def _receipt(
        request: RankingRequest,
        *,
        ranked_ids: tuple[str, ...],
        mode: RankingResultMode,
        reason: str,
    ) -> RankingResponseReceipt:
        return RankingResponseReceipt(
            schema_version=request.schema_version,
            request_id=request.request_id,
            policy_version=request.policy_version,
            model_version=request.model_version,
            history_revision=request.history_revision,
            history_generation=request.history_generation,
            consent_revision=request.consent_revision,
            server_commit_revision=request.server_commit_revision,
            newest_event_id=request.ordered_history[-1].event_id if request.ordered_history else None,
            candidate_ids=tuple(candidate.candidate_id for candidate in request.candidates),
            ranked_candidate_ids=ranked_ids,
            result_mode=mode,
            fallback_reason=reason,
        )

    def _fallback(self, request: RankingRequest, reason: str) -> RankingResponseReceipt:
        receipt = self._receipt(
            request,
            ranked_ids=tuple(candidate.candidate_id for candidate in request.candidates),
            mode=RankingResultMode.FALLBACK,
            reason=reason,
        )
        validate_ranking_response(receipt, request)
        return receipt
