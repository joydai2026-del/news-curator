"""Cancellable OpenAI-compatible transport using reviewed RankLLM prompt logic."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


class PromptBuilder(Protocol):
    """Adapter over the pinned RankLLM create_prompt implementation."""

    def create_prompt(self, *, query: str, passages: Sequence[str]) -> object: ...


@dataclass(frozen=True)
class AsyncProviderOutcome:
    order: tuple[int, ...]
    input_tokens: int
    output_tokens: int
    request_id: str
    attempts: int


class ProviderTimeout(RuntimeError):
    pass


class ProviderResponseError(ValueError):
    """A provider response failed validation after reporting trustworthy usage."""

    def __init__(self, message: str, *, input_tokens: int, output_tokens: int, request_id: str) -> None:
        super().__init__(message)
        self.input_tokens, self.output_tokens, self.request_id = input_tokens, output_tokens, request_id


class AsyncOpenAIResponses:
    """One immutable HTTPX client; timeout closes its transport on cancellation."""

    def __init__(self, *, client, endpoint: str, api_key: str, model: str, max_output_tokens: int,
                 reasoning_effort: str = "minimal", verbosity: str = "low", total_seconds: float = 6.0) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("provider endpoint must be HTTPS")
        if (not api_key or not model or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536
                or reasoning_effort not in {"none", "minimal", "low", "medium", "high"}
                or verbosity not in {"low", "medium", "high"} or not 0 < total_seconds <= 6):
            raise ValueError("invalid provider transport configuration")
        self._client, self._endpoint, self._api_key = client, endpoint.rstrip("/"), api_key
        self._model, self._total = model, total_seconds
        self._max_output_tokens, self._reasoning_effort, self._verbosity = max_output_tokens, reasoning_effort, verbosity

    async def create(self, prompt: object) -> Mapping[str, object]:
        try:
            async with asyncio.timeout(self._total):
                response = await self._client.post(self._endpoint + "/responses",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "input": prompt, "store": False,
                        "max_output_tokens": self._max_output_tokens,
                        "reasoning": {"effort": self._reasoning_effort}, "text": {"verbosity": self._verbosity}})
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, Mapping):
                    raise ValueError("provider response must be an object")
                return value
        except TimeoutError as exc:
            await self._client.aclose()
            raise ProviderTimeout("provider total deadline exceeded") from exc


class AsyncRankLLMProvider:
    """Uses RankLLM prompt construction while validating raw output exactly."""

    _RAW_PERMUTATION = re.compile(r"^\s*\[\d+\](\s*>\s*\[\d+\])*\s*$").fullmatch

    def __init__(self, *, prompt_builder: PromptBuilder, transport: AsyncOpenAIResponses) -> None:
        self._prompt_builder, self._transport = prompt_builder, transport

    async def rerank(self, *, query: str, passages: Sequence[str]) -> AsyncProviderOutcome:
        prompt = self._prompt_builder.create_prompt(query=query, passages=passages)
        return await self.rerank_prompt(prompt=prompt, candidate_count=len(passages))

    async def rerank_prompt(self, *, prompt: object, candidate_count: int) -> AsyncProviderOutcome:
        response = await self._transport.create(prompt)
        texts = []
        for item in response.get("output", ()) if isinstance(response.get("output"), list) else ():
            if isinstance(item, Mapping) and item.get("type") == "message":
                for content in item.get("content", ()) if isinstance(item.get("content"), list) else ():
                    if isinstance(content, Mapping) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        texts.append(content["text"])
        output = "".join(texts) if texts else None
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise ValueError("invalid raw provider response")
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        request_id = response.get("id")
        usage_valid = (type(input_tokens) is int and type(output_tokens) is int and input_tokens >= 0
                       and output_tokens >= 0 and isinstance(request_id, str) and bool(request_id))
        if not isinstance(output, str) or not self._RAW_PERMUTATION(output):
            if usage_valid:
                raise ProviderResponseError("invalid raw provider response", input_tokens=input_tokens,
                    output_tokens=output_tokens, request_id=request_id)
            raise ValueError("invalid raw provider response")
        order = tuple(int(value) for value in re.findall(r"\[(\d+)\]", output))
        if sorted(order) != list(range(1, candidate_count + 1)):
            if usage_valid:
                raise ProviderResponseError("provider response is not an exact permutation",
                    input_tokens=input_tokens, output_tokens=output_tokens, request_id=request_id)
            raise ValueError("provider response is not an exact permutation")
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
            raise ValueError("invalid provider usage")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("missing provider request id")
        return AsyncProviderOutcome(order, input_tokens, output_tokens, request_id, 1)
