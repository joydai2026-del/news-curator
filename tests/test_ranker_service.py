import pytest
from datetime import datetime, timezone

from curator.contracts.enums import ActorKind
from curator.contracts.ranking_request import AuthenticatedOwner

from curator.recommendation.service import AuthenticationError, RankingService, ServicePolicy, StaleRankingError


class Auth:
    def get_user(self, token):
        assert token == "valid"
        return {"id": "user", "app_metadata": {}}


class Store:
    def owner_states(self, access_token, story_ids):
        return {}
    def load_frozen_order(self, *, user_id, frozen_order_id):
        assert user_id == "user"
        if frozen_order_id != "frozen":
            return None
        return {"expires_at": 1100, "page_size": 2,
            "bindings": {"request_id": "request", "result_mode": "model", "fallback_reason": "",
                "history_generation": 1, "consent_revision": 1, "server_commit_revision": 2},
            "cards": [{"story_id": "a"}, {"story_id": "b"}, {"story_id": "c"}]}

    def history_snapshot(self, token):
        return {"history_generation": 1, "consent_revision": 1, "history_revision": 2,
            "learning_enabled": True, "provider_processing_enabled": True}


def service(enabled=True, clock=lambda: 1000):
    return RankingService(auth=Auth(), store=Store(), adapter=object(),
        policy=ServicePolicy("policy", "model", "provider", "tenant", enabled=enabled), cursor_key=b"x" * 32, clock=clock)


def test_disabled_service_fails_before_authentication():
    with pytest.raises(RuntimeError, match="ranking_disabled"):
        service(enabled=False).rank(authorization="Bearer invalid", body={})


def test_bearer_token_is_required():
    with pytest.raises(AuthenticationError):
        service().page(authorization="", cursor="bad")


def test_signed_cursor_pages_a_frozen_order_without_owner_input():
    subject = service()
    cursor = subject._cursor("frozen", 0, 1100)
    first = subject.page(authorization="Bearer valid", cursor=cursor)
    assert [card["story_id"] for card in first["cards"]] == ["a", "b"]
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    assert [card["story_id"] for card in second["cards"]] == ["c"]
    assert second["next_cursor"] is None


def test_cursor_tampering_and_expiry_fail_closed():
    subject = service()
    valid = subject._cursor("frozen", 0, 1100)
    with pytest.raises(StaleRankingError, match="invalid_cursor"):
        subject.page(authorization="Bearer valid", cursor=valid[:-1] + ("A" if valid[-1] != "A" else "B"))
    expired = service(clock=lambda: 1200)
    with pytest.raises(StaleRankingError, match="invalid_cursor"):
        expired.page(authorization="Bearer valid", cursor=valid)


def test_sql_snapshot_shape_preserves_resolved_context_query_and_unsave_semantics():
    subject = service()
    story_id = "story:" + "1" * 64
    snapshot = {"included_history_revision": 7, "history_revision": 9, "history_generation": 2,
        "consent_revision": 3, "learning_enabled": True, "events": [{"event_id": "event-1",
            "event_type": "save", "event_revision": 7, "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"story_id": story_id, "query": "safety", "saved": False},
            "story_title": "Observed title", "story_summary": "Observed summary", "source_id": "source"}]}
    rows = [{"story_id": story_id, "title": "Candidate", "summary": "Summary", "source_id": "source",
        "language": "en", "published_at": datetime.now(timezone.utc).isoformat()}]
    built = subject._request("request", AuthenticatedOwner("tenant", "user", "user", ActorKind.HUMAN), snapshot, rows, None)
    event = built.ordered_history[0]
    assert (event.story_id, event.query_text, event.story_title, event.action_value) == (story_id, "safety", "Observed title", False)
    assert (built.history_revision, built.server_commit_revision) == (7, 9)


@pytest.mark.parametrize("field", ["history_revision", "server_commit_revision", "history_generation", "consent_revision"])
def test_rank_rejects_stale_client_revision_bindings(field):
    snapshot = {"included_history_revision": 3, "history_revision": 5,
        "history_generation": 2, "consent_revision": 4}
    body = {"history_revision": 3, "server_commit_revision": 5,
        "history_generation": 2, "consent_revision": 4}
    body[field] += 1
    with pytest.raises(StaleRankingError, match=f"stale_{field}"):
        service()._validate_client_bindings(body, snapshot)


def test_post_provider_snapshot_change_is_rejected():
    before = {"history_revision": 5, "included_history_revision": 3, "history_generation": 2,
        "consent_revision": 4, "provider_processing_enabled": True}
    after = dict(before, history_revision=6)
    with pytest.raises(StaleRankingError, match="changed_history_revision"):
        service()._assert_fresh(before, after)
