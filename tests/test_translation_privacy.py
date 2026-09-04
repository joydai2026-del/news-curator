from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from curator.models import Item
from curator.personalization.auth import Session
from curator.translation import TranslationInput, TranslationPrivacyError
from curator.translation.selector import select_translation_candidates


def make_item(title, *, description="", sender=""):
    return Item(
        title=title,
        url="https://example.com/a",
        canonical_url="https://example.com/a",
        source_id="fixture",
        source_name="Fixture",
        published_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        description=description,
        newsletter_sender=sender,
    )


def make_newsletter_item(title, *, sender="", description=""):
    item = make_item(title, description=description, sender=sender)
    item.is_newsletter = True
    return item


def test_newsletters_are_explicitly_rejected_before_selection():
    newsletter = make_newsletter_item(
        "Private newsletter title",
        sender="subscriber-unique@example.test",
        description="Private newsletter blurb",
    )
    with pytest.raises(TranslationPrivacyError, match="newsletter"):
        select_translation_candidates([newsletter], target_language="zh")
    with pytest.raises(TranslationPrivacyError, match="newsletter"):
        TranslationInput.from_item(newsletter)


def test_preferences_cannot_cross_the_translation_input_factory():
    preference = Session(
        access_token="private-access-token",
        refresh_token="private-refresh-token",
        expires_at=1_800_000_000,
        user_id="private-user",
    )
    with pytest.raises(TranslationPrivacyError, match="only Item"):
        TranslationInput.from_item(preference)  # type: ignore[arg-type]
    with pytest.raises(TranslationPrivacyError, match="only Item"):
        select_translation_candidates([preference], target_language="zh")  # type: ignore[list-item]


def test_approved_snapshot_contains_no_url_sender_or_unapproved_fields():
    item = make_item("Public title", description="Public description", sender="private-sender")
    item.newsletter_sender = "private-sender@example.test"
    item.url = "https://example.test/private-token"
    snapshot = TranslationInput.from_item(item)
    serialized = repr(asdict(snapshot))
    assert "private-sender" not in serialized
    assert "example.test" not in serialized
    assert set(asdict(snapshot)) == {"title", "description", "source_language", "digest", "character_count"}
