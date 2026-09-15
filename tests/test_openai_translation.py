import json

import pytest
from datetime import datetime, timezone

from curator.sources import SafeHttpResponse
from curator.translation import (
    OpenAITranslationAdapter,
    OpenAITranslationConfig,
    TranslationInput,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationRequestItem,
)
from curator.models import Item


class Transport:
    def __init__(self, response): self.response = response; self.calls = []
    def request(self, *args, **kwargs): self.calls.append((args, kwargs)); return self.response


def item():
    return Item(title="标题", description="摘要", url="https://news.example/story", canonical_url="https://news.example/story", source_name="Public", source_id="public", language="zh", published_at=datetime(2026, 9, 15, tzinfo=timezone.utc))


def request():
    return TranslationProviderRequest((TranslationRequestItem("story-1", TranslationInput.from_item(item())),), "zh", "en")


def response(payload):
    return SafeHttpResponse(200, "https://api.openai.com/v1/responses", {}, json.dumps(payload).encode())


def good_response():
    return {"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps({"items":[{"request_id":"story-1","title":"Title","description":"Summary"}]})}]}],"usage":{"input_tokens":12,"output_tokens":8}}


def test_openai_adapter_uses_store_false_strict_json_and_exact_public_items():
    transport = Transport(response(good_response()))
    adapter = OpenAITranslationAdapter(config=OpenAITranslationConfig(), transport=transport, api_key=lambda: "sk-test" )
    result = adapter.translate(request())
    assert result.items[0].title == "Title"
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 8
    _, kwargs = transport.calls[0]
    payload = json.loads(kwargs["body"])
    assert payload["model"] == "gpt-5-mini" and payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["reasoning"] == {"effort":"minimal"}
    assert "untrusted data" in payload["input"][0]["content"][0]["text"]
    assert payload["input"][1]["content"][0]["text"] == '{"source_language":"zh","target_language":"en","items":[{"request_id":"story-1","title":"标题","description":"摘要"}]}'
    assert "owner" not in json.dumps(payload).lower()


@pytest.mark.parametrize("payload", [
    {"status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1}},
    {"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"{}"}]}],"usage":{"input_tokens":1,"output_tokens":1}},
    {"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps({"items":[{"request_id":"other","title":"X","description":""}]})}]}],"usage":{"input_tokens":1,"output_tokens":1}},
    good_response() | {"usage":{"input_tokens":True,"output_tokens":1}},
])
def test_openai_adapter_fails_closed_for_malformed_or_uncorrelated_response(payload):
    adapter = OpenAITranslationAdapter(config=OpenAITranslationConfig(), transport=Transport(response(payload)), api_key=lambda: "sk-test")
    with pytest.raises(TranslationProviderError) as error:
        adapter.translate(request())
    assert error.value.reason_code == "malformed_response"

def test_openai_adapter_accepts_reasoning_plus_exactly_one_message():
    payload=good_response(); payload["output"].insert(0,{"type":"reasoning","summary":[]})
    result=OpenAITranslationAdapter(config=OpenAITranslationConfig(),transport=Transport(response(payload)),api_key=lambda:"sk-test").translate(request())
    assert result.items[0].title=="Title"

def test_openai_money_units_and_conservative_serialized_input_ceiling():
    adapter=OpenAITranslationAdapter(config=OpenAITranslationConfig(),transport=Transport(response(good_response())),api_key=lambda:"sk-test")
    assert adapter.price_policy==(250_000,2_000_000)
    assert adapter.maximum_billable_tokens==(128*1024,4096)
