"""Versioned request and response boundary for M2 model ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .enums import ActorKind, EventType, M2HistoryEventType, RankingResultMode

_CANONICAL_STORY_ID = re.compile(r"story:[0-9a-f]{64}").fullmatch
_SUPPORTED_SCHEMA_VERSIONS = (1,)


@dataclass(frozen=True)
class AuthenticatedOwner:
    """Server-supplied owner context, excluded from model input.

    Possession of this value does not prove authentication. The server must
    authenticate and authorize it before constructing the ranking request.
    """

    tenant_id: str
    user_id: str
    principal_id: str
    actor_kind: ActorKind


@dataclass(frozen=True)
class RankingCandidate:
    """Immutable story content selected from the eligible-candidate registry."""

    candidate_id: str
    canonical_story_id: str
    source_document_id: str
    title: str
    summary: str
    source_id: str
    language: str
    published_at: datetime


@dataclass(frozen=True)
class OrderedHistoryEvent:
    """One committed event in owner-history order."""

    event_id: str
    event_type: EventType | M2HistoryEventType
    occurred_at: datetime
    event_revision: int
    story_id: str | None = None
    query_text: str | None = None
    story_title: str | None = None
    story_summary: str | None = None
    source_id: str | None = None
    action_value: bool | None = None


@dataclass(frozen=True)
class ModelRankingInput:
    """Provider-neutral private payload. It intentionally has no owner IDs."""

    query: str | None
    candidates: tuple[RankingCandidate, ...]
    ordered_history: tuple[OrderedHistoryEvent, ...]
    history_revision: int
    server_commit_revision: int
    history_generation: int
    consent_revision: int
    policy_version: str
    model_version: str


@dataclass(frozen=True)
class RankingRequest:
    """Authenticated server request plus the payload eligible for the model."""

    schema_version: int
    request_id: str
    owner: AuthenticatedOwner
    candidates: tuple[RankingCandidate, ...]
    selected_candidate_registry_ids: tuple[str, ...]
    ordered_history: tuple[OrderedHistoryEvent, ...]
    history_revision: int
    server_commit_revision: int
    history_generation: int
    consent_revision: int
    policy_version: str
    model_version: str
    query: str | None = None

    def model_input(self) -> ModelRankingInput:
        validate_ranking_request(self)
        return ModelRankingInput(
            query=self.query,
            candidates=self.candidates,
            ordered_history=self.ordered_history,
            history_revision=self.history_revision,
            server_commit_revision=self.server_commit_revision,
            history_generation=self.history_generation,
            consent_revision=self.consent_revision,
            policy_version=self.policy_version,
            model_version=self.model_version,
        )


@dataclass(frozen=True)
class RankingResponseReceipt:
    """Receipt binding a returned order to the exact request revisions."""

    schema_version: int
    request_id: str
    policy_version: str
    model_version: str
    history_revision: int
    server_commit_revision: int
    history_generation: int
    consent_revision: int
    newest_event_id: str | None
    candidate_ids: tuple[str, ...]
    ranked_candidate_ids: tuple[str, ...]
    result_mode: RankingResultMode
    fallback_reason: str = ""


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be non-blank and unpadded")


def _require_canonical_owner_id(value: object, field_name: str) -> None:
    from curator.ownership import noncanonical_id_reason

    _require_nonblank(value, field_name)
    assert isinstance(value, str)
    if noncanonical_id_reason(value):
        raise ValueError(f"{field_name} must be a canonical ownership ID")


def validate_ranking_request(request: RankingRequest) -> None:
    """Fail closed at the adapter boundary without adding a framework."""

    if not isinstance(request, RankingRequest) or not isinstance(request.owner, AuthenticatedOwner):
        raise ValueError("request and owner must use the supported contract types")
    if (
        not isinstance(request.candidates, tuple)
        or not all(isinstance(candidate, RankingCandidate) for candidate in request.candidates)
        or not isinstance(request.selected_candidate_registry_ids, tuple)
        or not all(isinstance(candidate_id, str) for candidate_id in request.selected_candidate_registry_ids)
        or not isinstance(request.ordered_history, tuple)
        or not all(isinstance(event, OrderedHistoryEvent) for event in request.ordered_history)
        or (request.query is not None and not isinstance(request.query, str))
    ):
        raise ValueError("request collections and query must use the supported contract types")
    if type(request.schema_version) is not int or request.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("unsupported schema version")
    if (type(request.history_revision) is not int or request.history_revision < 0
        or type(request.server_commit_revision) is not int
        or request.server_commit_revision < request.history_revision
        or type(request.history_generation) is not int or request.history_generation < 1
        or type(request.consent_revision) is not int or request.consent_revision < 1):
        raise ValueError("schema and history revisions must be valid")
    if not isinstance(request.owner.actor_kind, ActorKind):
        raise ValueError("owner actor kind must be a supported enum")
    _require_nonblank(request.request_id, "request_id")
    for field_name, value in (
        ("tenant_id", request.owner.tenant_id),
        ("user_id", request.owner.user_id),
        ("principal_id", request.owner.principal_id),
    ):
        _require_canonical_owner_id(value, field_name)
    _require_nonblank(request.policy_version, "policy_version")
    _require_nonblank(request.model_version, "model_version")
    candidate_ids = tuple(candidate.candidate_id for candidate in request.candidates)
    registry_ids = request.selected_candidate_registry_ids
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate IDs must be unique")
    if len(registry_ids) != len(set(registry_ids)):
        raise ValueError("canonical registry IDs must be unique")
    if set(candidate_ids) != set(registry_ids) or any(
        candidate.candidate_id != candidate.canonical_story_id
        or _CANONICAL_STORY_ID(candidate.candidate_id) is None
        for candidate in request.candidates
    ):
        raise ValueError("candidate IDs must exactly match the selected eligible-candidate registry")
    for candidate in request.candidates:
        for field_name in ("source_document_id", "title", "source_id", "language"):
            _require_nonblank(getattr(candidate, field_name), f"candidate {field_name}")
        if not isinstance(candidate.summary, str) or not isinstance(candidate.published_at, datetime):
            raise ValueError("candidate content fields have invalid types")
    revisions = tuple(event.event_revision for event in request.ordered_history)
    if any(type(revision) is not int for revision in revisions):
        raise ValueError("history event fields have invalid types")
    if any(revision < 1 for revision in revisions) or any(
        left >= right for left, right in zip(revisions, revisions[1:])
    ):
        raise ValueError("ordered history must use strict revision order")
    if revisions and revisions[-1] != request.history_revision:
        raise ValueError("history revision must name the newest included event")
    if not revisions and request.history_revision != 0:
        raise ValueError("empty history requires revision zero")
    if any(
        not isinstance(event.event_type, (EventType, M2HistoryEventType))
        or not isinstance(event.occurred_at, datetime)
        or event.occurred_at.tzinfo is None
        for event in request.ordered_history
    ):
        raise ValueError("history event fields have invalid types")
    for event in request.ordered_history:
        for value in (event.story_title, event.story_summary, event.source_id):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("history story context must contain valid strings")
        if event.action_value is not None and type(event.action_value) is not bool:
            raise ValueError("history action value must be boolean when present")
    event_ids = tuple(event.event_id for event in request.ordered_history)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("history event IDs must be unique")


def validate_ranking_response(
    receipt: RankingResponseReceipt, expected_request: RankingRequest
) -> None:
    """Require an exact unique candidate permutation and coherent mode."""

    if not isinstance(receipt, RankingResponseReceipt):
        raise ValueError("response must use the supported contract type")
    validate_ranking_request(expected_request)
    expected_newest = (
        expected_request.ordered_history[-1].event_id
        if expected_request.ordered_history else None
    )
    if (
        type(receipt.schema_version) is not int
        or receipt.schema_version != expected_request.schema_version
        or receipt.request_id != expected_request.request_id
        or receipt.policy_version != expected_request.policy_version
        or receipt.model_version != expected_request.model_version
        or type(receipt.history_revision) is not int
        or receipt.history_revision != expected_request.history_revision
        or type(receipt.server_commit_revision) is not int
        or receipt.server_commit_revision != expected_request.server_commit_revision
        or type(receipt.history_generation) is not int
        or receipt.history_generation != expected_request.history_generation
        or type(receipt.consent_revision) is not int
        or receipt.consent_revision != expected_request.consent_revision
        or receipt.newest_event_id != expected_newest
        or receipt.candidate_ids
        != tuple(candidate.candidate_id for candidate in expected_request.candidates)
    ):
        raise ValueError("response receipt does not match the expected request")
    if not isinstance(receipt.result_mode, RankingResultMode):
        raise ValueError("response mode must be a supported enum")
    if (
        len(receipt.ranked_candidate_ids) != len(set(receipt.ranked_candidate_ids))
        or set(receipt.ranked_candidate_ids) != set(receipt.candidate_ids)
        or len(receipt.ranked_candidate_ids) != len(receipt.candidate_ids)
    ):
        raise ValueError("ranked IDs must be an exact unique permutation")
    if receipt.result_mode is RankingResultMode.MODEL and receipt.fallback_reason:
        raise ValueError("model results cannot carry a fallback reason")
    if receipt.result_mode is RankingResultMode.FALLBACK and not receipt.fallback_reason:
        raise ValueError("fallback results require a reason")
