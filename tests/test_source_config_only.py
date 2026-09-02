"""Configuration-only standard source additions and registry isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from curator.config import load_config
from curator.sources import (
    SourceRegistry,
    SourceSpec,
    SourceValidationError,
    build_builtin_registry,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "row",
    [
        {
            "type": "rss",
            "id": "publisher-rss",
            "name": "Publisher RSS",
            "url": "https://example.com/rss",
        },
        {
            "type": "atom",
            "id": "publisher-atom",
            "name": "Publisher Atom",
            "url": "https://example.com/atom",
        },
        {
            "type": "news_sitemap",
            "id": "publisher-news",
            "name": "Publisher News",
            "url": "https://example.com/news.xml",
        },
        {
            "type": "json_feed",
            "id": "publisher-json",
            "name": "Publisher JSON",
            "url": "https://example.com/feed.json",
        },
    ],
)
def test_standard_source_addition_is_one_config_row(row):
    registry = build_builtin_registry()

    (spec,) = registry.parse_specs([row])

    assert spec.id == row["id"]
    assert registry.adapter_for(spec.type).type_key == row["type"]


def test_canary_media_uses_its_official_rss_endpoint():
    config = load_config(REPO_ROOT)
    source = next(
        row
        for category in config.categories
        for row in category.sources
        if row.id == "canary"
    )

    assert source.url == "https://www.canarymedia.com/rss.rss"
    assert source.options == {"allow_mislabeled_html_mime": True}


def test_source_ids_are_globally_unique_across_adapter_types():
    registry = build_builtin_registry()
    with pytest.raises(SourceValidationError, match="globally unique"):
        registry.parse_specs(
            [
                {
                    "type": "rss",
                    "id": "same",
                    "name": "RSS",
                    "url": "https://example.com/rss",
                },
                {
                    "type": "json_feed",
                    "id": "same",
                    "name": "JSON",
                    "url": "https://example.com/feed.json",
                },
            ]
        )


class PrivateAdapter:
    type_key = "private"

    def validate_options(self, _spec: SourceSpec) -> Mapping[str, Any]:
        return {}

    def fetch(self, spec, context):
        raise AssertionError("not needed")


def test_injected_registry_has_no_mutable_global_registration_leakage():
    first = SourceRegistry((PrivateAdapter(),))
    second = build_builtin_registry()

    assert first.keys == ("private",)
    assert "private" not in second.keys
    with pytest.raises(SourceValidationError, match="not allowlisted"):
        second.adapter_for("private")
    assert not hasattr(first, "register")


def test_adapter_options_cannot_escape_the_options_discriminator():
    registry = build_builtin_registry()
    with pytest.raises(SourceValidationError, match="unknown common fields"):
        registry.parse_spec(
            {
                "type": "json_feed",
                "id": "json",
                "name": "JSON",
                "url": "https://example.com/feed.json",
                "max_json_depth": 3,
            }
        )
