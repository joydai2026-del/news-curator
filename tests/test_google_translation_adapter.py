from __future__ import annotations

import json
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from curator.models import Item
from curator.sources import SafeHttpResponse, SafeTransportError, SafeTransportReason
from curator.translation import (
    GoogleTranslationAdapter,
    GoogleTranslationConfig,
    TranslationErrorReason,
    TranslationInput,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationRequestItem,
)
from curator.translation.google import GOOGLE_TRANSLATION_ORIGIN


def make_item(title, *, url="https://example.com/a", description=""):
    return Item(
        title=title,
        url=url,
        canonical_url=url,
        source_id="fixture",
        source_name="Fixture",
        published_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        description=description,
    )


class CapturingTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response(payload, *, status=200):
    return SafeHttpResponse(
        status_code=status,
        url=GOOGLE_TRANSLATION_ORIGIN,
        headers=MappingProxyType({"content-type": "application/json"}),
        body=json.dumps(payload, ensure_ascii=False).encode(),
    )


def request_for(*, description="Summary"):
    content = TranslationInput.from_item(make_item("Publisher title", description=description))
    return TranslationProviderRequest((TranslationRequestItem("stable-1", content),), "en", "zh")


def adapter(transport, token=lambda: "short-lived-token"):
    return GoogleTranslationAdapter(
        config=GoogleTranslationConfig(project_id="valid-project-123"),
        transport=transport,
        access_token=token,
    )


def test_google_adapter_uses_exact_origin_bearer_and_bounded_v3_body():
    transport = CapturingTransport(
        response({"translations": [{"translatedText": "发布者标题"}, {"translatedText": "摘要"}]})
    )
    result = adapter(transport).translate(request_for())
    args, kwargs = transport.calls[0]
    assert args[:2] == ("google-translation", "POST")
    assert args[2] == (
        "https://translation.googleapis.com/v3/projects/valid-project-123/locations/global:translateText"
    )
    assert kwargs["credential"].origin == GOOGLE_TRANSLATION_ORIGIN
    assert kwargs["credential"].header_name == "Authorization"
    assert kwargs["credential"].value == "Bearer short-lived-token"
    assert kwargs["allowed_mime_types"] == ("application/json",)
    body = json.loads(kwargs["body"])
    assert body == {
        "contents": ["Publisher title", "Summary"],
        "mimeType": "text/plain",
        "sourceLanguageCode": "en",
        "targetLanguageCode": "zh",
        "model": "projects/valid-project-123/locations/global/models/general/nmt",
    }
    assert result.model_version == body["model"]
    assert result.items[0].request_id == "stable-1"
    assert result.items[0].title == "发布者标题"


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"translations": []}, TranslationErrorReason.MALFORMED_RESPONSE),
        ({"translations": [{"translatedText": "only one"}]}, TranslationErrorReason.MALFORMED_RESPONSE),
        (
            {"translations": [{"translatedText": "标题", "detectedLanguageCode": "zh"}, {"translatedText": "摘要"}]},
            TranslationErrorReason.MALFORMED_RESPONSE,
        ),
    ],
)
def test_google_adapter_rejects_cardinality_and_language_mismatch(payload, reason):
    with pytest.raises(TranslationProviderError) as caught:
        adapter(CapturingTransport(response(payload))).translate(request_for())
    assert caught.value.reason is reason


def test_google_adapter_rejects_oversized_batch_before_token_or_transport():
    transport = CapturingTransport(response({"translations": []}))
    token_calls = []
    provider = GoogleTranslationAdapter(
        config=GoogleTranslationConfig(project_id="valid-project-123", max_batch_items=1),
        transport=transport,
        access_token=lambda: token_calls.append(True) or "token",
    )
    first = TranslationInput.from_item(make_item("One", url="https://example.com/1"))
    second = TranslationInput.from_item(make_item("Two", url="https://example.com/2"))
    request = TranslationProviderRequest(
        (TranslationRequestItem("one", first), TranslationRequestItem("two", second)), "en", "zh"
    )
    with pytest.raises(TranslationProviderError) as caught:
        provider.translate(request)
    assert caught.value.reason is TranslationErrorReason.INVALID_REQUEST
    assert token_calls == []
    assert transport.calls == []


def test_google_adapter_sanitizes_transport_token_and_source_failures():
    source_sentinel = "SOURCE-SENTINEL-NEVER-LOG"
    token_sentinel = "TOKEN-SENTINEL-NEVER-LOG"
    item = make_item(source_sentinel)
    request = TranslationProviderRequest(
        (TranslationRequestItem("safe-id", TranslationInput.from_item(item)),), "en", "zh"
    )
    failure = SafeTransportError("contains-" + token_sentinel, SafeTransportReason.CONNECT_FAILED)
    with pytest.raises(TranslationProviderError) as caught:
        adapter(CapturingTransport(failure), token=lambda: token_sentinel).translate(request)
    rendered = str(caught.value) + repr(caught.value.args) + repr(caught.value.__context__)
    assert source_sentinel not in rendered
    assert token_sentinel not in rendered
    assert caught.value.reason is TranslationErrorReason.TRANSPORT_FAILURE


@pytest.mark.parametrize(
    "model",
    (
        "google-nmt-v3",
        "projects/other-project-123/locations/global/models/general/nmt",
        "projects/valid-project-123/locations/us-central1/models/general/nmt",
        "projects/valid-project-123/locations/global/models/../nmt",
        "projects/valid-project-123/locations/global/models/general nmt",
    ),
)
def test_google_config_rejects_unbound_or_malformed_model_resources(model):
    with pytest.raises(ValueError, match="model resource"):
        GoogleTranslationConfig(project_id="valid-project-123", model_version=model)


def test_google_config_accepts_and_sends_a_bounded_custom_model_resource_exactly():
    model = "projects/valid-project-123/locations/global/models/custom-model-42"
    transport = CapturingTransport(
        response({"translations": [{"translatedText": "发布者标题"}, {"translatedText": "摘要"}]})
    )
    provider = GoogleTranslationAdapter(
        config=GoogleTranslationConfig(project_id="valid-project-123", model_version=model),
        transport=transport,
        access_token=lambda: "short-lived-token",
    )
    result = provider.translate(request_for())
    request_body = json.loads(transport.calls[0][1]["body"])
    assert request_body["model"] == model
    assert result.model_version == model
    assert provider.model_version == model
