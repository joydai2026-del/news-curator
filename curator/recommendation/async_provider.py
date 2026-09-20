"""Cancellable OpenAI-compatible transport using reviewed RankLLM prompt logic."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


class PromptBuilder(Protocol):
    """Adapter over the pinned RankLLM create_prompt implementation."""

    def create_prompt(self, *, query: str, passages: Sequence[str]) -> object: ...


# The five actions the model predicts. Exactly the actions the product captures:
# read_more, open_original, save, more_like_this and less_like_this all have a
# live write path. ask_question, dwell and dismiss deliberately are NOT here,
# because nothing captures them and a prediction nothing can score is a guess.
PREDICTED_ACTIONS = ("p_open", "p_read_original", "p_save", "p_more_like_this", "p_less_like_this")


@dataclass(frozen=True)
class AsyncProviderOutcome:
    order: tuple[int, ...]
    input_tokens: int
    output_tokens: int
    request_id: str
    attempts: int
    # Per-candidate action likelihoods, in the order the identifiers appear.
    # Empty in permutation mode, which is still the fallback shape.
    predictions: tuple[Mapping[str, float], ...] = ()


class ProviderTimeout(RuntimeError):
    pass


class ProviderHTTPError(RuntimeError):
    def __init__(self, reason: str) -> None:
        if reason not in {"provider_http_4xx", "provider_http_5xx"}:
            raise ValueError("invalid provider HTTP category")
        super().__init__(reason)
        self.reason = reason


class ProviderTransportFailure(RuntimeError):
    pass


class ProviderResponseInvalid(RuntimeError):
    pass


class ProviderResponseError(ValueError):
    """A provider response failed validation after reporting trustworthy usage."""

    def __init__(self, message: str, *, input_tokens: int, output_tokens: int, request_id: str) -> None:
        super().__init__(message)
        self.input_tokens, self.output_tokens, self.request_id = input_tokens, output_tokens, request_id


def exact_order_schema(candidate_count: int) -> dict[str, object]:
    if type(candidate_count) is not int or candidate_count < 1:
        raise ValueError("candidate count must be positive")
    return {"type": "json_schema", "name": "rank_order", "strict": True, "schema": {
        "type": "object", "properties": {"order": {"type": "array",
            "description": f"Permutation of identifiers 1 through {candidate_count}, each exactly once",
            "items": {"type": "integer", "minimum": 1, "maximum": candidate_count},
            "minItems": candidate_count, "maxItems": candidate_count}},
        "required": ["order"], "additionalProperties": False}}


def exact_prediction_schema(candidate_count: int) -> dict[str, object]:
    """One prediction per candidate: the model states likelihoods, not an order.

    The safety property of the permutation schema is preserved, carried on an
    object array instead: the multiset of identifiers must still be an exact
    non-repeating permutation of 1..n, so a malicious headline still cannot make
    the model emit free text or smuggle history back out.
    """
    if type(candidate_count) is not int or candidate_count < 1:
        raise ValueError("candidate count must be positive")
    probability = {"type": "number", "minimum": 0, "maximum": 1}
    return {"type": "json_schema", "name": "action_predictions", "strict": True, "schema": {
        "type": "object", "properties": {"predictions": {"type": "array",
            "description": f"One entry per identifier 1 through {candidate_count}, each exactly once",
            "items": {"type": "object", "properties": {
                "id": {"type": "integer", "minimum": 1, "maximum": candidate_count},
                **{action: dict(probability) for action in PREDICTED_ACTIONS}},
                "required": ["id", *PREDICTED_ACTIONS], "additionalProperties": False},
            "minItems": candidate_count, "maxItems": candidate_count}},
        "required": ["predictions"], "additionalProperties": False}}


class AsyncOpenAIResponses:
    """One immutable HTTPX client; timeout closes its transport on cancellation."""

    def __init__(self, *, client, endpoint: str, api_key: str, model: str, max_output_tokens: int,
                 reasoning_effort: str = "minimal", verbosity: str = "low", total_seconds: float = 6.0,
                 predict_actions: bool = False) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("provider endpoint must be HTTPS")
        if (not api_key or not model or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536
                or reasoning_effort not in {"none", "minimal", "low", "medium", "high"}
                or verbosity not in {"low", "medium", "high"} or not 0 < total_seconds <= 6):
            raise ValueError("invalid provider transport configuration")
        self._client, self._endpoint, self._api_key = client, endpoint.rstrip("/"), api_key
        self._model, self._total = model, total_seconds
        self._max_output_tokens, self._reasoning_effort, self._verbosity = max_output_tokens, reasoning_effort, verbosity
        self._predict_actions = predict_actions

    @property
    def predicts_actions(self) -> bool:
        return self._predict_actions

    def schema(self, candidate_count: int) -> dict[str, object]:
        return (exact_prediction_schema(candidate_count) if self._predict_actions
                else exact_order_schema(candidate_count))

    async def create(self, prompt: object, *, candidate_count: int) -> Mapping[str, object]:
        # The base ingestion environment imports this contract without installing
        # model-only HTTP dependencies. Load HTTPX only on an actual provider call.
        import httpx

        # wait_for preserves the repository's Python 3.10 CI support while
        # cancelling the whole request on the same total deadline.
        async def request():
            response = await self._client.post(self._endpoint + "/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": prompt, "store": False,
                    "max_output_tokens": self._max_output_tokens,
                    "reasoning": {"effort": self._reasoning_effort},
                    "text": {"verbosity": self._verbosity, "format": self.schema(candidate_count)}})
            if 400 <= response.status_code < 500:
                raise ProviderHTTPError("provider_http_4xx")
            if response.status_code >= 500:
                raise ProviderHTTPError("provider_http_5xx")
            if not 200 <= response.status_code < 300:
                raise ProviderResponseInvalid("provider response invalid")
            try:
                value = response.json()
            except ValueError:
                raise ProviderResponseInvalid("provider response invalid") from None
            if not isinstance(value, Mapping):
                raise ProviderResponseInvalid("provider response invalid")
            return value
        try:
            return await asyncio.wait_for(request(), timeout=self._total)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            await self._client.aclose()
            raise ProviderTimeout("provider total deadline exceeded") from None
        except httpx.TransportError:
            raise ProviderTransportFailure("provider transport failed") from None


class AsyncRankLLMProvider:
    """Uses RankLLM prompt construction while validating raw output exactly."""

    def __init__(self, *, prompt_builder: PromptBuilder, transport: AsyncOpenAIResponses) -> None:
        self._prompt_builder, self._transport = prompt_builder, transport

    async def rerank(self, *, query: str, passages: Sequence[str]) -> AsyncProviderOutcome:
        prompt = self._prompt_builder.create_prompt(query=query, passages=passages)
        return await self.rerank_prompt(prompt=prompt, candidate_count=len(passages))

    async def rerank_prompt(self, *, prompt: object, candidate_count: int) -> AsyncProviderOutcome:
        response = await self._transport.create(prompt, candidate_count=candidate_count)
        texts = []
        for item in response.get("output", ()) if isinstance(response.get("output"), list) else ():
            if isinstance(item, Mapping) and item.get("type") == "message":
                for content in item.get("content", ()) if isinstance(item.get("content"), list) else ():
                    if isinstance(content, Mapping) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        texts.append(content["text"])
        output = "".join(texts) if texts else None
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise ProviderResponseInvalid("provider response invalid")
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        request_id = response.get("id")
        usage_valid = (type(input_tokens) is int and type(output_tokens) is int and input_tokens >= 0
                       and output_tokens >= 0 and isinstance(request_id, str) and bool(request_id))
        predictions: tuple[Mapping[str, float], ...] = ()
        try:
            parsed = json.loads(output) if isinstance(output, str) else None
            if self._transport.predicts_actions:
                order_value, predictions = _parse_predictions(parsed, candidate_count)
                valid_order = order_value is not None
            else:
                order_value = parsed.get("order") if isinstance(parsed, Mapping) and set(parsed) == {"order"} else None
                valid_order = (isinstance(order_value, list) and len(order_value) == candidate_count
                    and all(type(value) is int and 1 <= value <= candidate_count for value in order_value)
                    and len(set(order_value)) == candidate_count)
        except json.JSONDecodeError:
            order_value, valid_order = None, False
        if not valid_order:
            if usage_valid:
                raise ProviderResponseError("invalid raw provider response", input_tokens=input_tokens,
                    output_tokens=output_tokens, request_id=request_id)
            raise ProviderResponseInvalid("provider response invalid")
        order = tuple(order_value)
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
            raise ProviderResponseInvalid("provider response invalid")
        if not isinstance(request_id, str) or not request_id:
            raise ProviderResponseInvalid("provider response invalid")
        return AsyncProviderOutcome(order, input_tokens, output_tokens, request_id, 1, predictions)


def _parse_predictions(parsed, candidate_count):
    """Validate the object array with the SAME invariant the permutation had.

    The identifiers must be an exact non-repeating permutation of 1..n and every
    probability must be a finite number in [0, 1]. Anything else is rejected,
    exactly as a malformed permutation was.
    """
    if not isinstance(parsed, Mapping) or set(parsed) != {"predictions"}:
        return None, ()
    entries = parsed["predictions"]
    if not isinstance(entries, list) or len(entries) != candidate_count:
        return None, ()
    by_id: dict[int, Mapping[str, float]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"id", *PREDICTED_ACTIONS}:
            return None, ()
        identifier = entry["id"]
        if type(identifier) is not int or not 1 <= identifier <= candidate_count or identifier in by_id:
            return None, ()
        values = {}
        for action in PREDICTED_ACTIONS:
            value = entry[action]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, ()
            value = float(value)
            if value != value or value in (float("inf"), float("-inf")) or not 0.0 <= value <= 1.0:
                return None, ()
            values[action] = value
        by_id[identifier] = values
    if len(by_id) != candidate_count:
        return None, ()
    order = sorted(by_id)
    return order, tuple(by_id[identifier] for identifier in order)
