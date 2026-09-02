from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from curator.models import Item
from curator.translation import (
    TranslationInput,
    TranslationPrivacyError,
    TranslationProviderRegistry,
    TranslationProviderRequest,
    TranslationRequestItem,
)
from curator.translation.selector import TranslationCandidatePolicy, select_translation_candidates


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


class StubProvider:
    provider_id = "stub"

    def translate(self, request):  # pragma: no cover - registry contract only
        raise AssertionError("not called")


def test_translation_input_can_only_be_created_from_an_item():
    with pytest.raises(TranslationPrivacyError):
        TranslationInput(_token=object(), title="private", description="", source_language="en")
    item = make_item("Publisher title", description="Publisher summary")
    approved = TranslationInput.from_item(item)
    assert approved.title == "Publisher title"
    assert approved.description == "Publisher summary"
    assert approved.character_count == len("Publisher titlePublisher summary")
    with pytest.raises(FrozenInstanceError):
        approved.title = "changed"


@pytest.mark.parametrize("field,value", [("title", "<b>not normalized</b>"), ("description", "  extra space  ")])
def test_translation_input_requires_pre_normalized_item_fields(field, value):
    item = make_item("Title", description="Summary")
    setattr(item, field, value)
    with pytest.raises(TranslationPrivacyError):
        TranslationInput.from_item(item)


def test_selector_is_bounded_stable_and_cross_language_only():
    items = [
        make_item("First", url="https://example.com/1"),
        make_item("First", url="https://example.com/duplicate"),
        make_item("Already Chinese", url="https://example.com/zh"),
        make_item("Second", url="https://example.com/2"),
    ]
    items[2].language = "zh"
    selected = select_translation_candidates(
        items,
        target_language="zh",
        policy=TranslationCandidatePolicy(max_items=2, max_characters=100),
    )
    assert [entry.content.title for entry in selected] == ["First", "Second"]
    assert selected[0].request_id == "t-" + selected[0].content.digest[:32]


def test_provider_request_validates_ids_languages_and_cardinality():
    content = TranslationInput.from_item(make_item("Title"))
    entry = TranslationRequestItem("request-1", content)
    request = TranslationProviderRequest((entry,), "en", "zh")
    assert request.items == (entry,)
    with pytest.raises(ValueError):
        TranslationProviderRequest((entry, entry), "en", "zh")
    with pytest.raises(ValueError):
        TranslationProviderRequest((entry,), "en", "en")


def test_registries_are_injected_and_do_not_share_state():
    first = TranslationProviderRegistry()
    second = TranslationProviderRegistry()
    first.register("stub", StubProvider())
    assert first.provider_ids == ("stub",)
    assert second.provider_ids == ()
    with pytest.raises(ValueError, match="unknown translation provider"):
        second.get("stub")
