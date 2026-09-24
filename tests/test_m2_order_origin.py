"""Order provenance stays truthful across current, prepared, and frozen views."""

from dataclasses import replace

from tests.test_m2_phase2_service import CLOCK, PaidStore, build, liked_events, paid, rank


class PreparedStore(PaidStore):
    def __init__(self):
        super().__init__(events=liked_events())
        self.enqueued = None
        self.ready = None

    def enqueue_prepared_order(self, **kwargs):
        self.enqueued = kwargs
        return True

    def consume_prepared_order(self, **kwargs):
        return self.ready


def test_recipe_and_prepared_model_origins_survive_page_turns():
    store = PreparedStore()
    subject = build(store, effective_policy_digest="a" * 64)
    subject._policy = replace(subject._policy, next_run_preparation_enabled=True)
    first = rank(subject, store)
    assert first["result_mode"] == "fallback"
    assert first["order_origin"] == "recipe"
    assert subject.page(authorization="Bearer valid", cursor=first["next_cursor"])["order_origin"] == "recipe"

    source = store.enqueued
    assert source is not None
    store.close_reading_run(user_id=source["user_id"], run_id=source["source_run_id"], closed_at="test")
    store.ready = {
        "owner_id": source["user_id"], "source_run_id": source["source_run_id"],
        "eligibility_key": source["eligibility_key"], "policy_digest": source["policy_digest"],
        "history_generation": source["history_generation"],
        "consent_revision": source["consent_revision"],
        "behavior_revision": source["behavior_revision"],
        "provider_policy_id": source["provider_policy_id"], "status": "ready",
        "expires_at": CLOCK + 3600,
        "ranked_candidate_ids": [item["candidate_id"] for item in reversed(source["request_payload"]["candidates"])],
    }
    second = rank(subject, store)
    assert second["result_mode"] == "model"
    assert second["order_origin"] == "prepared_model"
    assert subject.page(authorization="Bearer valid", cursor=second["next_cursor"])["order_origin"] == "prepared_model"


def test_direct_model_and_legacy_frozen_origin_are_truthful():
    store = PaidStore(events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert first["result_mode"] == "model"
    assert first["order_origin"] == "direct_model"
    assert subject.page(authorization="Bearer valid", cursor=first["next_cursor"])["order_origin"] == "direct_model"
    store.frozen["frozen-1"]["bindings"].pop("order_origin")
    assert subject.page(authorization="Bearer valid", cursor=first["next_cursor"])["order_origin"] == "direct_model"

    legacy = PaidStore(events=liked_events())
    legacy_subject = build(legacy, composition=False)
    legacy_first = rank(legacy_subject, legacy)
    assert legacy_first["order_origin"] == "freshness"

    older = PreparedStore()
    older_subject = build(older, effective_policy_digest="a" * 64)
    older_subject._policy = replace(older_subject._policy, next_run_preparation_enabled=True)
    frozen = rank(older_subject, older)
    older.frozen["frozen-1"]["bindings"].pop("order_origin")
    assert older_subject.page(authorization="Bearer valid", cursor=frozen["next_cursor"])["order_origin"] == "recipe"
