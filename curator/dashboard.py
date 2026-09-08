"""Bounded owner-only dashboard reads and loaded-view snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .personalization import AuthConfig, AuthError, PreferenceClient, Session
from .personalization.preferences import JsonRestTransport, RestTransport


_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_STORY_RE = re.compile(r"^story:[0-9a-f]{64}$")
_SUMMARY_FIELDS = {
    "schema_version", "scope", "snapshot_at", "saved_count", "saved_unread_count",
    "read_count", "active_interest_signal_count", "topic_signals",
}
_CARD_FIELDS = {
    "story_id", "title", "summary", "canonical_url", "language", "published_at",
    "publication_seq", "position", "topic_ids", "coverage_mentions", "score_components",
    "ordering_mode", "ordering_key", "topic_ranks", "source_kind", "source_name",
    "ranking_explanation", "page_order_mode", "next_cursor", "saved_at", "read_at",
    "state_revision", "interests",
}
MAX_SAVED_PAGES = 100
MAX_CARD_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= MAX_SAFE_INTEGER
    )


def _timestamp(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _bounded(value: Any, characters: int, encoded: int) -> bool:
    return isinstance(value, str) and len(value) <= characters and len(value.encode("utf-8")) <= encoded


@dataclass(frozen=True)
class DashboardSummary:
    value: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Any) -> "DashboardSummary":
        if not isinstance(value, dict) or set(value) != _SUMMARY_FIELDS:
            raise AuthError("The dashboard response was invalid.")
        if (
            isinstance(value["schema_version"], bool)
            or value["schema_version"] != 1
            or value["scope"] != "current_retained_state"
            or not _timestamp(value["snapshot_at"])
        ):
            raise AuthError("The dashboard response was invalid.")
        for field in ("saved_count", "saved_unread_count", "read_count", "active_interest_signal_count"):
            if not _integer(value[field]):
                raise AuthError("The dashboard response was invalid.")
        if value["saved_unread_count"] > value["saved_count"]:
            raise AuthError("The dashboard response was invalid.")
        rows = value["topic_signals"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise AuthError("The dashboard response was invalid.")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"topic_id", "more_like_count", "less_like_count"}:
                raise AuthError("The dashboard response was invalid.")
            topic_id = row["topic_id"]
            if not isinstance(topic_id, str) or not _TOPIC_RE.fullmatch(topic_id) or topic_id in seen:
                raise AuthError("The dashboard response was invalid.")
            if not _integer(row["more_like_count"]) or not _integer(row["less_like_count"]):
                raise AuthError("The dashboard response was invalid.")
            seen.add(topic_id)
        expected_order = sorted(
            rows,
            key=lambda row: (-(row["more_like_count"] + row["less_like_count"]), row["topic_id"]),
        )
        if rows != expected_order or sum(
            row["more_like_count"] + row["less_like_count"] for row in rows
        ) > value["active_interest_signal_count"]:
            raise AuthError("The dashboard response was invalid.")
        return cls(dict(value))

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.value))


def _safe_url(value: Any, *, allow_empty: bool) -> bool:
    if value == "" and allow_empty:
        return True
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password


def _validate_cursor(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"before_saved_at", "before_story_id"}
        and _timestamp(value["before_saved_at"])
        and isinstance(value["before_story_id"], str)
        and bool(_STORY_RE.fullmatch(value["before_story_id"]))
    )


def _validate_card(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CARD_FIELDS:
        raise AuthError("The saved response was invalid.")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_CARD_BYTES:
        raise AuthError("The saved response was invalid.")
    if not isinstance(value["story_id"], str) or not _STORY_RE.fullmatch(value["story_id"]):
        raise AuthError("The saved response was invalid.")
    if not _bounded(value["title"], 2000, 8000) or not value["title"] or not _bounded(value["summary"], 8000, 32000):
        raise AuthError("The saved response was invalid.")
    if value["language"] not in ("en", "zh") or value["source_kind"] not in ("outlet", "newsletter"):
        raise AuthError("The saved response was invalid.")
    if not _safe_url(value["canonical_url"], allow_empty=value["source_kind"] == "newsletter"):
        raise AuthError("The saved response was invalid.")
    if not _timestamp(value["published_at"]) or not _timestamp(value["saved_at"]):
        raise AuthError("The saved response was invalid.")
    if not _timestamp(value["read_at"], nullable=True):
        raise AuthError("The saved response was invalid.")
    if not _integer(value["publication_seq"]) or not _integer(value["position"]) or not _integer(value["state_revision"]):
        raise AuthError("The saved response was invalid.")
    if value["page_order_mode"] != "saved_at" or not _validate_cursor(value["next_cursor"]):
        raise AuthError("The saved response was invalid.")
    topics = value["topic_ids"]
    if not isinstance(topics, list) or not 1 <= len(topics) <= 20 or not all(isinstance(item, str) and _TOPIC_RE.fullmatch(item) for item in topics):
        raise AuthError("The saved response was invalid.")
    coverage = value["coverage_mentions"]
    if (
        not isinstance(coverage, list)
        or len(coverage) > 20
        or len(json.dumps(coverage, ensure_ascii=False).encode("utf-8")) > 32768
    ):
        raise AuthError("The saved response was invalid.")
    for mention in coverage:
        if not isinstance(mention, dict) or set(mention) != {"headline", "mentioned_at", "source_id", "source_kind", "source_name", "url"}:
            raise AuthError("The saved response was invalid.")
        if mention["source_kind"] not in ("outlet", "newsletter") or not _bounded(mention["source_id"], 160, 512) or not mention["source_id"] or not _bounded(mention["source_name"], 200, 1000) or not mention["source_name"] or not _bounded(mention["headline"], 2000, 8000) or not mention["headline"] or not _timestamp(mention["mentioned_at"]) or not _safe_url(mention["url"], allow_empty=False):
            raise AuthError("The saved response was invalid.")
    if value["ordering_mode"] not in ("weighted_total", "preference_then_freshness", "native_rank_then_freshness"):
        raise AuthError("The saved response was invalid.")
    if not all(isinstance(value[field], dict) for field in ("score_components", "ordering_key", "topic_ranks")):
        raise AuthError("The saved response was invalid.")
    if len(json.dumps(value["ordering_key"], ensure_ascii=False).encode("utf-8")) > 2048 or len(json.dumps(value["score_components"], ensure_ascii=False).encode("utf-8")) > 8192:
        raise AuthError("The saved response was invalid.")
    if len(value["topic_ranks"]) > 100 or not all(isinstance(topic, str) and _TOPIC_RE.fullmatch(topic) and _integer(position, minimum=1) for topic, position in value["topic_ranks"].items()):
        raise AuthError("The saved response was invalid.")
    if not _bounded(value["source_name"], 200, 1000) or not value["source_name"] or not _bounded(value["ranking_explanation"], 2000, 8000) or not value["ranking_explanation"]:
        raise AuthError("The saved response was invalid.")
    interests = value["interests"]
    if not isinstance(interests, list) or len(interests) > 20:
        raise AuthError("The saved response was invalid.")
    for interest in interests:
        if not isinstance(interest, dict) or set(interest) != {"topic_id", "signal", "revision"}:
            raise AuthError("The saved response was invalid.")
        if not isinstance(interest["topic_id"], str) or not _TOPIC_RE.fullmatch(interest["topic_id"]):
            raise AuthError("The saved response was invalid.")
        if interest["signal"] not in ("more_like", "less_like") or not _integer(interest["revision"]):
            raise AuthError("The saved response was invalid.")
    return json.loads(json.dumps(value))


class DashboardClient:
    def __init__(
        self,
        config: AuthConfig,
        *,
        transport: RestTransport | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or JsonRestTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def _headers(self, session: Session) -> dict[str, str]:
        return {
            "apikey": self.config.publishable_key,
            "authorization": f"Bearer {session.access_token}",
            "accept": "application/json",
            "content-type": "application/json",
        }

    def summary(self, session: Session) -> DashboardSummary:
        status, payload = self.transport.request(
            "POST",
            f"{self.config.supabase_url}/rest/v1/rpc/dashboard_summary",
            headers=self._headers(session),
            body={},
        )
        if status != 200:
            raise AuthError("The dashboard could not be read.")
        return DashboardSummary.from_mapping(payload)

    def _page_size(self, session: Session) -> int:
        status, payload = self.transport.request(
            "POST", f"{self.config.supabase_url}/rest/v1/rpc/latest_publication",
            headers=self._headers(session), body={},
        )
        if status != 200 or not isinstance(payload, dict):
            raise AuthError("The dashboard could not be read.")
        page_size = payload.get("page_size")
        if not _integer(page_size, minimum=1) or page_size > 100:
            raise AuthError("The dashboard response was invalid.")
        return page_size

    def _saved_page(self, session: Session, *, page_size: int, cursor: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        body = {"p_before_saved_at": None, "p_before_story_id": None, "p_limit": page_size}
        if cursor is not None:
            if not _validate_cursor(cursor):
                raise AuthError("The saved response was invalid.")
            body.update(p_before_saved_at=cursor["before_saved_at"], p_before_story_id=cursor["before_story_id"])
        status, payload = self.transport.request(
            "POST", f"{self.config.supabase_url}/rest/v1/rpc/saved_page",
            headers=self._headers(session), body=body,
        )
        if status != 200 or not isinstance(payload, list) or len(payload) > page_size:
            raise AuthError("The saved response was invalid.")
        return [_validate_card(row) for row in payload]

    def snapshot(self, session: Session, *, saved_pages: int = 1) -> dict[str, Any]:
        if not _integer(saved_pages, minimum=1) or saved_pages > MAX_SAVED_PAGES:
            raise ValueError("saved_pages must be between 1 and 100.")
        summary = self.summary(session).as_dict()
        preference = PreferenceClient(self.config, transport=self.transport).get(session)
        if preference is not None and not _integer(preference.revision):
            raise AuthError("The dashboard response was invalid.")
        page_size = self._page_size(session)
        loaded: list[dict[str, Any]] = []
        cursor: Mapping[str, Any] | None = None
        complete = False
        seen: set[str] = set()
        for _ in range(saved_pages):
            page = self._saved_page(session, page_size=page_size, cursor=cursor)
            for card in page:
                if card["story_id"] in seen:
                    raise AuthError("The saved response was invalid.")
                seen.add(card["story_id"])
                loaded.append(card)
            if len(page) < page_size:
                complete = True
                cursor = None
                break
            cursor = page[-1]["next_cursor"]
        preference_fields = (
            {key: value for key, value in preference.as_dict().items() if key != "user_id"}
            if preference is not None
            else {"revision": 0, "locale": "en", "interests": [], "saved_searches": [], "created_at": None, "updated_at": None}
        )
        snapshot_at = self.clock()
        if not _timestamp(snapshot_at):
            raise AuthError("The dashboard snapshot time was invalid.")
        return {
            "schema_version": 1,
            "kind": "loaded_dashboard_snapshot",
            "snapshot_at": snapshot_at,
            "summary": summary,
            "preferences": preference_fields,
            "saved": {
                "loaded_count": len(loaded),
                "displayed_count": len(loaded),
                "page_size": page_size,
                "all_saved_loaded": complete,
                "next_cursor": cursor,
                "items": loaded,
            },
        }
