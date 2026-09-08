from __future__ import annotations

import json
import ast
from pathlib import Path

import pytest

from curator.dashboard import DashboardClient, _validate_card
from curator.personalization import AuthConfig, AuthError, Session


CONFIG = AuthConfig("https://example.supabase.co", "sb_publishable_test")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return self.responses.pop(0)


def session() -> Session:
    return Session("private-access", "private-refresh", 9999999999, "user-a")


def summary() -> dict:
    return {
        "schema_version": 1,
        "scope": "current_retained_state",
        "snapshot_at": "2026-09-08T12:00:00+00:00",
        "saved_count": 3,
        "saved_unread_count": 2,
        "read_count": 1,
        "active_interest_signal_count": 1,
        "topic_signals": [{"topic_id": "ai", "more_like_count": 1, "less_like_count": 0}],
    }


def preference() -> dict:
    return {
        "user_id": "user-a",
        "revision": 2,
        "locale": "en",
        "interests": ["agents"],
        "saved_searches": [],
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-08T11:00:00Z",
    }


def saved(story_number: int) -> dict:
    source = Path("tests/test_auth_callback_playwright.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_feed_story")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<saved-card-fixture>", "exec"), namespace)
    _feed_story = namespace["_feed_story"]
    card = _feed_story(story_number, f"Stored story {story_number}")
    card.update({
        "page_order_mode": "saved_at",
        "next_cursor": {
            "before_saved_at": "2026-09-08T11:00:00Z",
            "before_story_id": card["story_id"],
        },
        "saved_at": "2026-09-08T11:00:00Z",
        "state_revision": 1,
    })
    return card


def test_saved_validator_accepts_the_existing_reader_fixture_shape_and_no_extra_fields() -> None:
    card = saved(1)
    assert _validate_card(card) == card
    with pytest.raises(AuthError, match="saved response was invalid"):
        _validate_card({**card, "user_id": "user-a"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic_ids", [{}]),
        ("topic_ranks", {"ai": True}),
        ("coverage_mentions", [{"access_token": "not-a-coverage-row"}]),
        ("source_name", ""),
        ("ranking_explanation", ""),
    ],
)
def test_saved_validator_rejects_malformed_nested_values_cleanly(field, value) -> None:
    card = saved(1)
    card[field] = value
    with pytest.raises(AuthError, match="saved response was invalid"):
        _validate_card(card)


def responses(*saved_pages):
    return [
        (200, summary()),
        (200, [preference()]),
        (200, {"page_size": 2}),
        *((200, page) for page in saved_pages),
    ]


def test_snapshot_is_explicitly_partial_and_contains_no_identity_or_tokens() -> None:
    transport = FakeTransport(responses([saved(1), saved(2)]))
    snapshot = DashboardClient(CONFIG, transport=transport, clock=lambda: "2026-09-08T12:01:00Z").snapshot(
        session(), saved_pages=1
    )
    assert snapshot["kind"] == "loaded_dashboard_snapshot"
    assert snapshot["saved"] == {
        "loaded_count": 2,
        "displayed_count": 2,
        "page_size": 2,
        "all_saved_loaded": False,
        "next_cursor": saved(2)["next_cursor"],
        "items": [saved(1), saved(2)],
    }
    assert set(snapshot["preferences"]) == {
        "revision", "locale", "interests", "saved_searches", "created_at", "updated_at"
    }
    rendered = json.dumps(snapshot)
    for forbidden in ("private-access", "private-refresh", "user-a", "publishable"):
        assert forbidden not in rendered


def test_snapshot_marks_saved_complete_only_after_short_page() -> None:
    transport = FakeTransport(responses([saved(1), saved(2)], [saved(3)]))
    snapshot = DashboardClient(CONFIG, transport=transport, clock=lambda: "2026-09-08T12:01:00Z").snapshot(
        session(), saved_pages=2
    )
    assert snapshot["saved"]["loaded_count"] == 3
    assert snapshot["saved"]["all_saved_loaded"] is True
    assert snapshot["saved"]["next_cursor"] is None


def test_summary_unknown_private_or_invalid_fields_fail_closed() -> None:
    for mutation in (
        {"schema_version": True},
        {"user_id": "user-a"},
        {"access_token": "private-access"},
        {"read_count": -1},
        {"read_count": 9_007_199_254_740_992},
        {"topic_signals": [{"topic_id": "ai", "more_like_count": 1, "less_like_count": 0},
                            {"topic_id": "ai", "more_like_count": 0, "less_like_count": 1}]},
    ):
        payload = summary()
        payload.update(mutation)
        transport = FakeTransport([(200, payload)])
        with pytest.raises(AuthError, match="dashboard response was invalid"):
            DashboardClient(CONFIG, transport=transport).summary(session())


def test_requests_use_bearer_and_public_key_without_printing_them(capsys) -> None:
    transport = FakeTransport([(200, summary())])
    DashboardClient(CONFIG, transport=transport).summary(session())
    call = transport.calls[0]
    assert call["headers"]["authorization"] == "Bearer private-access"
    assert call["headers"]["apikey"] == "sb_publishable_test"
    assert call["url"].endswith("/rest/v1/rpc/dashboard_summary")
    assert "private-access" not in capsys.readouterr().out


def test_cli_emits_only_projected_snapshot_and_reuses_existing_session(monkeypatch, capsys) -> None:
    import scripts.dashboard_cli as cli

    class FakeAuth:
        def __init__(self, _config, _storage):
            pass

        def valid_session(self):
            return session()

    class FakeClient:
        def __init__(self, _config):
            pass

        def snapshot(self, current, *, saved_pages):
            assert current.user_id == "user-a"
            assert saved_pages == 2
            return {"schema_version": 1, "kind": "loaded_dashboard_snapshot"}

    monkeypatch.setattr(cli, "AgentAuth", FakeAuth)
    monkeypatch.setattr(cli, "DashboardClient", FakeClient)
    monkeypatch.setattr(cli, "MacOSKeychainStorage", lambda account: object())
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    assert cli.main(["snapshot", "--saved-pages", "2"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"schema_version": 1, "kind": "loaded_dashboard_snapshot"}
    assert "private-access" not in output
    assert "private-refresh" not in output
    assert "user-a" not in output
