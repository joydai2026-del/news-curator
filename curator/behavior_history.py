"""Shared human and agent command boundary for owner behavior history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .contracts import EventType, M2HistoryEventType

_EVENT_ID = re.compile(r"event:[0-9a-f]{64}").fullmatch
_STORY_ID = re.compile(r"story:[0-9a-f]{64}").fullmatch


@dataclass(frozen=True)
class BehaviorEventCommand:
    event_id: str
    event_type: EventType | M2HistoryEventType
    payload: dict[str, object]
    occurred_at: datetime
    expected_history_generation: int
    schema_version: int = 1

    def rpc_arguments(self) -> dict[str, object]:
        validate_behavior_event(self)
        return {
            "p_event_id": self.event_id,
            "p_event_type": self.event_type.value,
            "p_payload": self.payload,
            "p_occurred_at": self.occurred_at.isoformat(),
            "p_expected_history_generation": self.expected_history_generation,
            "p_schema_version": self.schema_version,
        }


def validate_behavior_event(command: BehaviorEventCommand) -> None:
    if type(command.schema_version) is not int or command.schema_version != 1:
        raise ValueError("unsupported behavior event schema")
    if _EVENT_ID(command.event_id) is None:
        raise ValueError("invalid event identity")
    if not isinstance(command.event_type, (EventType, M2HistoryEventType)):
        raise ValueError("unsupported behavior event vocabulary")
    if not isinstance(command.payload, dict) or not command.payload:
        raise ValueError("behavior event payload must be a non-empty object")
    if not isinstance(command.occurred_at, datetime) or command.occurred_at.tzinfo is None:
        raise ValueError("behavior event time must carry a timezone")
    if type(command.expected_history_generation) is not int or command.expected_history_generation < 1:
        raise ValueError("invalid expected history generation")
    story_id = command.payload.get("story_id")
    if story_id is not None and (not isinstance(story_id, str) or _STORY_ID(story_id) is None):
        raise ValueError("invalid canonical story identity")
