from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from curator.models import Item
from curator.sources import SafeHttpResponse
from curator.translation import (
    GoogleTranslationAdapter,
    GoogleTranslationConfig,
    TranslationInput,
    TranslationProviderError,
    TranslationProviderRequest,
    TranslationRequestItem,
)


def make_item(title):
    return Item(
        title=title,
        url="https://example.com/a",
        canonical_url="https://example.com/a",
        source_id="fixture",
        source_name="Fixture",
        published_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )


class HostileTransport:
    def request(self, *args, **kwargs):
        secret = kwargs["credential"].value
        source = json.loads(kwargs["body"])["contents"][0]
        return SafeHttpResponse(500, args[2], {}, (secret + source).encode())


def test_provider_error_and_captured_streams_never_contain_secret_or_source(capsys):
    token = "UNIQUE-TOKEN-4f7df1"
    source = "UNIQUE-PRIVATE-SOURCE-a8507d"
    content = TranslationInput.from_item(make_item(source))
    request = TranslationProviderRequest((TranslationRequestItem("safe-id", content),), "en", "zh")
    provider = GoogleTranslationAdapter(
        config=GoogleTranslationConfig(project_id="valid-project-123"),
        transport=HostileTransport(),
        access_token=lambda: token,
    )
    with pytest.raises(TranslationProviderError) as caught:
        provider.translate(request)
    streams = capsys.readouterr()
    safe_surface = str(caught.value) + repr(caught.value.args) + streams.out + streams.err
    assert token not in safe_surface
    assert source not in safe_surface
