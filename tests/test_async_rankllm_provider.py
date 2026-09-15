import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from curator.recommendation.async_provider import (AsyncOpenAIResponses, AsyncRankLLMProvider, ProviderHTTPError,
    ProviderResponseInvalid, ProviderTimeout, ProviderTransportFailure)


@pytest.fixture(autouse=True)
def supported_asyncio_api(monkeypatch):
    # Python 3.10 has wait_for but not asyncio.timeout. Exercise the transport
    # without the newer API, including the actual stalled-connection case.
    monkeypatch.delattr(asyncio, "timeout", raising=False)


class Prompt:
    def create_prompt(self, *, query, passages):
        return [{"role": "user", "content": query + "\n" + "\n".join(passages)}]


def test_provider_contract_imports_without_model_only_site_packages():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import curator.recommendation.async_provider"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_exact_raw_permutation_and_usage_are_preserved():
    sent = []
    async def run():
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={
            "id": "response-1", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"order":[2,1]}'}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5}})
        transport = httpx.MockTransport(respond)
        client = httpx.AsyncClient(transport=transport)
        provider = AsyncRankLLMProvider(prompt_builder=Prompt(), transport=AsyncOpenAIResponses(
            client=client, endpoint="https://provider.invalid/v1", api_key="test", model="test", max_output_tokens=32))
        outcome = await provider.rerank(query="policy", passages=["first", "second"])
        await client.aclose()
        assert outcome.order == (2, 1)
        assert (outcome.input_tokens, outcome.output_tokens, outcome.request_id) == (12, 5, "response-1")
        body = __import__('json').loads(sent[0].content)
        schema = body['text']['format']['schema']['properties']['order']
        assert (schema['minItems'], schema['maxItems'], schema['items']['minimum'], schema['items']['maximum']) == (2, 2, 1, 2)
    asyncio.run(run())


@pytest.mark.parametrize("raw", ["2 1", '{"order":[2,2]}', '{"order":[2]}', '{"order":[3,1]}',
    '{"order":[2,1],"extra":true}', '{"order":[true,1]}'])
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


@pytest.mark.parametrize(("response", "error", "reason"), [
    (httpx.Response(302, json={"id": "private-response-marker", "output": [], "usage": {}}), ProviderResponseInvalid, None),
    (httpx.Response(401, text="private-response-marker"), ProviderHTTPError, "provider_http_4xx"),
    (httpx.Response(503, text="private-response-marker"), ProviderHTTPError, "provider_http_5xx"),
    (httpx.Response(200, text="private-response-marker"), ProviderResponseInvalid, None),
])
def test_provider_failure_categories_do_not_retain_response_body(response, error, reason):
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
        transport = AsyncOpenAIResponses(client=client, endpoint="https://provider.invalid/v1",
            api_key="test", model="test", max_output_tokens=32)
        with pytest.raises(error) as caught:
            await transport.create([], candidate_count=1)
        assert "private-response-marker" not in str(caught.value)
        assert caught.value.__cause__ is None
        if reason is not None:
            assert caught.value.reason == reason
        await client.aclose()
    asyncio.run(run())


def test_transport_failure_does_not_retain_exception_detail():
    async def run():
        def fail(request):
            raise httpx.ConnectError("private-transport-marker", request=request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        transport = AsyncOpenAIResponses(client=client, endpoint="https://provider.invalid/v1",
            api_key="test", model="test", max_output_tokens=32)
        with pytest.raises(ProviderTransportFailure) as caught:
            await transport.create([], candidate_count=1)
        assert "private-transport-marker" not in str(caught.value)
        assert caught.value.__cause__ is None
        await client.aclose()
    asyncio.run(run())


def test_missing_usage_is_classified_without_raw_response_detail():
    async def run():
        marker = "private-provider-marker"
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "id": marker, "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"order":[1]}' }]}]})))
        provider = AsyncRankLLMProvider(prompt_builder=Prompt(), transport=AsyncOpenAIResponses(
            client=client, endpoint="https://provider.invalid/v1", api_key="test", model="test", max_output_tokens=32))
        with pytest.raises(ProviderResponseInvalid) as caught:
            await provider.rerank(query="policy", passages=["first"])
        assert marker not in str(caught.value)
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
            await provider.create([], candidate_count=1)
        assert accepted.is_set()
        assert client.is_closed
        server.close()
        await server.wait_closed()
    asyncio.run(run())
