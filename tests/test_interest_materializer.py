"""The unattended preference read stays bounded and server-side."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from curator.personalization.materializer import (
    MaterializationError,
    SecretPreferenceConfig,
    fetch_interest_profile,
)
from curator.personalization.ranking import InterestProfile
from curator.config import Category, Config
from curator.identity import story_id_for_item
from curator.models import TierResult
from curator.pipeline import NEWSLETTER_CATEGORY_NAME, build, load_newsletter_artifact
from scripts import build_interest_ranking


OWNER_ID = "11111111-1111-4111-8111-111111111111"
SECRET = "sb_secret_test-only-value"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


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
        if isinstance(self.response, list) and self.response and isinstance(self.response[0], tuple):
            return self.response.pop(0)
        return self.response


def profile_responses(revision=4, interests=None, adjustments=None, weight=0.8, signal_revision=0):
    return [
        (200, [{"revision": revision, "interests": interests or []}]),
        (200, {
            "revision": signal_revision,
            "topic_adjustments": adjustments or [],
            "more_like_topic_weight": weight,
            "topic_signal_limit": 100,
        }),
    ]


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


def test_newsletter_topic_signal_is_scored_before_the_render_build(tmp_path, monkeypatch) -> None:
    cfg = Config(
        categories=[Category(name="AI", id="ai", keywords=["AI"])],
        rss=[],
        settings={"max_age_hours": 48},
        ranking={"weight_interest": 1.0},
        dedup={},
        hackernews={},
        reddit={},
        images={"enabled": False},
    )
    snapshot = SimpleNamespace(results=(TierResult("sources", [], True),), content_digest="a" * 64)
    newsletter_path = tmp_path / "newsletter.json"
    newsletter_path.write_text(json.dumps({
        "version": 1,
        "ok": True,
        "dark": False,
        "reason": "ok",
        "note": "",
        "unmatched_messages": 0,
        "watermark": NOW.isoformat(),
        "hashes": ["b" * 64],
        "status": {},
        "items": [
            {
                "title": "Older quantum dispatch",
                "url": "https://publisher.example/older",
                "canonical_url": "https://publisher.example/older",
                "source_id": "newsletter:test",
                "source_name": "Test Newsletter",
                "platform": "newsletter:test",
                "published_at": (NOW - timedelta(hours=4)).isoformat(),
                "description": "A public-safe newsletter summary.",
                "newsletter_sender": "Test Newsletter",
            },
            {
                "title": "Newer energy brief",
                "url": "https://publisher.example/newer",
                "canonical_url": "https://publisher.example/newer",
                "source_id": "newsletter:test",
                "source_name": "Test Newsletter",
                "platform": "newsletter:test",
                "published_at": (NOW - timedelta(hours=1)).isoformat(),
                "description": "Another public-safe newsletter summary.",
                "newsletter_sender": "Test Newsletter",
            },
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(build_interest_ranking, "_snapshot", lambda *_args: (snapshot, "c" * 64))
    monkeypatch.setattr(build_interest_ranking, "load_config", lambda _root: cfg)
    monkeypatch.setattr(
        build_interest_ranking,
        "fetch_interest_profile",
        lambda _config: InterestProfile(
            revision=2,
            interests=(),
            topic_adjustments=(("newsletters", 1.0),),
            more_like_topic_weight=0.8,
        ),
    )
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_SECRET_KEY", SECRET)
    monkeypatch.setenv("NEWS_CURATOR_OWNER_USER_ID", OWNER_ID)
    output = tmp_path / "interest-ranking.json"

    assert build_interest_ranking.main([
        "build", "--root", str(tmp_path), "--source-snapshot", str(tmp_path / "snapshot.json"),
        "--newsletter-artifact", str(newsletter_path), "--output", str(output),
    ]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    newsletter_items, newsletter_tier, _meta = load_newsletter_artifact(newsletter_path)
    assert payload["scores"] == {
        story_id_for_item(item): 0.8 for item in sorted(newsletter_items, key=story_id_for_item)
    }
    assert "quantum dispatch" not in output.read_text(encoding="utf-8")
    ranked = build(
        cfg, [newsletter_tier], NOW, newsletter_on=True, interest_scores=payload["scores"]
    )
    assert [item.title for item in ranked[NEWSLETTER_CATEGORY_NAME]] == [
        "Newer energy brief", "Older quantum dispatch",
    ]
    assert all(
        item.ranking_mode_by_topic[NEWSLETTER_CATEGORY_NAME] == "preference_then_freshness"
        and item.score_components_by_topic[NEWSLETTER_CATEGORY_NAME]["interest"] == 0.8
        for item in ranked[NEWSLETTER_CATEGORY_NAME]
    )
    validate_args = [
        "validate", "--root", str(tmp_path),
        "--source-snapshot", str(tmp_path / "snapshot.json"),
        "--newsletter-artifact", str(newsletter_path), "--input", str(output),
    ]
    assert build_interest_ranking.main(validate_args) == 0
    changed = json.loads(newsletter_path.read_text(encoding="utf-8"))
    changed["items"][0]["title"] = "Changed after score materialization"
    newsletter_path.write_text(json.dumps(changed), encoding="utf-8")
    assert build_interest_ranking.main(validate_args) == 2


def test_fetches_only_the_configured_owner_and_returns_a_valid_profile() -> None:
    transport = FakeTransport(
        profile_responses(interests=["AI agents"])
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
    signal_call = transport.calls[1]
    assert signal_call["method"] == "POST"
    assert signal_call["url"].endswith("/rest/v1/rpc/materialize_user_interest_signals")
    assert signal_call["body"] == {"p_user_id": OWNER_ID}
    assert "user_story_interests" not in signal_call["url"]


def test_more_than_200_historical_rows_are_represented_by_bounded_topic_aggregation() -> None:
    transport = FakeTransport(profile_responses(
        revision=4,
        adjustments=[
            {"topic_id": "ai", "adjustment": 201},
            {"topic_id": "energy", "adjustment": -4},
        ],
        signal_revision=205,
    ))
    profile = fetch_interest_profile(
        SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID),
        transport=transport,
    )

    assert profile.revision == 205
    assert profile.topic_adjustments == (("ai", 201.0), ("energy", -4.0))
    assert len(transport.calls) == 2


def test_legacy_service_role_jwt_uses_bearer_compatibility_header() -> None:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    legacy = f"{encode({'alg': 'HS256'})}.{encode({'role': 'service_role'})}.signature"
    transport = FakeTransport(
        profile_responses(revision=1, interests=["AI"])
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
        transport=FakeTransport(profile_responses(revision=3)),
    )

    assert profile == InterestProfile(revision=3, interests=(), more_like_topic_weight=0.8)


def test_topic_signal_and_policy_reads_fail_closed() -> None:
    config = SecretPreferenceConfig("https://example.supabase.co", SECRET, OWNER_ID)
    responses = profile_responses()
    responses[1] = (500, {"private": "body"})

    with pytest.raises(MaterializationError):
        fetch_interest_profile(config, transport=FakeTransport(responses))


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
