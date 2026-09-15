from __future__ import annotations

from datetime import datetime, timezone

import pytest

from curator.contracts import (
    ActorKind,
    AuthenticatedOwner,
    EventType,
    EvidenceClass,
    EvidenceOrigin,
    ConfidenceBand,
    LearningEvent,
    M2HistoryEventType,
    OrderedHistoryEvent,
    RankingCandidate,
    RankingRequest,
    RankingResponseReceipt,
    RankingResultMode,
    validate_ranking_request,
    validate_ranking_response,
)


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
STORY_A = "story:" + "a" * 64
STORY_B = "story:" + "b" * 64


def request() -> RankingRequest:
    return RankingRequest(
        schema_version=1,
        request_id="rank-request-1",
        owner=AuthenticatedOwner(
            tenant_id="tenant-1",
            user_id="user-1",
            principal_id="principal-1",
            actor_kind=ActorKind.HUMAN,
        ),
        query="climate policy",
        candidates=(
            RankingCandidate(
                candidate_id=STORY_A,
                canonical_story_id=STORY_A,
                source_document_id="doc-ars-20260909",
                title="Six Chinese AI firms accused of aggressively copying US frontier models",
                summary="US urges AI firms to ID, then secretly switch, Chinese users to less-capable models.",
                source_id="arstechnica",
                language="en",
                published_at=datetime(2026, 9, 9, 20, 6, 28, tzinfo=timezone.utc),
            ),
            RankingCandidate(
                candidate_id=STORY_B,
                canonical_story_id=STORY_B,
                source_document_id="doc-ieee-20260908",
                title="The Growing Proof That Autonomous Cars Save Lives",
                summary="Plenty of people remain spooked by autonomous vehicles, or AVs.",
                source_id="ieee",
                language="en",
                published_at=datetime(2026, 9, 8, 12, 59, 4, tzinfo=timezone.utc),
            ),
        ),
        selected_candidate_registry_ids=(STORY_A, STORY_B),
        ordered_history=(
            OrderedHistoryEvent("event-1", EventType.READ_MORE, NOW, 4, story_id=STORY_A),
            OrderedHistoryEvent("event-2", EventType.READ_MORE, NOW, 5, story_id=STORY_A),
            OrderedHistoryEvent("event-3", M2HistoryEventType.SEARCH_ZERO_RESULTS, NOW, 6, query_text="rare query"),
        ),
        history_revision=6,
        server_commit_revision=6,
        history_generation=1,
        consent_revision=1,
        policy_version="rank-policy-2",
        model_version="configured-model-revision",
    )


def test_request_preserves_optional_query_and_distinct_repeated_history() -> None:
    value = request()
    assert value.query == "climate policy"
    assert [event.event_id for event in value.ordered_history] == ["event-1", "event-2", "event-3"]
    assert value.owner.principal_id not in repr(value.model_input())
    assert value.owner.user_id not in repr(value.model_input())


@pytest.mark.parametrize("event_type", [
    M2HistoryEventType.SEARCH_QUERY,
    M2HistoryEventType.SEARCH_ZERO_RESULTS,
    M2HistoryEventType.OPEN_ORIGINAL,
])
def test_m2_event_vocabulary_covers_missing_history_semantics(event_type: M2HistoryEventType) -> None:
    event = LearningEvent(
        tenant_id="tenant-1",
        actor_id="actor-1",
        actor_kind=ActorKind.HUMAN,
        user_id="user-1",
        event_id="event-m2",
        event_type=event_type,
        occurred_at=NOW,
        recorded_at=NOW,
        surface="web",
        idempotency_key="delivery-1",
        evidence_class=EvidenceClass.EXPLICIT,
        origin=EvidenceOrigin.LIVE,
        confidence=ConfidenceBand.STRONG,
        policy_revision=2,
    )
    assert event.event_type is event_type


def test_request_rejects_noncanonical_or_unregistered_candidate_ids() -> None:
    value = request()
    with pytest.raises(ValueError, match="eligible-candidate registry"):
        validate_ranking_request(
            RankingRequest(**{**value.__dict__, "selected_candidate_registry_ids": (STORY_A,)})
        )


def test_request_rejects_history_out_of_revision_order() -> None:
    value = request()
    with pytest.raises(ValueError, match="strict revision order"):
        validate_ranking_request(
            RankingRequest(**{**value.__dict__, "ordered_history": tuple(reversed(value.ordered_history))})
        )


def test_response_must_be_exact_unique_candidate_permutation() -> None:
    value = request()
    receipt = RankingResponseReceipt(
        schema_version=1,
        request_id=value.request_id,
        policy_version=value.policy_version,
        model_version=value.model_version,
        history_revision=value.history_revision,
        server_commit_revision=value.server_commit_revision,
        history_generation=value.history_generation,
        consent_revision=value.consent_revision,
        newest_event_id="event-3",
        candidate_ids=(STORY_A, STORY_B),
        ranked_candidate_ids=(STORY_B, STORY_A),
        result_mode=RankingResultMode.MODEL,
    )
    validate_ranking_response(receipt, value)
    with pytest.raises(ValueError, match="exact unique permutation"):
        validate_ranking_response(
            RankingResponseReceipt(**{**receipt.__dict__, "ranked_candidate_ids": (STORY_A, STORY_A)}), value
        )


@pytest.mark.parametrize("field,bad", [
    ("schema_version", True),
    ("schema_version", 2),
    ("history_revision", True),
    ("history_revision", 5),
])
def test_request_rejects_invalid_revision_shapes(field: str, bad: object) -> None:
    value = request()
    with pytest.raises(ValueError):
        validate_ranking_request(RankingRequest(**{**value.__dict__, field: bad}))


def test_empty_history_requires_zero_revision_and_model_input_validates() -> None:
    value = request()
    invalid = RankingRequest(**{**value.__dict__, "ordered_history": (), "history_revision": 1})
    with pytest.raises(ValueError, match="empty history"):
        invalid.model_input()


def test_request_rejects_wrong_collection_and_query_types() -> None:
    value = request()
    with pytest.raises(ValueError, match="contract types"):
        validate_ranking_request(
            RankingRequest(**{**value.__dict__, "candidates": list(value.candidates)})  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="contract types"):
        validate_ranking_request(RankingRequest(**{**value.__dict__, "query": 7}))  # type: ignore[arg-type]


def test_response_receipt_is_bound_to_expected_request() -> None:
    value = request()
    receipt = RankingResponseReceipt(
        schema_version=1,
        request_id="wrong-request",
        policy_version=value.policy_version,
        model_version=value.model_version,
        history_revision=value.history_revision,
        server_commit_revision=value.server_commit_revision,
        history_generation=value.history_generation,
        consent_revision=value.consent_revision,
        newest_event_id="event-3",
        candidate_ids=(STORY_A, STORY_B),
        ranked_candidate_ids=(STORY_A, STORY_B),
        result_mode=RankingResultMode.MODEL,
    )
    with pytest.raises(ValueError, match="expected request"):
        validate_ranking_response(receipt, value)


def test_unknown_event_and_response_modes_fail_closed() -> None:
    value = request()
    bad_event = OrderedHistoryEvent("event-x", "unknown", NOW, 7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="history event fields"):
        validate_ranking_request(
            RankingRequest(**{**value.__dict__, "ordered_history": value.ordered_history + (bad_event,),
                              "history_revision": 7, "server_commit_revision": 7})
        )
    receipt = RankingResponseReceipt(
        schema_version=1,
        request_id=value.request_id,
        policy_version=value.policy_version,
        model_version=value.model_version,
        history_revision=value.history_revision,
        server_commit_revision=value.server_commit_revision,
        history_generation=value.history_generation,
        consent_revision=value.consent_revision,
        newest_event_id="event-3",
        candidate_ids=(STORY_A, STORY_B),
        ranked_candidate_ids=(STORY_A, STORY_B),
        result_mode="model",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="supported enum"):
        validate_ranking_response(receipt, value)
