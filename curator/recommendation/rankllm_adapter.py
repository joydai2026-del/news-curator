"""News Curator-owned safety boundary around RankLLM listwise ranking.

The vendor package is injected behind ``RankLLMEngine``. This module never
imports it, reads credentials, chooses an endpoint, or opens a socket.
"""

from __future__ import annotations

import math
import threading
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
from .async_provider import (MAXIMUM_PROVIDER_DEADLINE_SECONDS, ProviderHTTPError, ProviderResponseError,
    ProviderResponseInvalid, ProviderTimeout, ProviderTransportFailure)
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
    # The prompt BUDGET. How much of the request the model is shown, before any
    # cost fitting runs. Both come from the ranker policy's `prompt` section.
    max_history_events: int = 24
    max_model_candidates: int = 25

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
        if (not finite_number(self.deadline_seconds) or self.deadline_seconds <= 0
                or self.deadline_seconds > MAXIMUM_PROVIDER_DEADLINE_SECONDS):
            # Constant literal on purpose: diagnostics.py only passes a reviewed
            # constant through to the operator log, and an f-string here would
            # come back "suppressed". Change it with
            # MAXIMUM_PROVIDER_DEADLINE_SECONDS, never separately.
            raise ValueError("ranker deadline must be within sixty seconds")
        if type(self.max_history_events) is not int or not 0 <= self.max_history_events <= 200:
            raise ValueError("ranker prompt.max_history_events must be an integer between 0 and 200")
        if type(self.max_model_candidates) is not int or not 5 <= self.max_model_candidates <= 100:
            raise ValueError("ranker prompt.max_model_candidates must be an integer between 5 and 100")
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
                outcome = self._call_within(
                    (lambda: self._engine.rerank_prepared(prepared, timeout_seconds=remaining))
                    if prepared is not None else
                    (lambda: self._engine.rerank(self._budgeted_input(request.model_input())[0],
                                                 timeout_seconds=remaining)),
                    remaining)
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
            ranked_ids=self._complete_order(request, outcome.ranked_candidate_ids),
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

    def _budgeted_input(self, model_input):
        """Apply the prompt budget: newest N history events, first N candidates.

        This runs BEFORE the cost fitting below, and it is the reason a request
        fits at all. The live owner request prepared 50 candidates and 72 history
        events, about 38,000 input tokens, and every attempt ran out of the
        provider deadline. The budget is config (`prompt.max_history_events`,
        `prompt.max_model_candidates`), so shrinking or growing what the model
        reads never needs a code change.

        Candidates are cut from the TAIL, so the model reorders the first N of
        the recipe order and the rest keep the recipe order behind them (see
        `_complete_order`). History is cut from the FRONT, so the newest
        behavior, which is the behavior that describes the reader now, survives.
        """
        history = model_input.ordered_history
        candidates = model_input.candidates
        kept_history = history[len(history) - self._policy.max_history_events:] if (
            self._policy.max_history_events < len(history)) else history
        kept_candidates = candidates[:self._policy.max_model_candidates]
        if len(kept_history) == len(history) and len(kept_candidates) == len(candidates):
            return model_input, 0, 0
        return (replace(model_input, ordered_history=kept_history, candidates=kept_candidates),
                len(history) - len(kept_history), len(candidates) - len(kept_candidates))

    @staticmethod
    def _complete_order(request: RankingRequest, ranked_ids: tuple[str, ...]) -> tuple[str, ...]:
        """The model's order, then every candidate it never saw, in recipe order.

        The prompt budget hands the provider the first `prompt.max_model_candidates`
        candidates. The response contract is still an exact permutation of the
        WHOLE request, so the tail is appended here, unreordered. A model that
        returns a malformed head still fails `validate_ranking_response`, because
        nothing is dropped or invented: ids arrive in the order the model gave
        them, then the untouched remainder in the order the recipe chose.
        """
        seen = set(ranked_ids)
        if len(seen) != len(ranked_ids):
            # A repeated id is a malformed answer, not a short one. Completing it
            # would turn a duplicate into a valid-looking permutation, so it is
            # handed back untouched for `validate_ranking_response` to reject.
            return tuple(ranked_ids)
        return tuple(ranked_ids) + tuple(
            candidate.candidate_id for candidate in request.candidates
            if candidate.candidate_id not in seen)

    def _call_within(self, call, seconds: float):
        """Run the provider call under a wall clock the ADAPTER owns.

        The engine already cancels its own HTTP request on the same budget, and
        normally that typed timeout is what returns. This is the backstop for the
        case it cannot cover: an engine that blocks somewhere other than the
        awaited request still has to answer the reader, because the alternative
        is the container's function timeout killing the whole request and the
        reader seeing a 500 instead of a typed `provider_deadline` fallback.

        The worker is a daemon, so a wedged provider call can never hold the
        process open, and it is the only thing inside the bound: the observers
        that record usage and cost run on this thread, after it returns.
        """
        result: list = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                result.append(call())
            except BaseException as error:  # re-raised on the calling thread below
                failure.append(error)

        worker = threading.Thread(target=run, name="m2-provider-call", daemon=True)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            raise ProviderTimeout("provider wait exceeded the adapter deadline")
        if failure:
            raise failure[0]
        return result[0]

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
        model_input, events_dropped, candidates_dropped = self._budgeted_input(request.model_input())
        full = prepare(model_input)
        if self.reservation_estimate(estimated_input_tokens=full.input_tokens_bound,
                estimated_output_tokens=full.output_tokens_budget) is not None:
            return self._with_budget_counters(full, events_dropped, candidates_dropped)
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
        return self._with_budget_counters(best, events_dropped, candidates_dropped)

    @staticmethod
    def _with_budget_counters(prepared, history_events_omitted: int, candidates_omitted: int):
        if not hasattr(prepared, "history_events_budget_omitted"):
            return prepared
        return replace(prepared, history_events_budget_omitted=history_events_omitted,
                       candidates_budget_omitted=candidates_omitted)

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
