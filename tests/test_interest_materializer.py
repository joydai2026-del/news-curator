"""The unattended preference read stays bounded and server-side."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from curator.personalization.materializer import (
    MaterializationError,
    SecretPreferenceConfig,
    fetch_interest_profile,
)
from curator.personalization.ranking import InterestProfile
from scripts import build_interest_ranking


OWNER_ID = "11111111-1111-4111-8111-111111111111"
SECRET = "sb_secret_test-only-value"


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            }
        )
        return self.response


def test_build_script_uses_the_configured_snapshot_lifetime(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(source_snapshot_max_age_seconds=321)
    snapshot = object()
    seen = {}
    monkeypatch.setattr(build_interest_ranking, "load_config", lambda _root: config)
    monkeypatch.setattr(
        build_interest_ranking,
        "snapshot_config_digest",
        lambda _config: "a" * 64,
    )
    monkeypatch.setattr(
        build_interest_ranking,
        "ranking_config_digest",
        lambda _config: "b" * 64,
    )

    def load_snapshot(path, **kwargs):
        seen.update(kwargs)
        return snapshot

    monkeypatch.setattr(build_interest_ranking, "load_source_snapshot", load_snapshot)

    result, _digest = build_interest_ranking._snapshot(tmp_path, tmp_path / "snapshot.json")

    assert result is snapshot
    assert seen["max_age_seconds"] == 321


def test_fetches_only_the_configured_owner_and_returns_a_valid_profile() -> None:
    transport = FakeTransport(
        (200, [{"revision": 4, "interests": ["AI agents"]}])
    )
    config = SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID)

    profile = fetch_interest_profile(config, transport=transport)

    assert profile.revision == 4
    assert profile.interests == ("AI agents",)
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert "select=revision%2Cinterests" in call["url"]
    assert f"user_id=eq.{OWNER_ID}" in call["url"]
    assert "limit=2" in call["url"]
    assert call["headers"]["apikey"] == SECRET
    assert "authorization" not in call["headers"]
    assert SECRET not in repr(config)


def test_legacy_service_role_jwt_uses_bearer_compatibility_header() -> None:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    legacy = f"{encode({'alg': 'HS256'})}.{encode({'role': 'service_role'})}.signature"
    transport = FakeTransport(
        (200, [{"revision": 1, "interests": ["AI"]}])
    )

    fetch_interest_profile(
        SecretPreferenceConfig("https://example.supabase.co", legacy, OWNER_ID),
        transport=transport,
    )

    assert transport.calls[0]["headers"]["authorization"] == f"Bearer {legacy}"


@pytest.mark.parametrize(
    "response",
    [
        (401, None),
        (200, []),
        (200, [{"revision": 0, "interests": ["AI"]}] * 2),
        (200, [{"revision": -1, "interests": ["AI"]}]),
        (200, [{"revision": 0, "interests": [" AI "]}]),
        (200, [{"revision": 0, "interests": ["AI"], "user_id": OWNER_ID}]),
    ],
)
def test_missing_or_invalid_profile_blocks_materialization(response) -> None:
    config = SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID)
    with pytest.raises(MaterializationError):
        fetch_interest_profile(config, transport=FakeTransport(response))


def test_empty_interests_are_a_valid_opt_out() -> None:
    config = SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID)

    profile = fetch_interest_profile(
        config,
        transport=FakeTransport((200, [{"revision": 3, "interests": []}])),
    )

    assert profile == InterestProfile(revision=3, interests=())


@pytest.mark.parametrize(
    ("url", "secret", "owner"),
    [
        ("http://example.supabase.co", SECRET, OWNER_ID),
        ("https://example.supabase.co/path", SECRET, OWNER_ID),
        ("https://example.supabase.co", "sb_publishable_public", OWNER_ID),
        ("https://example.supabase.co", SECRET, "not-a-uuid"),
    ],
)
def test_configuration_rejects_unsafe_values(url, secret, owner) -> None:
    with pytest.raises(ValueError):
        SecretPreferenceConfig(url, secret, owner)


def test_profile_error_never_includes_secret_or_response_body() -> None:
    marker = "private-profile-marker"
    config = SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID)
    transport = FakeTransport((200, [{"revision": 0, "interests": [marker], "extra": {"bad": True}}]))

    with pytest.raises(MaterializationError) as caught:
        fetch_interest_profile(config, transport=transport)

    rendered = str(caught.value) + repr(caught.value)
    assert SECRET not in rendered
    assert marker not in rendered
    assert json.dumps(transport.response) not in rendered
