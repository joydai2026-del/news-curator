"""Fail-closed selection of a model order prepared during an earlier reading run."""

from __future__ import annotations

from typing import Mapping, Sequence


def select_prepared_order(
    prepared: Mapping[str, object] | None, *, owner_id: str, run_id: str,
    eligibility_key: str, policy_digest: str, history_generation: int,
    consent_revision: int, behavior_revision: int, provider_policy_id: str,
    provider_processing_enabled: bool, candidate_ids: Sequence[str],
    minimum_overlap: int, now: float,
) -> tuple[str, ...] | None:
    """Apply only the still-eligible part; never admit a stale or foreign story.

    The caller must first build the current owner-filtered four-lane window.
    Previously ranked ids are only a preference order over that window.
    Stories not in the earlier model result keep their current recipe order.
    The service-role consume RPC has already proved that any newer behavior
    events are complete and contain no negative feedback or unsave.
    """
    if not provider_processing_enabled or not isinstance(prepared, Mapping):
        return None
    expected = {
        "owner_id": owner_id,
        "eligibility_key": eligibility_key,
        "policy_digest": policy_digest,
        "history_generation": history_generation,
        "consent_revision": consent_revision,
        "provider_policy_id": provider_policy_id,
        "status": "ready",
    }
    if any(prepared.get(key) != value for key, value in expected.items()):
        return None
    prepared_revision = prepared.get("behavior_revision")
    if (type(prepared_revision) is not int or type(behavior_revision) is not int
            or prepared_revision < 0 or prepared_revision > behavior_revision):
        return None
    if not isinstance(prepared.get("source_run_id"), str) or prepared["source_run_id"] == run_id:
        return None
    expires_at = prepared.get("expires_at")
    if type(expires_at) not in (int, float) or expires_at <= now:
        return None
    ranked = prepared.get("ranked_candidate_ids")
    if not isinstance(ranked, list) or not all(isinstance(value, str) for value in ranked):
        return None
    if len(ranked) != len(set(ranked)) or not all(isinstance(value, str) for value in candidate_ids):
        return None
    if len(candidate_ids) != len(set(candidate_ids)) or type(minimum_overlap) is not int or minimum_overlap < 1:
        return None
    current = set(candidate_ids)
    shared = tuple(value for value in ranked if value in current)
    if len(shared) < minimum_overlap:
        return None
    seen = set(shared)
    return shared + tuple(value for value in candidate_ids if value not in seen)


def request_to_payload(request):
    """Store the private, validated provider input in service-role-only storage."""
    from dataclasses import asdict
    from datetime import datetime
    from curator.contracts.ranking_request import validate_ranking_request
    import json

    validate_ranking_request(request)

    def encoded(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError("unsupported private request field")

    return json.loads(json.dumps(asdict(request), default=encoded, separators=(",", ":")))


def request_from_payload(payload):
    """Revalidate persisted input before a background provider attempt."""
    from datetime import datetime
    from curator.contracts.enums import ActorKind, EventType, M2HistoryEventType
    from curator.contracts.ranking_request import (
        AuthenticatedOwner, OrderedHistoryEvent, RankingCandidate, RankingRequest,
        validate_ranking_request,
    )

    if not isinstance(payload, Mapping):
        raise ValueError("invalid persisted request")
    owner = payload["owner"]
    if not isinstance(owner, Mapping):
        raise ValueError("invalid persisted owner")

    def history_type(value):
        try:
            return EventType(value)
        except ValueError:
            return M2HistoryEventType(value)

    candidates = tuple(RankingCandidate(
        candidate_id=item["candidate_id"], canonical_story_id=item["canonical_story_id"],
        source_document_id=item["source_document_id"], title=item["title"],
        summary=item["summary"], source_id=item["source_id"], language=item["language"],
        published_at=datetime.fromisoformat(item["published_at"]),
    ) for item in payload["candidates"])
    events = tuple(OrderedHistoryEvent(
        event_id=item["event_id"], event_type=history_type(item["event_type"]),
        occurred_at=datetime.fromisoformat(item["occurred_at"]),
        event_revision=item["event_revision"], story_id=item.get("story_id"),
        query_text=item.get("query_text"), story_title=item.get("story_title"),
        story_summary=item.get("story_summary"), source_id=item.get("source_id"),
        action_value=item.get("action_value"),
    ) for item in payload["ordered_history"])
    request = RankingRequest(
        schema_version=payload["schema_version"], request_id=payload["request_id"],
        owner=AuthenticatedOwner(owner["tenant_id"], owner["user_id"],
                                 owner["principal_id"], ActorKind(owner["actor_kind"])),
        candidates=candidates,
        selected_candidate_registry_ids=tuple(payload["selected_candidate_registry_ids"]),
        ordered_history=events, history_revision=payload["history_revision"],
        server_commit_revision=payload["server_commit_revision"],
        history_generation=payload["history_generation"],
        consent_revision=payload["consent_revision"],
        policy_version=payload["policy_version"], model_version=payload["model_version"],
        query=payload.get("query"),
    )
    validate_ranking_request(request)
    return request
