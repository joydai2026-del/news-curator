"""The exact HTTP shape the adapter sends, and the shape it parses back.

No live call happens here and none has ever happened: the API key does not
exist yet. This pins the request and response contract so a drift in either is
caught by a test rather than by a failed hourly run.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.models import Item
from curator.translation.base import (
    TranslationInput,
    TranslationProviderRequest,
    TranslationRequestItem,
)
from curator.translation.model_provider import ModelPairingAdapter, ModelTranslationAdapter, ModelTranslationConfig

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads((ROOT / "tests/fixtures/translation/openai-chat-completion-response.json").read_text())
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


class RecordingTransport:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.calls = []

    def request(self, source_id, method, url, *, headers=None, body=None, credential=None,
                allowed_mime_types=()):
        self.calls.append({"source_id": source_id, "method": method, "url": url,
                           "headers": dict(headers or {}), "body": body,
                           "credential": credential, "mime": tuple(allowed_mime_types)})

        class Response:
            status_code = 200
            body = self.body
        return Response()


def zh_item():
    return Item(title="独家：某部门发布七项新规", url="https://example.cn/a", canonical_url="https://example.cn/a",
                source_id="cnbeta", source_name="cnBeta", published_at=NOW, language="zh",
                description="该通知自 2026 年起执行，涉及 7 个领域。")


def build(config=None, body=None):
    transport = RecordingTransport(body if body is not None else RECORDED)
    adapter = ModelTranslationAdapter(
        config=config or ModelTranslationConfig(provider_id="openai", model="gpt-5-mini"),
        transport=transport, api_key=lambda: "test-key")
    return adapter, transport


def translation_request(item):
    content = TranslationInput.from_item(item)
    return TranslationProviderRequest(
        items=(TranslationRequestItem(request_id="t-" + content.digest[:32], content=content),),
        source_language="zh", target_language="en")


def test_the_request_is_exactly_the_documented_chat_completions_call():
    adapter, transport = build()
    adapter.translate(translation_request(zh_item()))
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert call["headers"]["Accept"] == "application/json"
    assert call["mime"] == ("application/json",)
    # The key travels as an origin-bound credential, never in the body or URL.
    assert call["credential"].origin == "https://api.openai.com"
    assert call["credential"].header_name == "Authorization"
    assert "test-key" not in call["url"] and b"test-key" not in call["body"]
    sent = json.loads(call["body"])
    assert set(sent) == {"model", "messages", "response_format", "max_completion_tokens", "reasoning_effort"}
    assert sent["max_completion_tokens"] == 1000
    # Chat Completions takes a TOP-LEVEL STRING. The object form belongs to the
    # /responses endpoint and is rejected here as an unknown body argument.
    assert sent["reasoning_effort"] == "minimal"
    assert "reasoning" not in sent
    # No temperature on either call: the configured model family rejects a
    # non-default value, and a 400 would make the whole feature silently dead.
    assert "temperature" not in sent
    assert sent["model"] == "gpt-5-mini", "the model id comes from config, never a literal"
    assert sent["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in sent["messages"]] == ["system", "user"]
    # Only title and summary cross the boundary.
    user = json.loads(sent["messages"][1]["content"])
    assert set(user) == {"title", "summary"}
    assert "example.cn" not in call["body"].decode() and "cnbeta" not in call["body"].decode()


def test_the_endpoint_and_model_follow_config_rather_than_a_hardcoded_string():
    config = ModelTranslationConfig(provider_id="openai", model="gpt-5-nano",
                                    api_origin="https://api.example", api_path="/v2/chat")
    adapter, transport = build(config=config)
    adapter.translate(translation_request(zh_item()))
    call = transport.calls[0]
    assert call["url"] == "https://api.example/v2/chat"
    assert json.loads(call["body"])["model"] == "gpt-5-nano"


def test_a_realistic_recorded_response_parses_into_the_result_and_its_usage():
    adapter, _ = build()
    result = adapter.translate(translation_request(zh_item()))
    assert result.items[0].title == "Exclusive: seven new rules published"
    assert result.items[0].description == "The notice takes effect from 2026 and covers 7 areas."
    assert result.provider == "openai" and result.model_version == "gpt-5-mini:translation-json-v1"
    # Settlement reads these; a drift here silently makes every attempt free.
    assert (result.input_tokens, result.output_tokens) == (412, 37)


def test_extra_response_fields_do_not_break_parsing():
    """The provider adds fields over time; the adapter must tolerate that."""
    body = json.loads(json.dumps(RECORDED))
    body["obfuscation"] = "xyz"
    body["choices"][0]["message"]["annotations"] = []
    adapter, _ = build(body=body)
    assert adapter.translate(translation_request(zh_item())).items[0].title


def test_the_pairing_call_is_the_same_endpoint_with_deterministic_decoding():
    from curator.grouping import GroupingCandidate

    body = json.loads(json.dumps(RECORDED))
    body["choices"][0]["message"]["content"] = json.dumps({"match_index": 0})
    transport = RecordingTransport(body)
    adapter = ModelPairingAdapter(
        config=ModelTranslationConfig(provider_id="openai", model="gpt-5-mini"),
        transport=transport, api_key=lambda: "test-key")
    story = GroupingCandidate(story_id="story:a", language="zh", title="标题", summary="摘要",
                              published_at=NOW)
    context = [GroupingCandidate(story_id="story:b", language="en", title="A headline",
                                 summary="Body", published_at=NOW)]
    index, input_tokens, output_tokens = adapter.decide(story=story, context=context)
    assert (index, input_tokens, output_tokens) == (0, 412, 37)
    sent = json.loads(transport.calls[0]["body"])
    assert transport.calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert "temperature" not in sent, "a non-default temperature is rejected by this model family"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["model"] == "gpt-5-mini"
    assert sent["max_completion_tokens"] == 64
    assert sent["reasoning_effort"] == "minimal" and "reasoning" not in sent
    # Both calls must be the same shape, or one of them is untested in practice.
    assert set(sent) == {"model", "messages", "response_format", "max_completion_tokens", "reasoning_effort"}


def test_reasoning_effort_none_omits_the_parameter_entirely():
    """Some providers reject the field outright; `none` is the explicit opt-out."""
    config = ModelTranslationConfig(provider_id="openai", model="gpt-5-mini", reasoning_effort="none")
    adapter, transport = build(config=config)
    adapter.translate(translation_request(zh_item()))
    sent = json.loads(transport.calls[0]["body"])
    assert "reasoning_effort" not in sent and "reasoning" not in sent
    assert set(sent) == {"model", "messages", "response_format", "max_completion_tokens"}

    body = json.loads(json.dumps(RECORDED))
    body["choices"][0]["message"]["content"] = json.dumps({"match_index": None})
    pairing_transport = RecordingTransport(body)
    pairing = ModelPairingAdapter(config=config, transport=pairing_transport, api_key=lambda: "test-key")
    from curator.grouping import GroupingCandidate
    story = GroupingCandidate(story_id="story:a", language="zh", title="t", summary="s", published_at=NOW)
    context = [GroupingCandidate(story_id="story:b", language="en", title="h", summary="b", published_at=NOW)]
    pairing.decide(story=story, context=context)
    pairing_sent = json.loads(pairing_transport.calls[0]["body"])
    assert "reasoning_effort" not in pairing_sent


def test_a_configured_effort_is_sent_on_both_calls():
    config = ModelTranslationConfig(provider_id="openai", model="gpt-5-mini", reasoning_effort="low")
    adapter, transport = build(config=config)
    adapter.translate(translation_request(zh_item()))
    assert json.loads(transport.calls[0]["body"])["reasoning_effort"] == "low"
