"""Order provenance stays truthful across current, prepared, and frozen views."""

from dataclasses import replace
import json

import pytest

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
    recipe_ids = [card["story_id"] for card in first["cards"]]
    served_ids = [card["story_id"] for card in second["cards"]]
    assert served_ids != recipe_ids
    # The diversity pass may move later cards, but the leading prepared
    # permutation must reach the visible page, not only its provenance label.
    assert served_ids[:3] == store.ready["ranked_candidate_ids"][:3]
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
    legacy.frozen["frozen-1"]["bindings"].pop("order_origin", None)
    assert legacy_subject.page(authorization="Bearer valid",
        cursor=legacy_first["next_cursor"])["order_origin"] == "freshness"

    older = PreparedStore()
    older_subject = build(older, effective_policy_digest="a" * 64)
    older_subject._policy = replace(older_subject._policy, next_run_preparation_enabled=True)
    frozen = rank(older_subject, older)
    older.frozen["frozen-1"]["bindings"].pop("order_origin")
    assert older_subject.page(authorization="Bearer valid", cursor=frozen["next_cursor"])["order_origin"] == "recipe"


@pytest.mark.parametrize("failure", ("returned_false", "raised"))
def test_enqueue_failure_preserves_the_first_page_and_logs_only_fixed_fields(failure, capsys):
    baseline_store = PreparedStore()
    baseline_store.rows[0]["title"] = "private owner story title"
    baseline_service = build(baseline_store, effective_policy_digest="a" * 64)
    baseline_service._policy = replace(baseline_service._policy,
        next_run_preparation_enabled=True)
    expected = rank(baseline_service, baseline_store)
    capsys.readouterr()

    class FailingStore(PreparedStore):
        def enqueue_prepared_order(self, **kwargs):
            self.enqueued = kwargs
            if failure == "raised":
                raise RuntimeError("private search text " + str(kwargs["request_payload"]))
            return False

    store = FailingStore()
    store.rows[0]["title"] = "private owner story title"
    service = build(store, effective_policy_digest="a" * 64)
    service._policy = replace(service._policy, next_run_preparation_enabled=True)
    response = rank(service, store)

    assert response["cards"] == expected["cards"]
    assert response["result_mode"] == expected["result_mode"] == "fallback"
    assert response["order_origin"] == expected["order_origin"] == "recipe"
    assert response["next_cursor"] and len(store.frozen) == 1
    assert next(iter(store.views.values()))["pages_served"] == 1
    assert response["cards"] == service.page(authorization="Bearer valid",
        cursor=service._cursor("frozen-1", 0, int(store.frozen["frozen-1"]["expires_at"]),
                               response_number=1))["cards"]

    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    preparation = [line for line in lines if line["event"].startswith("m2_preparation_")]
    timings = [line for line in preparation if line["event"] == "m2_preparation_stage_timing"]
    assert {line["stage"] for line in timings} == {"consume", "enqueue"}
    assert all(set(line) == {"event", "stage", "duration_ms"}
               and isinstance(line["duration_ms"], (int, float))
               and line["duration_ms"] >= 0 for line in timings)
    if failure == "returned_false":
        assert [line for line in preparation if line["event"] == "m2_preparation_enqueue_rejected"] == [
            {"event": "m2_preparation_enqueue_rejected"}]
    else:
        failures = [line for line in preparation if line["event"] == "m2_preparation_enqueue_failed"]
        assert len(failures) == 1
        assert set(failures[0]) - {"frame"} == {"event", "exception_class", "detail"}
        assert failures[0]["exception_class"] == "RuntimeError"
        assert failures[0]["detail"] == "suppressed"
    serialized = json.dumps(preparation)
    for private in ("private search text", "private owner story title",
                    "11111111-1111-1111-1111-111111111111", "story:"):
        assert private not in serialized


def test_exhausted_run_response_reports_recipe_origin():
    store = PreparedStore()
    subject = build(store, effective_policy_digest="a" * 64)
    subject._policy = replace(subject._policy, next_run_preparation_enabled=True)
    first = rank(subject, store)
    assert first["order_origin"] == "recipe"
    # A bound order that expired while its run remains open cannot be ranked
    # again. The empty end response has no model order to claim.
    store.frozen["frozen-1"]["expires_at"] = CLOCK - 1
    exhausted = rank(subject, store)
    assert exhausted["cards"] == [] and exhausted["end_of_run"] is True
    assert exhausted["fallback_reason"] == "run_page_budget_exhausted"
    assert exhausted["result_mode"] == "fallback"
    assert exhausted["order_origin"] == "recipe"
