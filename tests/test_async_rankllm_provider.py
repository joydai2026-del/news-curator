import asyncio

import httpx
import pytest

from curator.recommendation.async_provider import AsyncOpenAIResponses, AsyncRankLLMProvider, ProviderTimeout


class Prompt:
    def create_prompt(self, *, query, passages):
        return [{"role": "user", "content": query + "\n" + "\n".join(passages)}]


def test_exact_raw_permutation_and_usage_are_preserved():
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "id": "response-1", "output": [{"type": "message", "content": [{"type": "output_text", "text": "[2] > [1]"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5}}))
        client = httpx.AsyncClient(transport=transport)
        provider = AsyncRankLLMProvider(prompt_builder=Prompt(), transport=AsyncOpenAIResponses(
            client=client, endpoint="https://provider.invalid/v1", api_key="test", model="test", max_output_tokens=32))
        outcome = await provider.rerank(query="policy", passages=["first", "second"])
        await client.aclose()
        assert outcome.order == (2, 1)
        assert (outcome.input_tokens, outcome.output_tokens, outcome.request_id) == (12, 5, "response-1")
    asyncio.run(run())


@pytest.mark.parametrize("raw", ["2 1", "[2] > [2]", "[2] > [1] explanation"])
def test_unvalidated_or_nonpermutation_output_is_rejected(raw):
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "id": "response-1", "output": [{"type": "message", "content": [{"type": "output_text", "text": raw}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}))
        client = httpx.AsyncClient(transport=transport)
        provider = AsyncRankLLMProvider(prompt_builder=Prompt(), transport=AsyncOpenAIResponses(
            client=client, endpoint="https://provider.invalid/v1", api_key="test", model="test", max_output_tokens=32))
        with pytest.raises(ValueError):
            await provider.rerank(query="policy", passages=["first", "second"])
        await client.aclose()
    asyncio.run(run())


@pytest.mark.allow_socket
def test_real_stalled_loopback_connection_is_cancelled_and_transport_closed():
    async def run():
        accepted = asyncio.Event()
        async def stall(reader, writer):
            accepted.set()
            try:
                await reader.read()
            finally:
                writer.close()
        try:
            server = await asyncio.start_server(stall, "127.0.0.1", 0)
        except PermissionError:
            pytest.skip("loopback bind denied by the outer sandbox")
        port = server.sockets[0].getsockname()[1]
        class LoopbackTransport(httpx.AsyncBaseTransport):
            def __init__(self): self.inner = httpx.AsyncHTTPTransport()
            async def handle_async_request(self, request):
                request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
                return await self.inner.handle_async_request(request)
            async def aclose(self): await self.inner.aclose()
        client = httpx.AsyncClient(timeout=None, transport=LoopbackTransport())
        provider = AsyncOpenAIResponses(client=client, endpoint="https://provider.invalid/v1",
            api_key="test", model="test", max_output_tokens=32, total_seconds=0.05)
        with pytest.raises(ProviderTimeout):
            await provider.create([])
        assert accepted.is_set()
        assert client.is_closed
        server.close()
        await server.wait_closed()
    asyncio.run(run())
