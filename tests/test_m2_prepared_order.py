from curator.recommendation.prepared_order import select_prepared_order


def ready(**changes):
    row = {
        "owner_id": "owner-a", "source_run_id": "run-old", "eligibility_key": "eligibility-a",
        "policy_digest": "policy-a", "history_generation": 2, "consent_revision": 3,
        "behavior_revision": 7, "provider_policy_id": "provider-a", "status": "ready", "expires_at": 200,
        "ranked_candidate_ids": ["b", "a", "c"],
    }
    row.update(changes)
    return row


def select(row=None, *, candidates=("a", "b", "d"), **changes):
    args = {
        "owner_id": "owner-a", "run_id": "run-new", "eligibility_key": "eligibility-a",
        "policy_digest": "policy-a", "history_generation": 2, "consent_revision": 3,
        "behavior_revision": 7, "provider_policy_id": "provider-a", "provider_processing_enabled": True,
        "candidate_ids": candidates, "minimum_overlap": 2, "now": 150,
    }
    args.update(changes)
    return select_prepared_order(row or ready(), **args)


def test_reuses_only_eligible_intersection_and_leaves_new_stories_in_recipe_order():
    assert select() == ("b", "a", "d")
    # The service-role consume RPC has checked that a later ordinary read or
    # save did not include negative feedback or an unexplained revision gap.
    assert select(behavior_revision=8) == ("b", "a", "d")


def test_never_reorders_current_run_or_crosses_owner_view_policy_or_privacy_epoch():
    for change in (
        {"source_run_id": "run-new"}, {"owner_id": "owner-b"},
        {"eligibility_key": "another"}, {"policy_digest": "another"},
        {"history_generation": 1}, {"consent_revision": 4}, {"behavior_revision": 8},
        {"provider_policy_id": "another"}, {"status": "pending"},
        {"expires_at": 149}, {"ranked_candidate_ids": ["a", "a"]},
    ):
        assert select(ready(**change)) is None
    assert select(provider_processing_enabled=False) is None


def test_rejects_low_overlap_and_invalid_candidate_sets():
    assert select(ready(ranked_candidate_ids=["b", "x", "y"])) is None
    assert select(candidates=("a", "a", "d")) is None
    assert select(ready(ranked_candidate_ids=["b", 2, "a"])) is None


def test_private_request_round_trip_preserves_history_and_owner_contract():
    from datetime import datetime, timezone
    from curator.contracts.enums import ActorKind, M2HistoryEventType
    from curator.contracts.ranking_request import (
        AuthenticatedOwner, OrderedHistoryEvent, RankingCandidate, RankingRequest,
    )
    from curator.recommendation.prepared_order import request_from_payload, request_to_payload

    story = "story:" + "a" * 64
    value = RankingRequest(1, "job-request", AuthenticatedOwner("tenant", "owner-a", "owner-a", ActorKind.HUMAN),
        (RankingCandidate(story, story, "document", "Real title", "Real summary", "source", "en",
            datetime(2026, 9, 24, tzinfo=timezone.utc)),), (story,),
        (OrderedHistoryEvent("event", M2HistoryEventType.SEARCH_QUERY,
            datetime(2026, 9, 24, tzinfo=timezone.utc), 1, query_text="real query"),),
        1, 1, 2, 3, "policy-a", "model-a", "real query")
    assert request_from_payload(request_to_payload(value)) == value
