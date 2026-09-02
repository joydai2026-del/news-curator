from dataclasses import replace

import pytest

from curator.translation import TranslationCacheKey


def key() -> TranslationCacheKey:
    return TranslationCacheKey(
        story_id="story:abc123",
        input_digest="a" * 64,
        field_selection=("title", "description"),
        normalization_version="normalize-v3",
        source_locale="en",
        target_locale="zh",
        provider="google",
        model_version="nmt-v3",
        glossary_policy_version="glossary-v1",
        candidate_policy_version="candidate-v2",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("story_id", "story:different"),
        ("input_digest", "b" * 64),
        ("field_selection", ("title",)),
        ("normalization_version", "normalize-v4"),
        ("source_locale", "en-US"),
        ("target_locale", "zh-Hant"),
        ("provider", "azure"),
        ("model_version", "nmt-v4"),
        ("glossary_policy_version", "glossary-v2"),
        ("candidate_policy_version", "candidate-v3"),
    ),
)
def test_every_versioned_key_component_changes_digest(field, value) -> None:
    original = key()
    assert replace(original, **{field: value}).digest != original.digest


def test_key_digest_is_stable_and_has_no_source_text() -> None:
    original = key()
    assert original.digest == key().digest
    assert len(original.digest) == 64
    assert set(original.digest) <= set("0123456789abcdef")
    assert "headline" not in repr(original.as_dict()).lower()


def test_cache_key_preserves_exact_provider_model_resource() -> None:
    model = "projects/valid-project-123/locations/global/models/general/nmt"
    resource_key = replace(key(), model_version=model)
    assert resource_key.model_version == model
    assert resource_key.as_dict()["model_version"] == model


@pytest.mark.parametrize(
    "mutation",
    (
        {"input_digest": "bad"},
        {"field_selection": ("description",)},
        {"source_locale": "en", "target_locale": "en"},
        {"provider": "bad provider"},
    ),
)
def test_invalid_or_incomplete_keys_fail_closed(mutation) -> None:
    with pytest.raises(ValueError):
        replace(key(), **mutation)
