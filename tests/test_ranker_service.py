import pytest
from datetime import datetime, timezone

from curator.contracts.enums import ActorKind
from curator.contracts.ranking_request import AuthenticatedOwner

from curator.recommendation.service import AuthenticationError, RankingService, ServicePolicy, StaleRankingError
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy


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
        policy=ServicePolicy("policy", "model", "provider", "tenant", enabled=enabled,
                             preview_owner_ids=("user",)), cursor_key=b"x" * 32, clock=clock)


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


@pytest.mark.parametrize("summary", ["", "   ", "Observed summary"])
def test_sql_snapshot_shape_preserves_resolved_context_query_and_unsave_semantics(summary):
    subject = service()
    story_id = "story:" + "1" * 64
    snapshot = {"included_history_revision": 7, "history_revision": 9, "history_generation": 2,
        "consent_revision": 3, "learning_enabled": True, "events": [{"event_id": "event-1",
            "event_type": "save", "event_revision": 7, "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"story_id": story_id, "query": "safety", "saved": False},
            "story_title": "Observed title", "story_summary": summary, "source_id": "source"}]}
    rows = [{"story_id": story_id, "title": "Candidate", "summary": "Summary", "source_id": "source",
        "language": "en", "published_at": datetime.now(timezone.utc).isoformat()}]
    built = subject._request("request", AuthenticatedOwner("tenant", "user", "user", ActorKind.HUMAN), snapshot, rows, None)
    event = built.ordered_history[0]
    assert (event.story_id, event.query_text, event.story_title, event.action_value) == (story_id, "safety", "Observed title", False)
    assert event.story_summary == (summary if summary.strip() else None)
    assert (built.history_revision, built.server_commit_revision) == (7, 9)
    model_input = built.model_input()
    assert model_input.ordered_history == built.ordered_history


@pytest.mark.parametrize("field", ["history_revision", "server_commit_revision", "history_generation", "consent_revision"])
def test_rank_rejects_stale_client_revision_bindings(field):
    snapshot = {"included_history_revision": 3, "history_revision": 5,
        "history_generation": 2, "consent_revision": 4}
    body = {"history_revision": 3, "server_commit_revision": 5,
        "history_generation": 2, "consent_revision": 4}
    body[field] += 1
    with pytest.raises(StaleRankingError, match=f"stale_{field}"):
        service()._validate_client_bindings(body, snapshot)


@pytest.mark.parametrize("field", ["history_generation", "consent_revision", "provider_processing_enabled"])
def test_post_provider_consent_or_generation_change_is_rejected(field):
    before = {"history_revision": 5, "included_history_revision": 3, "history_generation": 2,
        "consent_revision": 4, "provider_processing_enabled": True}
    after = dict(before)
    after[field] = 99 if field != "provider_processing_enabled" else False
    with pytest.raises(StaleRankingError, match=f"changed_{field}"):
        service()._assert_fresh(before, after)


@pytest.mark.parametrize("field", ["history_revision", "included_history_revision"])
def test_a_behavior_write_during_the_provider_call_no_longer_discards_the_paid_rank(field):
    """F1. These two move on EVERY behavior event, including a save in another
    tab. Treating them as fatal is what threw away a call that had already been
    paid for; only consent and generation make a paid order actually wrong."""
    before = {"history_revision": 5, "included_history_revision": 3, "history_generation": 2,
        "consent_revision": 4, "provider_processing_enabled": True}
    after = dict(before)
    after[field] += 1
    service()._assert_fresh(before, after)


def test_equal_time_corpus_cursor_has_no_gap_or_duplicate_across_fifty_candidate_boundary():
    published = "2026-09-14T12:00:00+00:00"
    rows = [{"story_id": f"story:{index:064x}", "title": f"Story {index}", "summary": "",
        "source_id": "public", "source_name": "Public", "language": "en", "published_at": published,
        "canonical_url": f"https://example.invalid/{index}", "category_ids": []}
        for index in range(101, 0, -1)]
    class BoundaryStore:
        def __init__(self): self.frozen = {}; self.sequence = 0
        def history_snapshot(self, token):
            return {"included_history_revision": 0, "history_revision": 0, "history_generation": 1,
                "consent_revision": 1, "learning_enabled": False, "provider_processing_enabled": False,
                "provider_policy_id": "policy", "events": []}
        def retained_candidates(self, *, limit, before_published_at=None, before_story_id=None, **kwargs):
            eligible = rows if before_story_id is None else [row for row in rows
                if (row["published_at"], row["story_id"]) < (before_published_at, before_story_id)]
            return eligible[:limit]
        def reserve_budget(self, **kwargs): return False
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs):
            self.sequence += 1; key = f"frozen-{self.sequence}"; self.frozen[key] = kwargs; return key
        def load_frozen_order(self, *, user_id, frozen_order_id):
            value = self.frozen[frozen_order_id]
            return {"expires_at": value["expires_at"], "page_size": value["page_size"],
                "bindings": value["bindings"], "cards": value["cards"]}
    store = BoundaryStore()
    adapter = RankLLMAdapter(policy=RankerPolicy("openai", "gpt-5-mini", "https://provider.invalid",
        "policy", input_cost_per_million_tokens_usd=.25, output_cost_per_million_tokens_usd=2), engine=object())
    subject = RankingService(auth=Auth(), store=store, adapter=adapter,
        policy=ServicePolicy("policy", "gpt-5-mini", "policy", "tenant", candidate_limit=50,
            maximum_page_size=25, enabled=True, preview_owner_ids=("user",)),
        cursor_key=b"x" * 32, clock=lambda: 1000)
    body = {"history_revision": 0, "server_commit_revision": 0, "history_generation": 1,
        "consent_revision": 1, "page_size": 25}
    response = subject.rank(authorization="Bearer valid", body=body)
    seen = []
    while True:
        seen.extend(card["story_id"] for card in response["cards"])
        if response["next_cursor"] is None: break
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
    assert seen == [row["story_id"] for row in rows]
    assert len(seen) == len(set(seen)) == 101
