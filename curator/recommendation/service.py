"""Authenticated application service for M2 ranking and frozen pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from curator.contracts.enums import ActorKind, EventType, M2HistoryEventType
from curator.contracts.ranking_request import (
    AuthenticatedOwner,
    OrderedHistoryEvent,
    RankingCandidate,
    RankingRequest,
)

from .rankllm_adapter import BudgetState, RankLLMAdapter
from .supabase_http import SupabaseAuthenticationError


class AuthenticationError(ValueError):
    pass


class StaleRankingError(RuntimeError):
    pass


class SupabaseAuth(Protocol):
    def get_user(self, access_token: str) -> Mapping[str, object]: ...


class RankingStore(Protocol):
    def history_snapshot(self, access_token: str) -> Mapping[str, object]: ...
    def retained_candidates(self, *, category_id: str | None, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None) -> Sequence[Mapping[str, object]]: ...
    def retained_candidates_language_exclusive(self, *, display_language: str, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None,
                            policy_id: str | None = None) -> Sequence[Mapping[str, object]]: ...
    def owner_states(self, access_token: str, story_ids: Sequence[str]) -> Mapping[str, Mapping[str, object]]: ...
    def reserve_budget(self, *, user_id: str, request_id: str, amount_usd: float, daily_limit_usd: float) -> bool: ...
    def settle_budget(self, *, user_id: str, request_id: str, actual_usd: float, status: str) -> None: ...
    def save_frozen_order(self, *, user_id: str, request_id: str, bindings: Mapping[str, object], cards: Sequence[Mapping[str, object]], page_size: int, expires_at: int) -> str: ...
    def load_frozen_order(self, *, user_id: str, frozen_order_id: str) -> Mapping[str, object] | None: ...


@dataclass(frozen=True)
class ServicePolicy:
    policy_version: str
    model_version: str
    provider_policy_id: str
    tenant_id: str
    candidate_limit: int = 200
    maximum_page_size: int = 25
    maximum_excluded_story_ids: int = 1000
    cursor_ttl_seconds: int = 900
    daily_cost_limit_usd: float = 2.0
    # The owners allowed to reach the paid path. EMPTY IS NOBODY, never
    # everybody: see `_authenticate`.
    preview_owner_ids: tuple[str, ...] = ()
    enabled: bool = False
    # The reader's display language. Exclusivity is always stated against this
    # value, never against "is Chinese", so the mirror direction is config.
    display_language: str = "en"
    # The language-exclusive corpus is one more value of the topic selector.
    # Empty disables the section without touching any other code path.
    exclusive_category_id: str = ""
    other_lane_enabled: bool = True
    # The pairing policy whose decisions this lane is allowed to serve. A
    # superseded prompt's answers must not survive an upgrade.
    exclusivity_policy_id: str = "pairing-json-v1"

    def __post_init__(self) -> None:
        # Two layers, both required, because the gap between them is where F5
        # lived: runtime.py refuses an empty env value on the normal boot path,
        # `_authenticate` refuses an unlisted owner at every request, and this
        # refuses the invalid POLICY OBJECT so an enabled service with no
        # allowlist cannot be constructed at all, by a fixture or by a second
        # composition root. Turning the gate off is a deliberate future change
        # to this line, never the accident of leaving a variable unset.
        if self.enabled and not self.preview_owner_ids:
            raise ValueError("an enabled ranking service requires a non-empty preview owner allowlist")
        if any(not isinstance(value, str) or not value.strip() for value in self.preview_owner_ids):
            raise ValueError("preview_owner_ids entries must be non-empty owner ids")
        if self.display_language not in ("en", "zh"):
            raise ValueError("display_language must be a supported language")
        if self.exclusive_category_id and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", self.exclusive_category_id):
            raise ValueError("exclusive_category_id must be a category id")
        # The lane RPC refuses a malformed policy id on EVERY request, so an
        # empty or mistyped value takes the section down for every reader.
        if not isinstance(self.exclusivity_policy_id, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,64}", self.exclusivity_policy_id):
            raise ValueError("exclusivity_policy_id must be a policy id")


class RankingService:
    def __init__(self, *, auth: SupabaseAuth, store: RankingStore, adapter: RankLLMAdapter,
                 policy: ServicePolicy, cursor_key: bytes, clock=time.time) -> None:
        if len(cursor_key) < 32:
            raise ValueError("cursor signing key must contain at least 32 bytes")
        self._auth, self._store, self._adapter = auth, store, adapter
        self._policy, self._cursor_key, self._clock = policy, cursor_key, clock

    def rank(self, *, authorization: str, body: Mapping[str, object]) -> dict[str, object]:
        if not self._policy.enabled:
            raise RuntimeError("ranking_disabled")
        token, owner = self._authenticate(authorization)
        snapshot = self._store.history_snapshot(token)
        self._validate_client_bindings(body, snapshot)
        if snapshot.get("provider_processing_enabled") and snapshot.get("provider_policy_id") != self._policy.provider_policy_id:
            raise StaleRankingError("provider_policy_mismatch")
        page_size = self._page_size(body.get("page_size", 20))
        eligibility = body.get("eligibility", {})
        if not isinstance(eligibility, Mapping):
            raise ValueError("invalid_eligibility")
        category_id = self._optional_string(eligibility.get("category"))
        query = self._optional_string(eligibility.get("query"))
        excluded = body.get("exclude_story_ids", [])
        if (not isinstance(excluded, list) or len(excluded) > self._policy.maximum_excluded_story_ids
                or any(not isinstance(value, str) or re.fullmatch(r"story:[0-9a-f]{64}", value) is None for value in excluded)):
            raise ValueError("invalid_exclude_story_ids")
        excluded_set = set(excluded)
        corpus_cursor = body.get("corpus_cursor") or {}
        if not isinstance(corpus_cursor, Mapping):
            raise ValueError("invalid_corpus_cursor")
        before_published = self._optional_string(corpus_cursor.get("before_published_at"))
        before_story = self._optional_string(corpus_cursor.get("before_story_id"))
        if (before_published is None) != (before_story is None):
            raise ValueError("invalid_corpus_cursor")
        # The language-exclusive section is served by the same M2 path: same
        # recipe, same pagination, same frozen order. Only the corpus narrows.
        if self._is_exclusive_category(category_id):
            rows = self._store.retained_candidates_language_exclusive(
                display_language=self._policy.display_language, query=query,
                limit=self._policy.candidate_limit + len(excluded_set) + 1,
                before_published_at=before_published, before_story_id=before_story,
                policy_id=self._policy.exclusivity_policy_id,
            )
        else:
            rows = self._store.retained_candidates(
                category_id=category_id, query=query, limit=self._policy.candidate_limit + len(excluded_set) + 1,
                before_published_at=before_published, before_story_id=before_story,
            )
        filtered = [row for row in rows if row.get("story_id") not in excluded_set]
        has_more = len(filtered) > self._policy.candidate_limit
        rows = filtered[:self._policy.candidate_limit]
        next_corpus = ({"before_published_at": rows[-1]["published_at"], "before_story_id": rows[-1]["story_id"]}
            if rows and has_more else None)
        request_id = str(uuid.uuid4())
        request = self._request(request_id, owner, snapshot, rows, query)
        processing_allowed = bool(snapshot.get("learning_enabled") and snapshot.get("provider_processing_enabled"))
        observed_usage = {}
        attempts_started = 0
        settled_cost = 0.0
        def record_usage(outcome, unknown_attempts, elapsed):
            observed_usage.update(input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                unknown_attempts=unknown_attempts, provider_elapsed_seconds=elapsed)
        def record_attempt(attempt, elapsed):
            nonlocal attempts_started
            attempts_started += 1
        preparation_reason = ""
        if processing_allowed and request.candidates:
            prepared, preparation_reason = self._adapter.prepare_with_reason(request)
        else:
            prepared = None
        estimate = None if prepared is None else self._adapter.reservation_estimate(
            estimated_input_tokens=prepared.input_tokens_bound, estimated_output_tokens=prepared.output_tokens_budget)
        reservation_created = estimate is not None and self._store.reserve_budget(
            user_id=owner.user_id, request_id=request_id, amount_usd=estimate,
            daily_limit_usd=self._policy.daily_cost_limit_usd)
        if not reservation_created:
            receipt = self._adapter.fallback(request, "no_candidates" if not request.candidates else
                "provider_processing_consent_required" if not processing_allowed else preparation_reason or
                "budget_reservation_failed")
        else:
            try:
                receipt = self._adapter.rank(request, provider_processing_consent=processing_allowed,
                    budget=BudgetState(0), estimated_input_tokens=prepared.input_tokens_bound,
                    estimated_output_tokens=prepared.output_tokens_budget, prepared=prepared,
                    usage_observer=record_usage, attempt_observer=record_attempt)
                if observed_usage:
                    settled_cost = self._adapter.settle_observed_cost(
                        input_tokens=observed_usage["input_tokens"], output_tokens=observed_usage["output_tokens"],
                        unknown_attempts=observed_usage["unknown_attempts"], reserved_usd=estimate)
                    self._store.settle_budget(user_id=owner.user_id, request_id=request_id,
                        actual_usd=settled_cost, status="settled")
                elif attempts_started == 0:
                    self._store.settle_budget(user_id=owner.user_id, request_id=request_id,
                        actual_usd=0.0, status="released")
                else:
                    # Once an attempt starts, transport/parser failure cannot
                    # prove zero provider charge. Retain the ceiling reservation.
                    # SQL003 scopes capacity by UTC statement_date, so an
                    # unresolved prior-day reservation cannot consume a new day.
                    settled_cost = 0.0
            except Exception:
                # An ambiguous settlement retains the durable reservation.
                # Releasing zero here could erase a provider charge.
                raise
        latest = self._store.history_snapshot(token)
        self._assert_fresh(snapshot, latest)
        owner_states = self._store.owner_states(token, [str(row["story_id"]) for row in rows])
        by_id = {str(row["story_id"]): self._card(row, owner_states.get(str(row["story_id"]), {})) for row in rows}
        cards = [by_id[story_id] for story_id in receipt.ranked_candidate_ids]
        expires_at = int(self._clock()) + self._policy.cursor_ttl_seconds
        bindings = self._bindings(receipt)
        bindings.update({"eligibility": {"category": category_id, "query": query},
            "corpus_cursor": next_corpus, "corpus_has_more": has_more,
            "corpus_start": dict(corpus_cursor), "excluded_story_ids": list(excluded_set),
            "execution": {**observed_usage, "settled_cost_usd": settled_cost,
                "attempts_started": attempts_started,
                "history_events_included": getattr(prepared, "history_events_included", 0) if prepared else 0,
                "history_events_omitted": getattr(prepared, "history_events_omitted", 0) if prepared else 0,
                "cost_basis": "observed_with_unknown_attempt_reserves" if observed_usage else
                    "unknown_provider_charge_reserved" if reservation_created and attempts_started else
                    "released_no_provider_call" if reservation_created else "no_provider_call",
                "newest_event_id": receipt.newest_event_id}})
        frozen_id = self._store.save_frozen_order(user_id=owner.user_id, request_id=request_id,
            bindings=bindings, cards=cards, page_size=page_size, expires_at=expires_at)
        next_cursor = self._cursor(frozen_id, min(page_size, len(cards)), expires_at) if page_size < len(cards) or has_more else None
        return self._page_response(bindings, cards[:page_size], next_cursor, receipt)

    def page(self, *, authorization: str, cursor: str) -> dict[str, object]:
        if not self._policy.enabled:
            raise RuntimeError("ranking_disabled")
        token, owner = self._authenticate(authorization)
        payload = self._decode_cursor(cursor)
        frozen = self._store.load_frozen_order(user_id=owner.user_id, frozen_order_id=str(payload["frozen_order_id"]))
        if not frozen or int(frozen["expires_at"]) < int(self._clock()):
            raise StaleRankingError("cursor_expired")
        current = self._store.history_snapshot(token)
        current_bindings = {"history_generation": current.get("history_generation"),
            "consent_revision": current.get("consent_revision"),
            "server_commit_revision": current.get("history_revision")}
        for key in ("history_generation", "consent_revision"):
            if current_bindings[key] != frozen["bindings"].get(key):
                raise StaleRankingError(f"changed_{key}")
        changed = current_bindings["server_commit_revision"] != frozen["bindings"].get("server_commit_revision")
        if changed:
            visible = list(dict.fromkeys(frozen["bindings"].get("excluded_story_ids", []) +
                [card["story_id"] for card in frozen["cards"][:int(payload["offset"])]]))
            return self.rank(authorization=authorization, body={**current_bindings,
                "history_revision": current.get("included_history_revision", 0),
                "eligibility": frozen["bindings"].get("eligibility", {}), "exclude_story_ids": visible,
                "corpus_cursor": frozen["bindings"].get("corpus_start", {}),
                "page_size": frozen.get("page_size", self._policy.maximum_page_size)})
        if frozen["bindings"].get("result_mode") == "model" and not current.get("provider_processing_enabled"):
            raise StaleRankingError("consent_disabled")
        offset = int(payload["offset"])
        cards = list(frozen["cards"])
        size = int(frozen.get("page_size", self._policy.maximum_page_size))
        next_offset = offset + size
        next_cursor = self._cursor(str(payload["frozen_order_id"]), next_offset, int(frozen["expires_at"])) if next_offset < len(cards) else None
        if offset >= len(cards) and frozen["bindings"].get("corpus_has_more"):
            return self.rank(authorization=authorization, body={**current_bindings,
                "history_revision": current.get("included_history_revision", 0),
                "eligibility": frozen["bindings"].get("eligibility", {}), "exclude_story_ids": [],
                "corpus_cursor": frozen["bindings"].get("corpus_cursor"), "page_size": size})
        if next_cursor is None and frozen["bindings"].get("corpus_has_more"):
            next_cursor = self._cursor(str(payload["frozen_order_id"]), len(cards), int(frozen["expires_at"]))
        return {"schema_version": 1, **self._public_bindings(frozen["bindings"]), "cards": cards[offset:next_offset], "next_cursor": next_cursor}

    def _authenticate(self, authorization: str) -> tuple[str, AuthenticatedOwner]:
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise AuthenticationError("bearer token required")
        token = authorization[7:].strip()
        try:
            user = self._auth.get_user(token)
        except SupabaseAuthenticationError as error:
            raise AuthenticationError("invalid authenticated user") from error
        user_id = user.get("id")
        if not isinstance(user_id, str):
            raise AuthenticationError("invalid authenticated user")
        # EMPTY MEANS NOBODY. The truthiness guard that used to stand here made
        # an empty allowlist mean "admit everyone", so every enabled service
        # built outside build_application() (a fixture, a script, a second
        # composition root) served the paid provider path to any authenticated
        # owner. runtime.py refuses an empty env value on the normal boot path;
        # this is the same decision made where it is actually enforced.
        if user_id not in self._policy.preview_owner_ids:
            raise AuthenticationError("owner is not enabled for preview")
        return token, AuthenticatedOwner(self._policy.tenant_id, user_id, user_id, ActorKind.HUMAN)

    def _request(self, request_id, owner, snapshot, rows, query):
        candidates = tuple(RankingCandidate(str(r["story_id"]), str(r["story_id"]), str(r["story_id"]),
            str(r["title"]), str(r.get("summary", "")), str(r["source_id"]), str(r["language"]),
            datetime.fromisoformat(str(r["published_at"]).replace("Z", "+00:00"))) for r in rows)
        def event_type(value: object):
            try:
                return EventType(str(value))
            except ValueError:
                return M2HistoryEventType(str(value))

        def optional_context(value: object):
            return None if isinstance(value, str) and not value.strip() else value

        raw_events = snapshot.get("events", ()) if snapshot.get("learning_enabled") else ()
        events = tuple(OrderedHistoryEvent(str(e["event_id"]), event_type(e["event_type"]),
            datetime.fromisoformat(str(e["occurred_at"]).replace("Z", "+00:00")), int(e["event_revision"]),
            e.get("payload", {}).get("story_id"), e.get("payload", {}).get("query"),
            optional_context(e.get("story_title")), optional_context(e.get("story_summary")),
            optional_context(e.get("source_id")), e.get("payload", {}).get("saved")) for e in raw_events)
        return RankingRequest(1, request_id, owner, candidates, tuple(c.candidate_id for c in candidates), events,
            int(snapshot["included_history_revision"]), int(snapshot["history_revision"]),
            int(snapshot["history_generation"]), int(snapshot["consent_revision"]), self._policy.policy_version,
            self._policy.model_version, query)

    def _validate_client_bindings(self, body, snapshot):
        expected = {"history_revision": snapshot.get("included_history_revision"),
            "server_commit_revision": snapshot.get("history_revision"),
            "history_generation": snapshot.get("history_generation"), "consent_revision": snapshot.get("consent_revision")}
        for key, value in expected.items():
            if body.get(key) != value:
                raise StaleRankingError(f"stale_{key}")

    @staticmethod
    def _assert_fresh(before, after):
        for key in ("history_revision", "included_history_revision", "history_generation", "consent_revision", "provider_processing_enabled"):
            if before.get(key) != after.get(key):
                raise StaleRankingError(f"changed_{key}")

    def _cursor(self, frozen_id, offset, expires_at):
        raw = json.dumps({"frozen_order_id": frozen_id, "offset": offset, "expires_at": expires_at}, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, cursor):
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)); raw, signature = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(signature, hmac.new(self._cursor_key, raw, hashlib.sha256).digest()): raise ValueError
            payload = json.loads(raw)
            if int(payload["expires_at"]) < int(self._clock()): raise ValueError
            return payload
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise StaleRankingError("invalid_cursor") from exc

    def _page_size(self, value):
        if type(value) is not int or not 1 <= value <= self._policy.maximum_page_size: raise ValueError("invalid_page_size")
        return value

    @staticmethod
    def _optional_string(value):
        if value is None: return None
        if not isinstance(value, str) or value != value.strip() or not value: raise ValueError("invalid_optional_string")
        return value

    def _is_exclusive_category(self, category_id) -> bool:
        return bool(self._policy.other_lane_enabled and self._policy.exclusive_category_id
                    and category_id == self._policy.exclusive_category_id)

    @staticmethod
    def _card(row, owner_state):
        language = str(row["language"])
        titles = row.get("title_translations") or {}
        summaries = row.get("summary_translations") or {}
        if not isinstance(titles, Mapping) or not isinstance(summaries, Mapping):
            raise ValueError("invalid_translation_overlay")
        # Version 2 is version 1 plus the five translation fields. The reader
        # accepts both for one release, so reader and ranker deploy in any order.
        card = {"card_schema_version": 2, "story_id": row["story_id"], "title": row["title"],
            "summary": row.get("summary", ""), "source_name": row["source_name"], "published_at": row["published_at"],
            "url": row["canonical_url"], "source_id": row["source_id"], "language": language,
            "category_ids": row.get("category_ids", []), "read_at": owner_state.get("read_at"),
            "saved_at": owner_state.get("saved_at"), "state_revision": owner_state.get("state_revision", 0),
            "interests": owner_state.get("interests", [])}
        status = {}
        for target in ("en", "zh"):
            if target == language:
                card[f"title_{target}"] = str(row["title"])
                card[f"summary_{target}"] = str(row.get("summary", ""))
                status[target] = "original"
                continue
            title = titles.get(target)
            summary = summaries.get(target)
            card[f"title_{target}"] = str(title) if isinstance(title, str) else ""
            card[f"summary_{target}"] = str(summary) if isinstance(summary, str) else ""
            # A story with no translation is still served. The reader marks it.
            status[target] = "translated" if card[f"title_{target}"] else "untranslated"
        card["translation_status"] = status
        return card

    @staticmethod
    def _bindings(receipt):
        return {"request_id": receipt.request_id, "policy_version": receipt.policy_version, "model_version": receipt.model_version,
            "history_revision": receipt.history_revision, "history_generation": receipt.history_generation,
            "consent_revision": receipt.consent_revision, "server_commit_revision": receipt.server_commit_revision,
            "result_mode": receipt.result_mode.value,
            "fallback_reason": receipt.fallback_reason}

    @staticmethod
    def _public_bindings(bindings):
        fields = ("request_id", "policy_version", "model_version", "history_revision", "history_generation",
                  "consent_revision", "server_commit_revision", "result_mode", "fallback_reason")
        return {key: bindings[key] for key in fields if key in bindings}

    @classmethod
    def _page_response(cls, bindings, cards, cursor, receipt):
        return {"schema_version": receipt.schema_version, **cls._public_bindings(bindings), "cards": cards, "next_cursor": cursor}
