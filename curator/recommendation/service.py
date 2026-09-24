"""Authenticated application service for M2 ranking and frozen pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from curator.dedup import normalize_title
from curator.contracts.enums import ActorKind, EventType, M2HistoryEventType, RankingResultMode
from curator.contracts.ranking_request import (
    AuthenticatedOwner,
    OrderedHistoryEvent,
    RankingCandidate,
    RankingRequest,
    validate_ranking_response,
)

from .composition import BACKFILL_LANE, CompositionPolicy
from .diagnostics import log_suppressed_exception
from .finalize import _duplicate_keys, finalize_order
from .lane_diagnostics import LaneDiagnostics
from .prepared_order import request_to_payload, select_prepared_order
from .profile import BehaviorProfile, build_profile
from .rankllm_adapter import BudgetState, RankLLMAdapter
from .recipe import LanedCandidate, assign_lane, build_window, lane_window_quotas
from .supabase_http import SupabaseAuthenticationError


# The most Supabase round trips one /rank can make WHILE HOLDING the run's
# ranking claim. It sizes run.ranking_claim_seconds: the claim may not expire
# while its holder is still working, or a second caller takes over and pays for
# the same view. Measured, not guessed, by
# tests/test_ranker_claimed_section_budget.py, which walks the longest path with
# a counting transport and refuses a count above this number.
#
# The initial paid path now measures 22 calls after SQL-side filtering, but
# the same claim must also protect two bounded continuation scans. Keep the
# 33-call floor for the accepted continuation policy range, including its
# owner-state retries; reducing it based on the first-page trace alone would
# permit the claim to expire while a valid page turn is still working.
CLAIMED_SECTION_MAX_TRANSPORT_CALLS = 33
# Two bounded continuation scans can run under one claim. Each scan is capped
# at two general and eight lane RPCs when a refill is allowed; the remaining
# calls cover claim, frozen-order reloads, owner states, appends and release,
# plus one non-retried opened-ID lookup per pass.
CONTINUATION_CLAIMED_MAX_TRANSPORT_CALLS = 33

# Private control result from `_existing_run_page`: a bound order that was
# deleted by a consent/history invalidation may be replaced, while an expired
# order that still exists must not buy a second ranking in the same view.
_MISSING_FROZEN_ORDER = object()

_PAGE_STAGE_LABELS = frozenset({
    "authenticate", "cursor_decode", "frozen_load", "history_load", "claim",
    "locked_load", "continuation_pass", "final_load", "claim_release",
    "owner_overlay", "filtered_record", "response_reserve",
})


@contextmanager
def _page_stage(stages: dict[str, float], label: str):
    if label not in _PAGE_STAGE_LABELS:
        raise ValueError("invalid_page_stage")
    started = time.perf_counter()
    try:
        yield
    finally:
        stages[label] = round(stages.get(label, 0) +
                              (time.perf_counter() - started) * 1000, 3)


def _page_diagnostic(payload: Mapping[str, object]) -> None:
    """Keep page-path telemetry from changing the page or its original error."""
    try:
        sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _page_suppressed_exception(event: str, error: Exception, **fields) -> None:
    try:
        log_suppressed_exception(event, error, stream=sys.stderr, **fields)
    except Exception:
        pass

# The request accepts at most 1,000 exclusions. Eleven batches can step past all
# of them and fill the 51-row candidate-plus-lookahead window; one additional
# batch absorbs a full page of incomplete translations without making the
# paid, claim-held section unbounded.
EXCLUSIVE_SCAN_MAX_BATCHES = 12
EXCLUSIVE_CONTINUATION_MAX_BATCHES = 2


class AuthenticationError(ValueError):
    pass


class StaleRankingError(RuntimeError):
    pass


class RankingInProgressError(StaleRankingError):
    """Another request is already buying this run's ranking.

    Not an error and not staleness: the answer exists in a moment, and the right
    behavior is to wait for it rather than to buy a second one. Raised only when
    the winner has not bound its order yet; once it has, the loser is served that
    order instead of this.
    """

    def __init__(self) -> None:
        super().__init__("ranking_in_progress")


class ProviderConsentRequiredError(StaleRankingError):
    """The owner consented to a DIFFERENT provider policy than the one running.

    This is not staleness and it is not an outage: it is a question with an
    answer the reader can offer in one tap. It carries the policy id the owner
    has to agree to, because a reader that cannot name it can only show a dead
    feed. Raised whenever the prompt revision moves, which is exactly what
    happens on the deploy that ships a new ranking prompt.
    """

    def __init__(self, provider_policy_id: str) -> None:
        super().__init__("provider_consent_required")
        self.provider_policy_id = provider_policy_id


class SupabaseAuth(Protocol):
    def get_user(self, access_token: str) -> Mapping[str, object]: ...


class RankingStore(Protocol):
    def history_snapshot(self, access_token: str) -> Mapping[str, object]: ...
    def retained_candidates(self, *, category_id: str | None, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None) -> Sequence[Mapping[str, object]]: ...
    def retained_candidates_language_exclusive(self, *, display_language: str, query: str | None, limit: int,
                            before_published_at: str | None = None, before_story_id: str | None = None,
                            policy_id: str | None = None) -> Sequence[Mapping[str, object]]: ...
    def retained_candidates_v2(self, *, category_id: str | None, query: str | None, lane: str | None,
                            profile_categories: Sequence[str], profile_sources: Sequence[str],
                            trend_window_hours: int, trend_min_sources: int, max_age_hours: int | None,
                            min_age_hours: int | None, limit: int, owner_id: str,
                            hide_already_opened: bool, before_published_at: str | None = None,
                            before_story_id: str | None = None,
                            before_source_count: int | None = None,
                            excluded_story_ids: Sequence[str] = (),
                            suppressed_sources: Sequence[str] = (),
                            suppressed_topics: Sequence[str] = ()) -> Sequence[Mapping[str, object]]: ...
    def enqueue_prepared_order(self, **kwargs) -> bool: ...
    def consume_prepared_order(self, **kwargs) -> Mapping[str, object] | None: ...
    def prepared_history_is_compatible(self, **kwargs) -> bool: ...
    def open_reading_run(self, *, user_id: str, idle_minutes: int, max_minutes: int,
                         profile: Mapping[str, object]) -> Mapping[str, object]: ...
    def record_reading_run_filter(self, *, user_id: str, run_id: str,
                                  story_ids: Sequence[str]) -> int: ...
    def open_run_view(self, *, user_id: str, run_id: str,
                      eligibility_key: str) -> Mapping[str, object]: ...
    def bind_run_frozen_order(self, *, user_id: str, run_id: str, eligibility_key: str,
                              frozen_order_id: str, token: str | None = None) -> bool: ...
    def claim_run_ranking(self, *, user_id: str, run_id: str, eligibility_key: str, token: str,
                          ttl_seconds: int) -> Mapping[str, object]: ...
    def claim_continuation_snapshot(self, *, user_id: str, run_id: str,
                                    eligibility_key: str, frozen_order_id: str,
                                    token: str, ttl_seconds: int) -> Mapping[str, object]: ...
    def release_run_ranking_claim(self, *, user_id: str, run_id: str, eligibility_key: str,
                                  token: str) -> bool: ...
    def record_run_page(self, *, user_id: str, run_id: str, eligibility_key: str,
                        pages: int) -> int: ...
    def reserve_run_response(self, *, user_id: str, run_id: str, eligibility_key: str,
                             frozen_order_id: str, response_number: int,
                             offset: int, next_offset: int) -> Mapping[str, object]: ...
    def owner_states(self, access_token: str, story_ids: Sequence[str]) -> Mapping[str, Mapping[str, object]]: ...
    def opened_candidate_ids(self, access_token: str, story_ids: Sequence[str]) -> set[str]: ...
    def reserve_budget(self, *, user_id: str, request_id: str, amount_usd: float, daily_limit_usd: float) -> bool: ...
    def reserve_budget_claimed(self, *, user_id: str, request_id: str, amount_usd: float,
                               daily_limit_usd: float, run_id: str, eligibility_key: str,
                               claim_token: str) -> Mapping[str, object]: ...
    def settle_budget(self, *, user_id: str, request_id: str, actual_usd: float, status: str) -> None: ...
    def save_frozen_order(self, *, user_id: str, request_id: str, bindings: Mapping[str, object], cards: Sequence[Mapping[str, object]], page_size: int, expires_at: int, run_id: str | None = None) -> str: ...
    def load_frozen_order(self, *, user_id: str, frozen_order_id: str) -> Mapping[str, object] | None: ...
    def extend_frozen_order(self, *, user_id: str, frozen_order_id: str,
                            cards: Sequence[Mapping[str, object]],
                            bindings: Mapping[str, object]) -> int: ...


@dataclass(frozen=True)
class ServicePolicy:
    policy_version: str
    model_version: str
    provider_policy_id: str
    tenant_id: str
    candidate_limit: int = 200
    maximum_page_size: int = 25
    maximum_excluded_story_ids: int = 1000
    exclusive_scan_max_batches: int = EXCLUSIVE_SCAN_MAX_BATCHES
    exclusive_continuation_max_batches: int = EXCLUSIVE_CONTINUATION_MAX_BATCHES
    cursor_ttl_seconds: int = 3600
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
    # The M2.1 Phase 2 feed recipe. None keeps the pre-Phase-2 window (the newest
    # `candidate_limit` rows), which is the documented rollback: the recipe is
    # turned off by unsetting one config path, not by reverting code.
    composition: CompositionPolicy | None = None
    # Hash of the complete effective ranking contract. The production runtime
    # binds checked-in policies, prompt content, and ranking code into this.
    # Alternate composition roots receive a deterministic dataclass digest.
    effective_policy_digest: str = ""
    next_run_preparation_enabled: bool = False
    next_run_preparation_ttl_seconds: int = 7200
    next_run_preparation_minimum_overlap: int = 5

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
        if self.effective_policy_digest and re.fullmatch(r"[0-9a-f]{64}", self.effective_policy_digest) is None:
            raise ValueError("effective_policy_digest must be lowercase SHA-256")
        if type(self.next_run_preparation_enabled) is not bool:
            raise ValueError("next_run_preparation_enabled must be boolean")
        if (type(self.next_run_preparation_ttl_seconds) is not int
                or not 3600 <= self.next_run_preparation_ttl_seconds <= 86400):
            raise ValueError("next_run_preparation_ttl_seconds must be 3600..86400")
        if (type(self.next_run_preparation_minimum_overlap) is not int
                or not 1 <= self.next_run_preparation_minimum_overlap <= 50):
            raise ValueError("next_run_preparation_minimum_overlap must be 1..50")
        if self.display_language not in ("en", "zh"):
            raise ValueError("display_language must be a supported language")
        if self.exclusive_category_id and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", self.exclusive_category_id):
            raise ValueError("exclusive_category_id must be a category id")
        if (not isinstance(self.maximum_excluded_story_ids, int)
                or isinstance(self.maximum_excluded_story_ids, bool)
                or not 0 <= self.maximum_excluded_story_ids <= 1000):
            raise ValueError("maximum_excluded_story_ids must be between 0 and 1000")
        if (not isinstance(self.cursor_ttl_seconds, int)
                or isinstance(self.cursor_ttl_seconds, bool)
                or self.cursor_ttl_seconds < 1):
            raise ValueError("cursor_ttl_seconds must be a positive integer")
        if (self.composition is not None
                and self.cursor_ttl_seconds < self.composition.max_run_minutes * 60):
            raise ValueError("cursor_ttl_seconds must cover the complete reading run")
        for name, value, maximum in (
                ("exclusive_scan_max_batches", self.exclusive_scan_max_batches, 50),
                ("exclusive_continuation_max_batches", self.exclusive_continuation_max_batches, 10)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
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
            raise ProviderConsentRequiredError(self._policy.provider_policy_id)
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
        composition = self._policy.composition
        exclusive = self._is_exclusive_category(category_id)
        promotion: list[Mapping[str, object]] = []
        # The reading run: one per visit, at most one open per owner. The profile
        # is computed once at run open and FROZEN on the run row, so every page
        # inside the run is explainable afterwards from one stored version.
        run = self._open_run(owner, snapshot, composition)
        # ONE PAID RANKING PER VIEW, refreshes included. Keyed by (run,
        # eligibility): All, a category, a search and the language-exclusive
        # section are four different things to look at, and keying this by the
        # run alone made a category tap return the All page for up to an hour.
        eligibility_key = self._eligibility_key(category_id, query, exclusive,
            history_generation=snapshot.get("history_generation"),
            consent_revision=snapshot.get("consent_revision"))
        view = self._open_view(owner, run, eligibility_key)
        existing = self._existing_run_page(token, owner, view, snapshot, page_size)
        if existing is not None and existing is not _MISSING_FROZEN_ORDER:
            return existing
        if (composition is not None and view and view.get("frozen_order_id")
                and existing is not _MISSING_FROZEN_ORDER):
            # The order expired or became unusable after this run had already
            # bound a ranking. Binding is the durable proof that this view used
            # its one ranking, even when finalization produced no readable
            # cards. Buying another ranking would silently charge again. The
            # next reading run is the point at which fresh content may be bought.
            return self._run_end_response(snapshot, run, eligibility_key)
        # CLAIM BEFORE PAYING. Reusing an order the run has already bound closes
        # the refresh hole; it does not close the race, because a second request
        # arriving while the first is still in flight sees no bound order yet.
        # Exactly one caller wins this compare-and-set, and only the winner may
        # reserve, call the provider and bind.
        claim = self._claim_ranking(owner, run, eligibility_key, composition)
        if claim is not None and not claim.get("granted"):
            served = self._existing_run_page(token, owner,
                self._open_view(owner, run, eligibility_key), snapshot, page_size)
            if isinstance(served, Mapping):
                return served
            raise RankingInProgressError()
        claim_token = (claim or {}).get("token")
        try:
            return self._ranked(token, owner, run, view, eligibility_key, claim_token, snapshot,
                                composition, exclusive, category_id, query, page_size, body,
                                excluded_set, before_published, before_story, corpus_cursor)
        except Exception:
            # R7-1. Every raising path after the claim used to hold it for the
            # full TTL, so one provider failure made the next request wait a
            # minute for a claim nobody was using. The release is conditional on
            # still holding the token, so a request that already lost it releases
            # nothing.
            if claim_token and run and run.get("run_id"):
                self._release_claim(owner, run, eligibility_key, claim_token)
            raise

    def _ranked(self, token, owner, run, view, eligibility_key, claim_token, snapshot,
                composition, exclusive, category_id, query, page_size, body, excluded_set,
                before_published, before_story, corpus_cursor):
        promotion: list[Mapping[str, object]] = []
        hot_story_ids: set[str] = set()
        exclusive_consumed_rows: list[Mapping[str, object]] = []
        exclusive_has_more = False
        run_snapshot = run.get("profile_snapshot") if run else None
        same_epoch = (isinstance(run_snapshot, Mapping)
            and run_snapshot.get("schema_version") == 2
            and run_snapshot.get("_history_generation", snapshot.get("history_generation"))
                == snapshot.get("history_generation")
            and run_snapshot.get("_consent_revision", snapshot.get("consent_revision"))
                == snapshot.get("consent_revision"))
        # Clearing history or changing consent is an explicit privacy boundary.
        # The SQL run may remain open for its reading-hour audit, but a new epoch
        # must not reuse the old learned profile.
        profile = (BehaviorProfile.from_snapshot(run_snapshot) if same_epoch else
                   build_profile(snapshot, policy=composition, now=self._now())
                   if composition is not None else BehaviorProfile())
        # The run freezes affinity, but a new view inside it must honor a tap
        # recorded after run open. This also covers predeploy schema-1 runs.
        hidden_story_ids = (build_profile(snapshot, policy=composition, now=self._now()).hidden_story_ids
                            if composition is not None and composition.immediate_negative_filter else frozenset())
        # The language-exclusive section is served by the same M2 path: same
        # recipe, same pagination, same frozen order. Only the corpus narrows.
        if exclusive:
            rows, exclusive_consumed_rows, exclusive_has_more = self._exclusive_display_rows(
                query=query, before_published=before_published, before_story=before_story,
                target_count=self._policy.candidate_limit + 1,
                excluded_story_ids=excluded_set | hidden_story_ids,
                max_batches=self._policy.exclusive_scan_max_batches)
        elif composition is not None:
            rows, hot_story_ids, general_boundary, general_has_more = self._pool_rows(
                category_id, query, profile, composition, before_published, before_story,
                excluded_story_ids=excluded_set | hidden_story_ids, owner=owner)
            # Capped promotion: a few stories only the other language's press
            # carried get to compete for a place in All, on merit. They do NOT
            # get extra slots; they enter the same pool and take their own
            # lane's quota like any other candidate.
            promotion = [row for row in self._promotion_rows(
                query, composition, before_published, before_story)
                if row.get("story_id") not in hidden_story_ids]
            known = {row.get("story_id") for row in rows}
            rows = rows + [row for row in promotion if row.get("story_id") not in known]
        else:
            rows = self._store.retained_candidates(
                category_id=category_id, query=query, limit=self._policy.candidate_limit + len(excluded_set) + 1,
                before_published_at=before_published, before_story_id=before_story,
            )
        # Exclusivity is a property of the STORY, not of the request, so a
        # promoted card carries the same label in All that it carries in the
        # section. When the section itself is being served, every row has it.
        exclusive_ids = ({str(row["story_id"]) for row in rows} if exclusive
                         else {str(row["story_id"]) for row in promotion})
        filtered = ([row for row in rows if row.get("story_id") not in (excluded_set | hidden_story_ids)]
                    if not exclusive else rows)
        has_more = (exclusive_has_more if exclusive else
                    general_has_more if composition is not None else
                    len(filtered) > self._policy.candidate_limit)
        has_more = has_more or len(filtered) > self._policy.candidate_limit
        lane_diagnostics = LaneDiagnostics() if composition is not None else None
        opened_ids = (self._opened_candidate_ids(token, filtered, composition, profile, lane_diagnostics)
                      if composition is not None else set())
        opened_rows = [row for row in filtered if str(row["story_id"]) in opened_ids]
        filtered = [row for row in filtered if str(row["story_id"]) not in opened_ids]
        rows = filtered[:self._policy.candidate_limit]
        laned: tuple[LanedCandidate, ...] = ()
        if composition is not None:
            # THIS is the product: four labeled pools with quotas and caps. The
            # model only reorders what the recipe hands it.
            laned = build_window(filtered, profile=profile, policy=composition,
                                 now=self._now(), size=composition.candidate_window_size,
                                 diagnostics=lane_diagnostics)
            rows = [item.row for item in laned]
            has_more = has_more or len(filtered) > len(rows)
        next_corpus = None
        if composition is not None:
            # The SAME cursor shape the continuation writes, hot key included, so
            # the first "load more" resumes from a cursor of the shape it expects
            # rather than from a narrower one written by a different code path.
            if exclusive:
                cursor_rows = self._exclusive_safe_cursor_rows(
                    exclusive_consumed_rows, {str(row.get("story_id")) for row in rows},
                    excluded_set | opened_ids | hidden_story_ids)
                if has_more:
                    next_corpus = (self._exclusive_corpus_cursor(cursor_rows) if cursor_rows else
                                   {"before_published_at": before_published,
                                    "before_story_id": before_story})
            elif has_more:
                next_corpus = self._next_corpus_cursor(
                    rows + opened_rows, hot_story_ids, general_boundary=general_boundary)
        elif has_more and rows:
            boundary = rows[-1]
            next_corpus = {"before_published_at": boundary["published_at"],
                           "before_story_id": boundary["story_id"]}
        request_id = str(uuid.uuid4())
        request = self._request(request_id, owner, snapshot, rows, query)
        processing_allowed = bool(snapshot.get("learning_enabled") and snapshot.get("provider_processing_enabled"))
        observed_usage = {}
        attempts_started = 0
        settled_cost = 0.0
        prepared = None
        prepared_order = None
        reservation_created = False
        if composition is not None and self._policy.next_run_preparation_enabled:
            # This view is frozen before it is served. A ready result can be
            # consumed only from an earlier run; it never changes this run later.
            if processing_allowed and run and run.get("run_id"):
                consume_started = time.perf_counter()
                try:
                    earlier = self._store.consume_prepared_order(
                        user_id=owner.user_id, target_run_id=str(run["run_id"]),
                        eligibility_key=eligibility_key,
                        policy_digest=self._policy.effective_policy_digest,
                        candidate_ids=tuple(candidate.candidate_id for candidate in request.candidates),
                        minimum_overlap=self._policy.next_run_preparation_minimum_overlap)
                    prepared_order = select_prepared_order(earlier, owner_id=owner.user_id,
                        run_id=str(run["run_id"]), eligibility_key=eligibility_key,
                        policy_digest=self._policy.effective_policy_digest,
                        history_generation=request.history_generation,
                        consent_revision=request.consent_revision,
                        behavior_revision=snapshot.get("history_revision"),
                        provider_policy_id=self._policy.provider_policy_id,
                        provider_processing_enabled=processing_allowed,
                        candidate_ids=tuple(candidate.candidate_id for candidate in request.candidates),
                        minimum_overlap=self._policy.next_run_preparation_minimum_overlap,
                        now=self._clock())
                except Exception as error:
                    log_suppressed_exception("m2_prepared_order_unavailable", error,
                        stream=sys.stderr)
                finally:
                    _page_diagnostic({"event": "m2_preparation_stage_timing", "stage": "consume",
                        "duration_ms": round((time.perf_counter() - consume_started) * 1000, 3)})
            receipt = self._adapter.fallback(request,
                "next_run_preparation_pending" if processing_allowed else
                "provider_processing_consent_required")
            if prepared_order is not None:
                receipt = replace(receipt, ranked_candidate_ids=prepared_order,
                    result_mode=RankingResultMode.MODEL, fallback_reason="")
                validate_ranking_response(receipt, request)
        else:
            def record_usage(outcome, unknown_attempts, elapsed):
                observed_usage.update(input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                    unknown_attempts=unknown_attempts, provider_elapsed_seconds=elapsed)
            def record_attempt(attempt, elapsed):
                nonlocal attempts_started
                attempts_started += 1
            preparation_reason = ""
            if processing_allowed and request.candidates:
                prepared, preparation_reason = self._adapter.prepare_with_reason(request)
            estimate = None if prepared is None else self._adapter.reservation_estimate(
                estimated_input_tokens=prepared.input_tokens_bound, estimated_output_tokens=prepared.output_tokens_budget)
            reservation_created = estimate is not None and self._reserve(
                owner, request_id, estimate, run, eligibility_key, claim_token)
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
                        # An uncertain provider attempt retains its reservation.
                        settled_cost = 0.0
                except Exception:
                    # An ambiguous settlement retains the durable reservation.
                    raise
        latest = self._store.history_snapshot(token)
        self._assert_fresh(snapshot, latest)
        if (prepared_order is not None
                and latest.get("history_revision") != snapshot.get("history_revision")):
            # Consume proved compatibility at its own transaction. A later
            # positive tap should not waste a paid order; a negative or
            # unexplained change must still refuse that order before freeze.
            compatible = False
            try:
                compatible = self._store.prepared_history_is_compatible(
                    user_id=owner.user_id, history_generation=request.history_generation,
                    behavior_revision=snapshot.get("history_revision"))
            except Exception as error:
                log_suppressed_exception("m2_prepared_order_history_check_failed", error,
                    stream=sys.stderr)
            if not compatible:
                prepared_order = None
                receipt = self._adapter.fallback(request, "prepared_order_history_changed")
        owner_states = self._store.owner_states(token, [str(row["story_id"]) for row in rows])
        finalization = None
        if composition is not None:
            lane_by_id = {item.story_id: item for item in laned}
            ordered = [lane_by_id[story_id] for story_id in receipt.ranked_candidate_ids if story_id in lane_by_id]
            if not exclusive:
                # The cap is a CEILING, never a floor. The highest-ranked
                # promoted candidates keep their places and the rest step out,
                # so All is never padded with stories that did not earn a place.
                ordered = self._cap_promotions(ordered, exclusive_ids,
                                               composition.exclusive_promote_to_all_max)
            # The hard diversity pass. Deterministic, replayable, and applied to
            # the model's order rather than asked of the model.
            # The VISIBLE page and the finalization page are the same page.
            # When they differed, a short internal page shifted the boundary and
            # the first 25 cards the reader saw were a slice ACROSS two
            # quota-checked pages, which is how a 7/4/11/3 mix rendered as
            # 14/7/1/3. One number, used everywhere.
            page_size = min(page_size, composition.page_size)
            finalization = finalize_order(ordered, policy=composition, owner_states=owner_states,
                page_size=page_size, pages=composition.max_pages_per_run, profile=profile,
                diagnostics=lane_diagnostics)
            lane_diagnostics.emit("rank")
            if finalization.short_lane_reasons:
                # The operator signal for JJ's hourly review: which lane came up
                # short on which page, and which rung of the ladder was used.
                print(json.dumps({"event": "m2_page_shortfall", "request_id": request_id,
                    "short_lane_reasons": [dict(entry) for entry in finalization.short_lane_reasons]},
                    separators=(",", ":")), file=sys.stderr, flush=True)
            cards = [self._card(item.row, owner_states.get(item.story_id, {}), lane=item.lane,
                                composition=composition,
                                exclusive=item.story_id in exclusive_ids,
                                also_covered_by=finalization.also_covered_by.get(item.story_id, ()))
                     for item in finalization.cards]
        else:
            by_id = {str(row["story_id"]): self._card(row, owner_states.get(str(row["story_id"]), {})) for row in rows}
            cards = [by_id[story_id] for story_id in receipt.ranked_candidate_ids]
        pending_candidates = self._pending_after_finalization(
            filtered, ordered, finalization,
            {str(card.get("story_id")) for card in cards}, composition
        ) if composition is not None and finalization is not None else []
        pending_ids = {str(row.get("story_id")) for row in pending_candidates}
        pending_exclusive_story_ids = sorted(pending_ids & exclusive_ids)
        if pending_candidates and not exclusive:
            has_more = True
            if not next_corpus:
                # A short corpus has no older database keyset, but its bounded
                # unserved tail is still a complete continuation recipe.
                next_corpus = {"pending_only": True}
        expires_at = int(self._clock()) + self._policy.cursor_ttl_seconds
        bindings = self._bindings(receipt)
        bindings["order_origin"] = (
            "prepared_model" if prepared_order is not None else
            "direct_model" if receipt.result_mode.value == "model" else
            "recipe" if composition is not None else "freshness")
        if latest.get("history_revision") != snapshot.get("history_revision"):
            # F1. A behavior write landed during the provider call. The order was
            # paid for and is still the right order; rebinding it to the CURRENT
            # revision is what lets the epoch trigger accept it, so the money buys
            # a page instead of a discarded 409.
            bindings["server_commit_revision"] = latest.get("history_revision")
        if finalization is not None:
            bindings.update({"run_id": (run or {}).get("run_id"),
                # The run's FROZEN profile, carried on the order itself. A
                # continuation past the end of this order has to compose the
                # next cards from the same profile, and reading it from here
                # costs no round trip and cannot drift from what was ranked.
                "profile_snapshot": profile.as_snapshot(),
                "short_lane_reasons": [dict(entry) for entry in finalization.short_lane_reasons],
                "calibration_kl": finalization.calibration_kl,
                "calibration_alarm": finalization.calibration_alarm,
                # Including the backfill lane: a page counted as 25 while
                # reporting 7/4/11/3 was hiding where the other card came from.
                "lane_counts": {lane: sum(1 for item in finalization.cards if item.lane == lane)
                                for lane in (*composition.lane_priority, BACKFILL_LANE)}})
            event_groups = {item.story_id: str(item.row["event_group_id"])
                            for item in finalization.cards
                            if isinstance(item.row.get("event_group_id"), str)
                            and item.row.get("event_group_id")}
            if event_groups:
                bindings["event_group_ids"] = event_groups
        bindings.update({"eligibility": {"category": category_id, "query": query},
            "eligibility_key": eligibility_key,
            # Count responses the owner can actually read, not offset/page-size
            # arithmetic. A diversity-constrained view can legitimately return
            # a short page, and offset arithmetic let that view serve many more
            # than the configured number of responses.
            # An empty bounded scan is not a readable response. Start it below
            # offset zero so the first continuation that finds cards is not
            # mistaken for a free replay and must reserve response slot one.
            "responses_served": 1 if cards else 0,
            # Unlike last_served_next_offset, this does not move when later
            # responses are reserved. An old first-page cursor may replay only
            # within its originally served range.
            "initial_response_next_offset": min(page_size, len(cards)) if cards else None,
            "last_served_offset": 0 if cards else -1,
            "last_served_next_offset": min(page_size, len(cards)) if cards else 0,
            "corpus_cursor": next_corpus, "corpus_has_more": has_more,
            "pending_candidates": pending_candidates,
            "pending_exclusive_story_ids": pending_exclusive_story_ids,
            "corpus_start": dict(corpus_cursor), "excluded_story_ids": list(excluded_set),
            "execution": {**observed_usage, "settled_cost_usd": settled_cost,
                "attempts_started": attempts_started,
                "history_events_included": getattr(prepared, "history_events_included", 0) if prepared else 0,
                "history_events_omitted": getattr(prepared, "history_events_omitted", 0) if prepared else 0,
                # What the PROMPT BUDGET dropped, separate from what cost fitting
                # dropped: the operator reading a receipt can tell a configured
                # window from a corpus that outgrew its budget.
                "history_events_budget_omitted":
                    getattr(prepared, "history_events_budget_omitted", 0) if prepared else 0,
                "candidates_budget_omitted":
                    getattr(prepared, "candidates_budget_omitted", 0) if prepared else 0,
                "cost_basis": "observed_with_unknown_attempt_reserves" if observed_usage else
                    "unknown_provider_charge_reserved" if reservation_created and attempts_started else
                    "released_no_provider_call" if reservation_created else "no_provider_call",
                "newest_event_id": receipt.newest_event_id}})
        frozen_id = self._store.save_frozen_order(user_id=owner.user_id, request_id=request_id,
            bindings=bindings, cards=cards, page_size=page_size, expires_at=expires_at,
            run_id=(run or {}).get("run_id"))
        if run and run.get("run_id"):
            # The run now owns this ranking, so the next refresh is answered from
            # it rather than paying again. Conditional on still holding the
            # claim, and it releases the claim: a slow loser must not be able to
            # overwrite the winner's order after the fact.
            bound = self._store.bind_run_frozen_order(user_id=owner.user_id,
                run_id=str(run["run_id"]), eligibility_key=eligibility_key,
                frozen_order_id=frozen_id, token=claim_token)
            if claim_token and not bound:
                # The claim moved on while this request was working. Serving the
                # order it just wrote would be serving an order nothing is bound
                # to, and the reader would hold a cursor into a ranking the next
                # refresh will not find. The winner's order is the real one.
                self._abandon_unbound_order(owner, request_id, reservation_created, observed_usage)
                served = self._existing_run_page(token, owner,
                    self._open_view(owner, run, eligibility_key), latest, page_size)
                if isinstance(served, Mapping):
                    return served
                raise RankingInProgressError()
            if cards and self._reserve_response_slot(owner, str(run["run_id"]),
                    eligibility_key, frozen_id, 1, 0,
                    min(page_size, len(cards))) is None:
                raise RuntimeError("page_budget_unavailable")
        if (composition is not None and self._policy.next_run_preparation_enabled
                and processing_allowed and request.candidates and run and run.get("run_id")):
            # Enqueue after the current order and first response are durable.
            # A queue failure never delays or mutates the page being read.
            enqueue_started = time.perf_counter()
            try:
                queued = self._store.enqueue_prepared_order(user_id=owner.user_id,
                    source_run_id=str(run["run_id"]), eligibility_key=eligibility_key,
                    policy_digest=self._policy.effective_policy_digest,
                    history_generation=request.history_generation,
                    consent_revision=request.consent_revision,
                    behavior_revision=request.server_commit_revision,
                    provider_policy_id=self._policy.provider_policy_id,
                    request_id=request_id, request_payload=request_to_payload(request),
                    ttl_seconds=self._policy.next_run_preparation_ttl_seconds)
                if not queued:
                    _page_diagnostic({"event": "m2_preparation_enqueue_rejected"})
            except Exception as error:
                log_suppressed_exception("m2_preparation_enqueue_failed", error,
                    stream=sys.stderr)
            finally:
                _page_diagnostic({"event": "m2_preparation_stage_timing", "stage": "enqueue",
                    "duration_ms": round((time.perf_counter() - enqueue_started) * 1000, 3)})
        next_cursor = (self._cursor(frozen_id, min(page_size, len(cards)), expires_at,
                                    response_number=2 if cards else 1)
                       if page_size < len(cards) or has_more else None)
        return self._page_response(bindings, cards[:page_size], next_cursor, receipt)

    def page(self, *, authorization: str, cursor: str) -> dict[str, object]:
        started = time.perf_counter()
        stages: dict[str, float] = {}
        try:
            return self._page_with_timing(authorization=authorization, cursor=cursor,
                                          stages=stages)
        finally:
            # One record per request keeps concurrent page turns separable
            # without logging a user, cursor, story, query or request id.
            # Diagnostic failure must not change a page result or mask its error.
            _page_diagnostic({"event": "m2_page_stage_timing", "route": "/page",
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
                "stages_ms": stages})

    def _page_with_timing(self, *, authorization: str, cursor: str,
                          stages: dict[str, float]) -> dict[str, object]:
        if not self._policy.enabled:
            raise RuntimeError("ranking_disabled")
        with _page_stage(stages, "authenticate"):
            token, owner = self._authenticate(authorization)
        with _page_stage(stages, "cursor_decode"):
            payload = self._decode_cursor(cursor)
        with _page_stage(stages, "frozen_load"):
            frozen = self._store.load_frozen_order(user_id=owner.user_id, frozen_order_id=str(payload["frozen_order_id"]))
        if not frozen or int(frozen["expires_at"]) < int(self._clock()):
            raise StaleRankingError("cursor_expired")
        with _page_stage(stages, "history_load"):
            current = self._store.history_snapshot(token)
        current_bindings = {"history_generation": current.get("history_generation"),
            "consent_revision": current.get("consent_revision"),
            "server_commit_revision": current.get("history_revision")}
        for key in ("history_generation", "consent_revision"):
            if current_bindings[key] != frozen["bindings"].get(key):
                raise StaleRankingError(f"changed_{key}")
        # F7. A behavior event inside a reading run no longer re-ranks. Reading a
        # story and then pressing "load more" used to mint a new request id and a
        # new PAID provider call, and the page the reader was on moved under her.
        # A page turn is now a slice of the frozen order and costs nothing.
        if frozen["bindings"].get("result_mode") == "model" and not current.get("provider_processing_enabled"):
            raise StaleRankingError("consent_disabled")
        offset = int(payload["offset"])
        cards = list(frozen["cards"])
        size = int(frozen.get("page_size", self._policy.maximum_page_size))
        composition = self._policy.composition
        bindings = frozen.get("bindings") or {}
        run_id = bindings.get("run_id")
        eligibility_key = bindings.get("eligibility_key")
        selected_category = (bindings.get("eligibility") or {}).get("category")
        response_number = payload.get("response_number")
        legacy_cursor = response_number is None
        if legacy_cursor and composition is not None:
            # The atomic response budget cannot prove the offset represented by
            # a pre-cutover cursor. Refuse it so an old tab refreshes through
            # rank() onto the versioned eligibility view instead of spending
            # additional content against a legacy page counter.
            raise StaleRankingError("cursor_version")
        if legacy_cursor:
            # The non-composed legacy policy has no per-run response budget.
            response_number = max(1, offset // max(size, 1) + 1)
        if (not isinstance(response_number, int) or isinstance(response_number, bool)
                or response_number < 1 or response_number > 1000):
            raise StaleRankingError("invalid_cursor")
        if composition is not None and size > 0:
            # The response ordinal is signed into the cursor, so a short page or
            # bounded empty scan cannot distort the budget. Empty scans retain
            # the same ordinal; only a readable response reserves it.
            if response_number > composition.max_pages_per_run:
                # The run has served every page it promises. Reaching further
                # would keep returning older and older stories that met no pool's
                # rule, so the honest answer is that this run is over.
                return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                        "cards": [], "next_cursor": None, "end_of_run": True}
        continuation_pending = False
        continuation_offsets = (frozen.get("bindings") or {}).get("continuation_offsets")
        event_group_ids = (frozen.get("bindings") or {}).get("event_group_ids")
        preview, preview_end, _preview_removed = self._slice(
            cards, offset, size, current,
            continuation_offsets=continuation_offsets,
            event_group_ids=event_group_ids, selected_category=selected_category)
        continuation_needed = (offset >= len(cards)
            or (composition is not None and len(preview) < size
                and preview_end >= len(cards)))
        if continuation_needed and frozen["bindings"].get("corpus_has_more"):
            # F7, the branch that used to re-rank. Inside a reading run there is
            # never a second provider call: load more browses OLDER news, in the
            # same deterministic recipe order, with the same pools and labels and
            # no model. The new cards are APPENDED to this frozen order, so every
            # already-signed cursor keeps pointing at the same card.
            if self._policy.composition is not None:
                if (not run_id or not isinstance(eligibility_key, str)
                        or re.fullmatch(r"[0-9a-f]{64}", eligibility_key) is None):
                    raise RuntimeError("page_budget_unavailable")
                with _page_stage(stages, "claim"):
                    continuation_token, claimed_snapshot = self._claim_continuation(
                        owner, str(run_id), eligibility_key, composition,
                        str(payload["frozen_order_id"]))
                try:
                    # The first snapshot was loaded before the lock. Reload
                    # inside it: a caller that waited for another continuation
                    # must observe the batch that caller already appended rather
                    # than append the same corpus cursor again.
                    # One corpus window can append fewer than a readable page
                    # while older candidates remain. Refill under the SAME
                    # continuation claim before reserving this response slot.
                    # Re-read the persisted order after each append so the
                    # second pass sees its cursor, pending rows and boundaries.
                    terminal_pending = None
                    complete_snapshot = None
                    for attempt in range(composition.continuation_refill_max_passes):
                        if attempt == 0 and claimed_snapshot is not None:
                            locked = claimed_snapshot
                        else:
                            with _page_stage(stages, "locked_load"):
                                locked = self._store.load_frozen_order(user_id=owner.user_id,
                                    frozen_order_id=str(payload["frozen_order_id"]))
                        if not locked or int(locked["expires_at"]) < int(self._clock()):
                            raise StaleRankingError("cursor_expired")
                        locked_cards = list(locked["cards"])
                        locked_bindings = locked.get("bindings") or {}
                        locked_preview, locked_end, _locked_removed = self._slice(
                            locked_cards, offset, size, current,
                            continuation_offsets=locked_bindings.get("continuation_offsets"),
                            event_group_ids=locked_bindings.get("event_group_ids"),
                            selected_category=selected_category)
                        needs_more = (offset >= len(locked_cards)
                            or (len(locked_preview) < size and locked_end >= len(locked_cards)))
                        if not needs_more or not locked_bindings.get("corpus_has_more"):
                            # Reuse only a complete persisted later page. Its
                            # atomic response reservation still rejects an order
                            # deleted by a concurrent privacy change. The first
                            # response can bypass that gate, so keep its reread.
                            if (not needs_more and len(locked_preview) == size
                                    and response_number > 1 and offset > 0):
                                complete_snapshot = locked
                            break
                        # Explicit exclusions plus already-frozen cards can
                        # require far more than two general-pool RPCs. A second
                        # such scan would exceed the claim's transport budget.
                        # Leave this response ordinal unspent for a retry.
                        excluded_count = len({str(card.get("story_id")) for card in locked_cards}
                            | {str(story_id) for story_id in
                               (locked_bindings.get("excluded_story_ids") or ())})
                        general_budget = max(composition.pool_scan_max_batches,
                            (excluded_count + composition.candidate_window_size
                             + composition.page_size + 99) // 100)
                        if attempt and general_budget > composition.pool_scan_max_batches:
                            break
                        with _page_stage(stages, "continuation_pass"):
                            added, continuation_pending, appended_snapshot = self._continue_frozen_order(
                                token, owner, locked, str(payload["frozen_order_id"]), size,
                                page_prefix=locked_preview)
                        # Only a fully persisted later page can reuse the append
                        # result. Privacy deletion and response-budget races still
                        # fail in the atomic response reservation below.
                        if (claimed_snapshot is not None and appended_snapshot is not None
                                and response_number > 1 and offset > 0):
                            appended_preview, _, _ = self._slice(
                                appended_snapshot["cards"], offset, size, current,
                                continuation_offsets=appended_snapshot["bindings"].get("continuation_offsets"),
                                event_group_ids=appended_snapshot["bindings"].get("event_group_ids"),
                                selected_category=selected_category)
                            if len(appended_preview) == size:
                                complete_snapshot = appended_snapshot
                                break
                        # An empty bounded scan preserves the same response
                        # ordinal for the caller to retry. Do not turn it into
                        # an unbounded search inside this one request.
                        if not added:
                            if (continuation_pending
                                    and not self._is_exclusive_category(
                                        (locked_bindings.get("eligibility") or {}).get("category"))
                                    and attempt + 1 < composition.continuation_refill_max_passes
                                    and general_budget <= composition.pool_scan_max_batches):
                                with _page_stage(stages, "locked_load"):
                                    advanced = self._store.load_frozen_order(
                                        user_id=owner.user_id,
                                        frozen_order_id=str(payload["frozen_order_id"]))
                                advanced_bindings = ((advanced or {}).get("bindings") or {})
                                if (advanced_bindings.get("corpus_cursor")
                                        != locked_bindings.get("corpus_cursor")
                                        or advanced_bindings.get("pending_candidates")
                                        != locked_bindings.get("pending_candidates")):
                                    # Zero admitted cards can still retire a
                                    # blocked corpus head. A second bounded
                                    # scan may find its legal replacement.
                                    continue
                            terminal_pending = continuation_pending
                            break
                    if complete_snapshot is not None:
                        frozen = complete_snapshot
                    else:
                        with _page_stage(stages, "final_load"):
                            frozen = self._store.load_frozen_order(user_id=owner.user_id,
                                frozen_order_id=str(payload["frozen_order_id"]))
                    if not frozen or int(frozen["expires_at"]) < int(self._clock()):
                        raise StaleRankingError("cursor_expired")
                    cards = list(frozen["cards"])
                    continuation_pending = (bool(frozen["bindings"].get("corpus_has_more"))
                                            if terminal_pending is None else terminal_pending)
                finally:
                    with _page_stage(stages, "claim_release"):
                        self._release_claim(owner, {"run_id": str(run_id)}, eligibility_key,
                                            continuation_token)
                frozen["bindings"]["corpus_has_more"] = continuation_pending
            else:
                # The documented rollback: with no recipe configured there is no
                # deterministic continuation to fall back on, so the pre-Phase-2
                # behavior stands unchanged rather than silently ending the feed.
                return self.rank(authorization=authorization, body={**current_bindings,
                    "history_revision": current.get("included_history_revision", 0),
                    "eligibility": frozen["bindings"].get("eligibility", {}), "exclude_story_ids": [],
                    "corpus_cursor": frozen["bindings"].get("corpus_cursor"), "page_size": size})
        visible, next_offset, removed = self._slice(
            cards, offset, size, current,
            continuation_offsets=(frozen.get("bindings") or {}).get("continuation_offsets"),
            event_group_ids=(frozen.get("bindings") or {}).get("event_group_ids"),
            selected_category=selected_category)
        if (composition is not None and continuation_pending and len(visible) < size
                and (frozen.get("bindings") or {}).get("corpus_scan_has_more")):
            # Persisted append(s) remain available for this same signed offset.
            # Do not spend a readable response on a partial page while the
            # bounded corpus scan says there may be older eligible stories.
            return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                    "cards": [], "next_cursor": self._cursor(
                        str(payload["frozen_order_id"]), offset, int(frozen["expires_at"]),
                        response_number=response_number)}
        with _page_stage(stages, "owner_overlay"):
            visible = self._overlay_owner_states(token, visible)
        with _page_stage(stages, "filtered_record"):
            self._record_filtered(owner, frozen, removed)
        next_cursor = self._cursor(str(payload["frozen_order_id"]), next_offset, int(frozen["expires_at"])) if next_offset < len(cards) else None
        if offset >= len(cards) and continuation_pending:
            return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                    "cards": [], "next_cursor": self._cursor(
                        str(payload["frozen_order_id"]), offset, int(frozen["expires_at"]),
                        response_number=response_number)}
        if offset >= len(cards):
            # End of the run, said explicitly rather than as an empty page that
            # looks like a failure. There is nothing more to serve from this
            # frozen order, and a new reading run is what brings new stories.
            return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                    "cards": [], "next_cursor": None, "end_of_run": True}
        # An empty filtered slice is not a readable response. Keep its signed
        # ordinal so the first older card that survives the filter reserves the
        # slot instead of skipping it and failing the strict page budget.
        next_response_number = response_number + 1 if visible else response_number
        if not visible and composition is not None:
            # Moving an already-spent ordinal to a later offset would authorize
            # an extra unique page as an "idempotent replay". The row-locked
            # view is the authority on whether this ordinal is still unspent.
            view = self._open_view(owner, {"run_id": str(run_id)}, eligibility_key)
            served = view.get("pages_served") if isinstance(view, Mapping) else None
            if (not isinstance(served, int) or isinstance(served, bool) or served < 0):
                return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                        "cards": [], "next_cursor": None, "end_of_run": True}
            if served >= response_number:
                next_response_number = max(served, response_number) + 1
        if next_cursor is None and frozen["bindings"].get("corpus_has_more"):
            next_cursor = self._cursor(str(payload["frozen_order_id"]), len(cards),
                                       int(frozen["expires_at"]),
                                       response_number=next_response_number)
        elif next_cursor is not None:
            next_cursor = self._cursor(str(payload["frozen_order_id"]), next_offset,
                                       int(frozen["expires_at"]),
                                       response_number=next_response_number)
        # The original first slice may replay, even after later pages. An
        # initially empty order has no such range until its first reservation.
        # Feedback can make an offset-zero cursor scan into new cards; that
        # must not bypass the response reservation.
        bindings = frozen.get("bindings", {})
        served_responses = bindings.get("responses_served")
        first_end = bindings.get("initial_response_next_offset")
        first_response_replay = (response_number == 1 and offset == 0
                                 and ((type(first_end) is int and first_end == next_offset)
                                      or (first_end is None and served_responses == 1
                                          and bindings.get("last_served_offset") == 0
                                          and bindings.get("last_served_next_offset") == next_offset)))
        if visible and composition is not None and not first_response_replay:
            reservation = None
            if (run_id and isinstance(eligibility_key, str)
                    and re.fullmatch(r"[0-9a-f]{64}", eligibility_key) is not None):
                with _page_stage(stages, "response_reserve"):
                    reservation = self._reserve_response_slot(
                        owner, str(run_id), eligibility_key,
                        str(payload["frozen_order_id"]), response_number,
                        offset, next_offset)
            if reservation is None:
                # The row-locked RPC is the authority. A concurrent cursor that
                # lost this slot cannot serve another readable response even if
                # it loaded the frozen bindings before the winner committed.
                return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                        "cards": [], "next_cursor": None, "end_of_run": True}
            frozen["bindings"].update({"responses_served": response_number,
                                       "last_served_offset": offset,
                                       "last_served_next_offset": next_offset})
        return {"schema_version": 1, **self._public_bindings(frozen["bindings"]), "cards": visible, "next_cursor": next_cursor}

    def _continue_frozen_order(self, token, owner, frozen, frozen_order_id, size,
                               *, page_prefix=()):
        """Older news, composed by the recipe alone. No provider call, ever.

        Returns the cards appended and whether another bounded continuation may
        exist. Any failure here degrades to "no more cards" rather than to a
        paid ranking: a page turn that quietly bills is the bug being fixed.
        """
        started_at = time.perf_counter()
        pool_ms = None
        composition = self._policy.composition
        bindings = frozen.get("bindings", {})
        cursor = bindings.get("corpus_cursor") or {}
        if composition is None or not isinstance(cursor, Mapping):
            return (), False, None
        lane_diagnostics = LaneDiagnostics()
        eligibility = bindings.get("eligibility") or {}
        category_id = eligibility.get("category") if isinstance(eligibility, Mapping) else None
        query = eligibility.get("query") if isinstance(eligibility, Mapping) else None
        profile = BehaviorProfile.from_snapshot(bindings.get("profile_snapshot"))
        hidden_story_ids = (profile.hidden_story_ids
                            if composition.immediate_negative_filter else frozenset())
        seen = {str(card.get("story_id")) for card in frozen.get("cards", ())}
        original_exclusions = {str(story_id) for story_id in
                               (bindings.get("excluded_story_ids") or ())}
        exclusive = self._is_exclusive_category(category_id)
        pending = self._load_pending_candidates(bindings, composition)
        pending_exclusive_story_ids = self._load_pending_exclusive_story_ids(
            bindings, pending, composition)
        if not cursor and (exclusive or not pending):
            return (), False, None
        if exclusive:
            pooled, cursor_rows, fetched_more = self._exclusive_display_rows(
                query=query, before_published=cursor.get("before_published_at"),
                before_story=cursor.get("before_story_id"),
                target_count=self._policy.candidate_limit + 1,
                excluded_story_ids=seen | original_exclusions | hidden_story_ids,
                max_batches=self._policy.exclusive_continuation_max_batches)
            hot_story_ids: set[str] = set()
            used_pending = False
        else:
            pending_only = bool(pending) and (
                not cursor or cursor.get("pending_only") is True)
            if pending_only:
                fetched, hot_story_ids = [], set()
                pooled, cursor_rows, fetched_more = pending, [], False
            else:
                pool_started_at = time.perf_counter()
                fetched, hot_story_ids, general_boundary, general_has_more = self._pool_rows(
                    category_id, query, profile, composition,
                    cursor.get("before_published_at"), cursor.get("before_story_id"),
                    self._hot_cursor(cursor), excluded_story_ids=seen | original_exclusions,
                    owner=owner)
                pool_ms = round((time.perf_counter() - pool_started_at) * 1000)
                known = {str(row.get("story_id")) for row in pending}
                pooled = pending + [row for row in fetched
                                    if str(row.get("story_id")) not in known]
                cursor_rows = fetched
                fetched_more = general_has_more
            used_pending = bool(pending)
        event_groups = dict(bindings.get("event_group_ids") or {})
        eligible_rows = [row for row in pooled
                         if str(row.get("story_id")) not in (seen | original_exclusions)
                         and not (exclusive and self._suppressed_by_profile(
                             row, profile, composition, selected_category=category_id))]
        rows = self._exclude_frozen_duplicates(
            eligible_rows, frozen.get("cards", ()), event_groups)
        retained_ids = {str(row.get("story_id")) for row in rows}
        semantic_drop_ids = {str(row.get("story_id")) for row in eligible_rows
                             if str(row.get("story_id")) not in retained_ids}
        opened_ids = self._opened_candidate_ids(token, rows, composition, profile, lane_diagnostics)
        rows = [row for row in rows if str(row["story_id"]) not in opened_ids]
        if rows or semantic_drop_ids:
            composition_now = self._now()
            lane_diagnostics.record("frozen_duplicate_removed", (
                assign_lane(row, profile=profile, policy=composition, now=composition_now)[0]
                for row in eligible_rows if str(row.get("story_id")) in semantic_drop_ids))
        laned = build_window(rows, profile=profile, policy=composition, now=composition_now,
                             size=composition.candidate_window_size,
                             diagnostics=lane_diagnostics) if rows else ()
        if laned and not exclusive and pending_exclusive_story_ids:
            prefix_promotions = sum(
                1 for card in page_prefix if card.get("exclusive_label"))
            laned = self._cap_promotions(
                laned, pending_exclusive_story_ids,
                max(0, composition.exclusive_promote_to_all_max - prefix_promotions))
        owner_states_started_at = time.perf_counter()
        owner_states = self._store.owner_states(token, [item.story_id for item in laned]) if laned else {}
        owner_states_ms = round((time.perf_counter() - owner_states_started_at) * 1000)
        finalization = finalize_order(laned, policy=composition, owner_states=owner_states,
            page_size=min(size, composition.page_size), pages=composition.max_pages_per_run,
            profile=profile, diagnostics=lane_diagnostics) if laned else None
        added = [self._card(item.row, owner_states.get(item.story_id, {}), lane=item.lane,
                            composition=composition,
                            exclusive=exclusive or item.story_id in pending_exclusive_story_ids,
                            also_covered_by=finalization.also_covered_by.get(item.story_id, ()))
                 for item in (finalization.cards if finalization else ())]
        for item in (finalization.cards if finalization else ()):
            group = item.row.get("event_group_id")
            if isinstance(group, str) and group:
                event_groups[item.story_id] = group
        unreflowed = added
        added = self._align_continuation(
            frozen.get("cards", ()), added, size, composition, event_groups,
            page_prefix=page_prefix, defer_on_collision=not exclusive)
        lane_diagnostics.record("append_selected", (card["lane"] for card in added))
        lane_diagnostics.emit("continuation")
        alignment_deferred_ids = ({str(card.get("story_id")) for card in unreflowed}
                                  - {str(card.get("story_id")) for card in added})
        if event_groups:
            bindings["event_group_ids"] = event_groups
        if finalization is not None:
            deferred_pending = self._pending_after_finalization(
                rows, laned, finalization,
                {str(card.get("story_id")) for card in added}, composition)
            if alignment_deferred_ids:
                # A candidate the finalizer admitted but the stitched page
                # could not legally place is pending, not hard-dropped. The
                # next older scan can supply a separating story.
                deferred_pending = self._pending_candidates(
                    list(deferred_pending) + [row for row in rows
                        if str(row.get("story_id")) in alignment_deferred_ids],
                    {str(card.get("story_id")) for card in added}, composition)
        else:
            deferred_pending = self._pending_candidates(
                rows, {str(card.get("story_id")) for card in added}, composition)
        if exclusive:
            safe_rows = self._exclusive_safe_cursor_rows(
                cursor_rows, {item.story_id for item in laned},
                seen | original_exclusions | semantic_drop_ids | opened_ids,
)
            next_corpus = (self._exclusive_corpus_cursor(safe_rows) if safe_rows else dict(cursor))
            remaining_pending = []
        elif used_pending:
            next_corpus = (self._next_corpus_cursor(
                cursor_rows, hot_story_ids, general_boundary=general_boundary)
                           if cursor_rows else dict(cursor))
            if "hot" not in next_corpus and isinstance(cursor.get("hot"), Mapping):
                next_corpus["hot"] = dict(cursor["hot"])
            remaining_pending = deferred_pending
        else:
            next_corpus = self._next_corpus_cursor(
                cursor_rows, hot_story_ids, general_boundary=general_boundary)
            if "hot" not in next_corpus and isinstance(cursor.get("hot"), Mapping):
                next_corpus["hot"] = dict(cursor["hot"])
            remaining_pending = deferred_pending
        remaining_pending_ids = {str(row.get("story_id")) for row in remaining_pending}
        remaining_pending_exclusive_ids = sorted(
            remaining_pending_ids & pending_exclusive_story_ids)
        if exclusive:
            more = fetched_more or len(rows) > len(added)
        else:
            more = fetched_more or bool(remaining_pending)
        previous_total = len(frozen.get("cards", ()))
        continuation_offsets = list(bindings.get("continuation_offsets") or ())
        if added and previous_total not in continuation_offsets:
            continuation_offsets.append(previous_total)
            bindings["continuation_offsets"] = continuation_offsets
        continuation_bindings = {
            "corpus_cursor": next_corpus,
            "corpus_has_more": more,
            "corpus_scan_has_more": fetched_more,
            "pending_candidates": remaining_pending,
            "pending_exclusive_story_ids": remaining_pending_exclusive_ids,
            "continuation_mode": "recipe_only",
        }
        if event_groups:
            continuation_bindings["event_group_ids"] = event_groups
        if continuation_offsets:
            continuation_bindings["continuation_offsets"] = continuation_offsets
        # The return is the whole point: the RPC refuses to grow an order past
        # its cap and returns 0, and a transport failure raises. Serving cards
        # this store did not accept would show her the same stories again on the
        # next page turn, and letting the error out would 500 a page turn.
        extend_started_at = time.perf_counter()
        try:
            total = self._store.extend_frozen_order(user_id=owner.user_id,
                frozen_order_id=frozen_order_id, cards=added,
                bindings=continuation_bindings)
        except Exception as error:
            _page_suppressed_exception("m2_continuation_failed", error,
                reason="store_unavailable", frozen_order_id=frozen_order_id)
            return (), False, None
        _page_diagnostic({"event": "m2_continuation_pass_timing",
            "pass_total_ms": round((time.perf_counter() - started_at) * 1000),
            "pool_ms": pool_ms,
            "owner_states_ms": owner_states_ms,
            "extend_ms": round((time.perf_counter() - extend_started_at) * 1000),
            "pool_rows": len(pooled), "added_cards": len(added)})
        if (not isinstance(total, int) or total < previous_total
                or (added and total <= previous_total)):
            # The order did not grow: it has reached its cap, or the row was not
            # matched. Either way this run is over, and saying so is better than
            # silently repeating the page she just read.
            _page_diagnostic({"event": "m2_continuation_exhausted", "reason": "order_at_capacity"})
            return (), False, None
        accepted_snapshot = None
        if added and total == previous_total + len(added):
            accepted_snapshot = {**frozen,
                "cards": list(frozen.get("cards", ())) + added,
                "bindings": {**bindings, **continuation_bindings}}
        return tuple(added), more, accepted_snapshot

    def _record_run_page(self, owner, run_id, eligibility_key, pages):
        """This view's page high-water mark before this page. Never fails a page."""
        try:
            previous = self._store.record_run_page(user_id=owner.user_id, run_id=run_id,
                                                   eligibility_key=eligibility_key, pages=pages)
        except Exception as error:
            log_suppressed_exception("m2_page_budget_unavailable", error, stream=sys.stderr,
                run_id=run_id)
            return 0
        return previous if isinstance(previous, int) and not isinstance(previous, bool) else 0

    def _reserve_response_slot(self, owner, run_id, eligibility_key, frozen_order_id,
                               response_number, offset, next_offset):
        """Reserve or replay one signed readable-response ordinal.

        The database locks the view row and updates its high-water mark plus the
        frozen order's offset in one transaction. ``previous == ordinal - 1``
        reserves a new response; ``previous == ordinal`` is an exact replay.
        Older, skipped, or same-ordinal/different-offset requests fail closed.
        """
        try:
            result = self._store.reserve_run_response(user_id=owner.user_id, run_id=run_id,
                eligibility_key=eligibility_key, frozen_order_id=frozen_order_id,
                response_number=response_number, offset=offset, next_offset=next_offset)
        except Exception as error:
            _page_suppressed_exception("m2_page_budget_unavailable", error,
                run_id=run_id)
            raise RuntimeError("page_budget_unavailable") from error
        if not isinstance(result, Mapping) or not isinstance(result.get("reserved"), bool):
            raise RuntimeError("page_budget_unavailable")
        if result.get("reserved") is not True:
            return None
        previous = result.get("previous")
        if not isinstance(previous, int) or isinstance(previous, bool):
            raise RuntimeError("page_budget_unavailable")
        return previous

    def _run_end_response(self, snapshot, run, eligibility_key):
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
            f"news-curator:{(run or {}).get('run_id')}:{eligibility_key}:end"))
        bindings = {"request_id": request_id,
            "policy_version": self._policy.policy_version,
            "model_version": self._policy.model_version,
            "history_revision": snapshot.get("included_history_revision", 0),
            "history_generation": snapshot.get("history_generation"),
            "consent_revision": snapshot.get("consent_revision"),
            "server_commit_revision": snapshot.get("history_revision"),
            "result_mode": "fallback", "fallback_reason": "run_page_budget_exhausted",
            "order_origin": "recipe" if self._policy.composition is not None else "freshness"}
        return {"schema_version": 1, **bindings, "cards": [], "next_cursor": None,
                "end_of_run": True}

    def _record_filtered(self, owner, frozen, removed):
        """Persist what "less like this" removed, so the page replays.

        Without this the filter is recomputed from whatever the profile happens
        to be at read time, and a page reviewed an hour later cannot be told
        apart from a page that never had those cards. It is also what makes
        `reading-pages --hour` honest about what she actually saw.
        """
        run_id = (frozen.get("bindings") or {}).get("run_id")
        if not run_id or not removed:
            return
        try:
            # Recorded against the run the page was SERVED FROM, even when that
            # run has since closed. A run closing mid-visit used to open a window
            # where the filter still applied and nothing was written down, so a
            # page reviewed later could not be told apart from one that never had
            # those cards.
            recorded = self._store.record_reading_run_filter(user_id=owner.user_id,
                run_id=str(run_id), story_ids=sorted(removed))
            if not isinstance(recorded, int) or recorded <= 0:
                _page_diagnostic({"event": "m2_filter_not_recorded", "run_id": str(run_id)})
        except Exception as error:
            # A page must render even when the audit write fails. The filter
            # itself already happened; this only records it.
            _page_suppressed_exception("m2_filter_record_failed", error,
                run_id=str(run_id))

    def _opened_candidate_ids(self, token, rows, composition, profile, diagnostics):
        if not composition.hide_already_opened or not rows:
            return set()
        # This snapshot only protects admission capacity. The authoritative
        # post-provider state/freshness reads remain mandatory for races.
        opened = self._store.opened_candidate_ids(token, [str(row["story_id"]) for row in rows])
        now = self._now()
        diagnostics.record("admission_opened_removed", (
            assign_lane(row, profile=profile, policy=composition, now=now)[0]
            for row in rows if str(row["story_id"]) in opened))
        return opened

    @staticmethod
    def _hot_cursor(cursor):
        """The hot lane's keyset out of a stored corpus cursor, or None.

        All three parts or none: the SQL refuses half a keyset, because half a
        keyset silently drops rows at the page boundary.
        """
        hot = cursor.get("hot") if isinstance(cursor, Mapping) else None
        if not isinstance(hot, Mapping):
            return None
        count = hot.get("before_source_count")
        published, story = hot.get("before_published_at"), hot.get("before_story_id")
        if not isinstance(count, int) or isinstance(count, bool) or not published or not story:
            return None
        return (published, story, count)

    @staticmethod
    def _pending_candidate_limit(composition):
        """Policy-derived ceiling for unserved rows held by one reading run."""
        quotas = lane_window_quotas(composition, composition.candidate_window_size)
        fetched_per_round = composition.candidate_window_size + composition.page_size
        fetched_per_round += sum(
            RankingService._lane_fetch_limit(quotas[lane]) for lane in composition.lane_priority)
        promotion_cap = composition.exclusive_promote_to_all_max
        fetched_per_round += max(promotion_cap * 4, promotion_cap)
        unserved_per_round = max(0, fetched_per_round - composition.candidate_window_size)
        return unserved_per_round * composition.max_pages_per_run

    @staticmethod
    def _lane_fetch_limit(quota):
        return min(100, max(quota * 3, 10))

    @classmethod
    def _pending_candidates(cls, rows, served_ids, composition):
        """Bounded fetched rows that a frozen order has not served yet.

        The corpus cursor advances past the whole over-fetch. Keeping its
        unserved tail on the same frozen order prevents those rows from falling
        behind that cursor while preserving stable signed offsets.
        """
        pending = []
        seen = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            story_id = row.get("story_id")
            if not isinstance(story_id, str) or story_id in served_ids or story_id in seen:
                continue
            pending.append(dict(row))
            seen.add(story_id)
        if len(pending) > cls._pending_candidate_limit(composition):
            raise RuntimeError("pending_candidate_limit_exceeded")
        return pending

    @classmethod
    def _pending_after_finalization(cls, rows, offered, finalization, served_ids,
                                    composition):
        """Keep deferred rows without resurrecting a finalizer hard drop."""
        offered_ids = {item.story_id for item in offered}
        remaining_ids = {item.story_id for item in finalization.remaining}
        eligible = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            story_id = str(row.get("story_id"))
            if story_id not in offered_ids or story_id in remaining_ids:
                eligible.append(row)
        return cls._pending_candidates(eligible, served_ids, composition)

    @staticmethod
    def _semantic_duplicate_keys(row, event_group=None):
        canonical = dict(row)
        if not canonical.get("canonical_url") and canonical.get("url"):
            canonical["canonical_url"] = canonical["url"]
        if event_group:
            canonical["event_group_id"] = event_group
        candidate = LanedCandidate(
            story_id=str(canonical.get("story_id", "")), lane=BACKFILL_LANE,
            lane_score=0.0, row=canonical)
        return _duplicate_keys(candidate)

    @classmethod
    def _exclude_frozen_duplicates(cls, rows, frozen_cards, event_groups):
        """Drop only candidates duplicating an already frozen card.

        Duplicates within ``rows`` still reach the finalizer together, where
        their multi-outlet coverage is attached to the surviving card.
        """
        frozen_keys = set()
        groups = event_groups if isinstance(event_groups, Mapping) else {}
        for card in frozen_cards:
            if not isinstance(card, Mapping):
                continue
            frozen_keys.update(cls._semantic_duplicate_keys(
                card, groups.get(str(card.get("story_id")))))
        return [row for row in rows if isinstance(row, Mapping)
                and frozen_keys.isdisjoint(cls._semantic_duplicate_keys(row))]

    @classmethod
    def _load_pending_candidates(cls, bindings, composition):
        raw = bindings.get("pending_candidates", ()) if isinstance(bindings, Mapping) else ()
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise RuntimeError("invalid_pending_candidates")
        if len(raw) > cls._pending_candidate_limit(composition):
            raise RuntimeError("invalid_pending_candidates")
        pending = []
        seen = set()
        for row in raw:
            if not isinstance(row, Mapping):
                raise RuntimeError("invalid_pending_candidates")
            story_id = row.get("story_id")
            if not isinstance(story_id, str) or story_id in seen:
                raise RuntimeError("invalid_pending_candidates")
            pending.append(dict(row))
            seen.add(story_id)
        return pending

    @classmethod
    def _load_pending_exclusive_story_ids(cls, bindings, pending, composition):
        raw = (bindings.get("pending_exclusive_story_ids", ())
               if isinstance(bindings, Mapping) else ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise RuntimeError("invalid_pending_exclusive_story_ids")
        if len(raw) > cls._pending_candidate_limit(composition):
            raise RuntimeError("invalid_pending_exclusive_story_ids")
        pending_ids = {str(row.get("story_id")) for row in pending}
        exclusive_ids = set()
        for story_id in raw:
            if (not isinstance(story_id, str) or story_id in exclusive_ids
                    or story_id not in pending_ids):
                raise RuntimeError("invalid_pending_exclusive_story_ids")
            exclusive_ids.add(story_id)
        return exclusive_ids

    @staticmethod
    def _next_corpus_cursor(rows, hot_story_ids=(), *, general_boundary=None):
        """Where the next continuation resumes, carrying BOTH orderings.

        The general lanes resume from the oldest row read. The hot lane resumes
        from its OWN sort key, built ONLY from rows the hot lane itself returned.
        Every row in the general pool carries an independent_source_count, most
        of them 1, and letting one of those become the boundary told the SQL to
        return only hot rows below it, so the hot lane emptied after the first
        continuation while count-3 stories were still waiting.
        """
        if not rows:
            return ({"before_published_at": general_boundary[0],
                     "before_story_id": general_boundary[1]}
                    if general_boundary and all(general_boundary) else {})
        oldest = min(rows, key=lambda item: (str(item["published_at"]), str(item["story_id"])))
        # A hot/interested lane may have fetched an older row than the general
        # keyset. Only the general query's own last row is its safe boundary.
        general = (general_boundary if general_boundary and all(general_boundary)
                   else (oldest["published_at"], oldest["story_id"]))
        cursor = {"before_published_at": general[0], "before_story_id": general[1]}
        hot = [row for row in rows
               if str(row.get("story_id")) in set(hot_story_ids)
               and isinstance(row.get("independent_source_count"), int)
               and not isinstance(row.get("independent_source_count"), bool)]
        if hot:
            last = min(hot, key=lambda item: (item["independent_source_count"],
                                              str(item["published_at"]), str(item["story_id"])))
            cursor["hot"] = {"before_source_count": last["independent_source_count"],
                             "before_published_at": last["published_at"],
                             "before_story_id": last["story_id"]}
        return cursor

    def _slice(self, cards, offset, size, snapshot, *, continuation_offsets=None,
               event_group_ids=None, selected_category=None):
        """One page of the frozen order, with "less like this" applied at RENDER.

        The frozen array itself is never mutated, so the HMAC-signed cursor stays
        valid and an offset minted before the filter still resolves to the same
        position. A filtered slice is topped up by walking further into the same
        array, and the reported next offset is the position actually reached.
        An empty first look-ahead stays bounded so its response ordinal can be
        retried safely. Once some cards are ready, scanning the rest of the
        already-bounded frozen array avoids spending an ordinal on a short page.
        """
        composition = self._policy.composition
        if composition is None:
            return cards[offset:offset + size], offset + size, []
        if not composition.immediate_negative_filter:
            profile = None
        else:
            profile = build_profile(snapshot, policy=composition, now=self._now())
        boundaries = {value for value in (continuation_offsets or ())
                      if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        if ((profile is None or not profile.hidden_story_ids)
                and not any(boundary < offset + size for boundary in boundaries)):
            return cards[offset:offset + size], offset + size, []
        visible, removed, position = [], [], offset
        first_limit = min(len(cards), offset + size * 2)
        while position < len(cards) and len(visible) < size:
            if position >= first_limit and not visible:
                break
            card = cards[position]
            stitched = any(boundary <= position for boundary in boundaries)
            position += 1
            if self._suppressed_by_profile(
                    card, profile, composition, selected_category=selected_category):
                removed.append(str(card.get("story_id")))
                continue
            if (stitched and self._violates_page_invariants(
                        visible, card, composition, event_group_ids)):
                # Keep the colliding card for the next response instead of
                # silently losing it behind the cursor. Normal continuations
                # are reflowed before persistence, so this is the bounded
                # fallback for an impossible partial page (for example, only
                # one source remains at the end of the corpus).
                position -= 1
                break
            visible.append(card)
        return visible, position, removed

    @staticmethod
    def _violates_page_invariants(visible, candidate, composition, event_group_ids=None):
        """Keep hard page rules intact when filtering stitches two slices."""
        groups = event_group_ids if isinstance(event_group_ids, Mapping) else {}
        source_id = str(candidate.get("source_id", ""))
        group = groups.get(str(candidate.get("story_id", "")))
        for previous in visible[-composition.same_source_window:]:
            if source_id and str(previous.get("source_id", "")) == source_id:
                return True
            if (isinstance(group, str) and group
                    and groups.get(str(previous.get("story_id", ""))) == group):
                return True
        title = normalize_title(str(candidate.get("title", "")))
        url = candidate.get("url") or candidate.get("canonical_url")
        for previous in visible:
            if title and normalize_title(str(previous.get("title", ""))) == title:
                return True
            previous_url = previous.get("url") or previous.get("canonical_url")
            if isinstance(url, str) and url and previous_url == url:
                return True
            if (isinstance(group, str) and group
                    and groups.get(str(previous.get("story_id", ""))) == group):
                return True
        return False

    def _align_continuation(self, existing, added, size, composition, event_group_ids=None,
                            *, page_prefix=None, defer_on_collision=False):
        """Reflow appended cards onto the frozen order's actual page boundaries."""
        if not added or composition is None or size <= 0:
            return list(added)
        tail_size = len(existing) % size
        page = (list(page_prefix) if page_prefix is not None
                else list(existing[-tail_size:]) if tail_size else [])
        remaining = list(added)
        aligned = []
        while remaining:
            if len(page) >= size:
                page = []
            chosen = next((index for index, candidate in enumerate(remaining)
                           if not self._violates_page_invariants(
                               page, candidate, composition, event_group_ids)), None)
            # The final runtime slice still fails closed on a collision. This
            # fallback merely keeps the reorder bounded when no legal candidate
            # remains for the current partial page.
            if chosen is None and defer_on_collision:
                break
            chosen = 0 if chosen is None else chosen
            candidate = remaining.pop(chosen)
            aligned.append(candidate)
            page.append(candidate)
        return aligned

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
        """Scoped the same way _assert_fresh is, and for the same reason.

        history_revision and server_commit_revision move on EVERY behavior event.
        Demanding that a client echo them exactly turns a save in another tab
        into a refused request before any work is done, which is the same bug as
        F1 seen one step earlier. Generation and consent still have to match:
        those are the values that make an order wrong rather than out of date.
        """
        expected = {"history_generation": snapshot.get("history_generation"),
                    "consent_revision": snapshot.get("consent_revision")}
        for key, value in expected.items():
            if body.get(key) != value:
                raise StaleRankingError(f"stale_{key}")
        for key, current in (("history_revision", snapshot.get("included_history_revision")),
                             ("server_commit_revision", snapshot.get("history_revision"))):
            seen = body.get(key)
            # A revision from the FUTURE is still refused: that is a binding
            # nobody computed, not a client that is merely a moment behind.
            if not isinstance(seen, int) or isinstance(seen, bool) or (
                    isinstance(current, int) and seen > current):
                raise StaleRankingError(f"stale_{key}")

    @staticmethod
    def _assert_fresh(before, after):
        # F1. Scoped to the three values that make a paid order WRONG rather than
        # merely out of date. history_revision and included_history_revision move
        # on every behavior event, including a save in a second tab, and checking
        # them here is exactly what threw a paid provider call away.
        for key in ("history_generation", "consent_revision", "provider_processing_enabled"):
            if before.get(key) != after.get(key):
                raise StaleRankingError(f"changed_{key}")

    def _cursor(self, frozen_id, offset, expires_at, *, response_number=None):
        payload = {"frozen_order_id": frozen_id, "offset": offset, "expires_at": expires_at}
        if response_number is not None:
            payload["response_number"] = response_number
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
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

    def _now(self) -> datetime:
        from datetime import timezone
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def _open_run(self, owner, snapshot, composition):
        """Open or join the owner's reading run and freeze the profile on it."""
        if composition is None:
            return None
        fresh = build_profile(snapshot, policy=composition, now=self._now())
        frozen_profile = {**fresh.as_snapshot(),
            "_history_generation": snapshot.get("history_generation"),
            "_consent_revision": snapshot.get("consent_revision")}
        run = self._store.open_reading_run(user_id=owner.user_id, idle_minutes=composition.idle_minutes,
                                           max_minutes=composition.max_run_minutes,
                                           profile=frozen_profile)
        if not isinstance(run, Mapping):
            return {}
        stored = run.get("profile_snapshot")
        same_epoch = (isinstance(stored, Mapping)
            and stored.get("_history_generation") == snapshot.get("history_generation")
            and stored.get("_consent_revision") == snapshot.get("consent_revision"))
        if not same_epoch:
            raise RuntimeError("reading_run_epoch_unavailable")
        return run

    def _promotion_rows(self, query, composition, before_published, before_story):
        """Language-exclusive candidates allowed to compete for a place in All.

        Off entirely at a cap of zero, and then not even fetched: a switch that
        still costs a round trip is not off.
        """
        cap = composition.exclusive_promote_to_all_max
        if not (self._policy.other_lane_enabled and self._policy.exclusive_category_id and cap > 0):
            return []
        started_at = time.perf_counter()
        ready, _boundary, _has_more = self._exclusive_display_rows(
            query=query, before_published=before_published, before_story=before_story,
            target_count=max(cap * 4, cap), excluded_story_ids=(), max_batches=2)
        print(json.dumps({"event": "m2_promotion_timing",
            "duration_ms": round((time.perf_counter() - started_at) * 1000), "rows": len(ready)},
            separators=(",", ":")), file=sys.stderr, flush=True)
        return ready

    def _exclusive_display_rows(self, *, query, before_published, before_story,
                                target_count, excluded_story_ids, max_batches,
                                suppressed_sources=()):
        """Read through raw exclusive rows until enough complete display copy exists.

        The RPC caps one call at 100 rows. Its cursor is over the raw corpus, so
        the returned boundary always advances across untranslated rows instead
        of falsely declaring the section exhausted.
        """
        ready = []
        excluded = {str(story_id) for story_id in excluded_story_ids}
        suppressed = set(suppressed_sources)
        consumed_rows = []
        batches = 0
        while len(ready) < target_count and (max_batches is None or batches < max_batches):
            batch = list(self._store.retained_candidates_language_exclusive(
                display_language=self._policy.display_language, query=query, limit=100,
                before_published_at=before_published, before_story_id=before_story,
                policy_id=self._policy.exclusivity_policy_id))
            batches += 1
            if not batch:
                return ready, consumed_rows, False
            for row in batch:
                consumed_rows.append(row)
                story_id = str(row.get("story_id"))
                if (story_id not in excluded and row.get("source_id") not in suppressed
                        and self._display_ready_rows((row,))):
                    ready.append(row)
                if len(ready) >= target_count:
                    # The last candidate is a look-ahead sentinel proving there
                    # is another page. It was not offered to the recipe, so it
                    # remains on the next scan rather than being skipped.
                    return ready, consumed_rows[:-1], True
            has_more = len(batch) == 100
            if not has_more:
                return ready, consumed_rows, False
            boundary = batch[-1]
            before_published = str(boundary["published_at"])
            before_story = str(boundary["story_id"])
        return ready, consumed_rows, True

    def _display_ready_rows(self, rows):
        """Keep other-language cards only when the configured display copy is complete.

        The language-exclusive surface promises a readable title and summary in
        the reader's display language. Translation failures remain reviewable in
        storage, but they cannot leak raw copy into that promised surface or its
        capped All-page promotion.
        """
        target = self._policy.display_language
        ready = []
        for row in rows:
            if str(row.get("language")) == target:
                title, summary = row.get("title"), row.get("summary")
            else:
                titles = row.get("title_translations")
                summaries = row.get("summary_translations")
                if not isinstance(titles, Mapping) or not isinstance(summaries, Mapping):
                    continue
                title, summary = titles.get(target), summaries.get(target)
            if isinstance(title, str) and title.strip() and isinstance(summary, str) and summary.strip():
                ready.append(row)
        return ready

    def _exclusive_safe_cursor_rows(self, consumed_rows, admitted_story_ids,
                                    excluded_story_ids, *, suppressed_sources=()):
        """Return the raw prefix that can be retired without losing a story.

        Display-incomplete and explicitly excluded rows are intentionally
        skipped. A ready row advances the cursor only when the recipe actually
        admitted it; the first source-capped row stops the prefix so a later
        continuation can reconsider it with a fresh per-window cap.
        """
        admitted = {str(story_id) for story_id in admitted_story_ids}
        excluded = {str(story_id) for story_id in excluded_story_ids}
        suppressed = set(suppressed_sources)
        safe = []
        for row in consumed_rows:
            story_id = str(row.get("story_id"))
            if (story_id in excluded or row.get("source_id") in suppressed
                    or not self._display_ready_rows((row,))
                    or story_id in admitted):
                safe.append(row)
                continue
            break
        return safe

    @staticmethod
    def _exclusive_corpus_cursor(consumed_rows):
        """Resume after the last row in the exclusive RPC's declared order."""
        boundary = consumed_rows[-1]
        return {"before_published_at": boundary["published_at"],
                "before_story_id": boundary["story_id"]}

    @staticmethod
    def _cap_promotions(ordered, exclusive_ids, cap):
        if not exclusive_ids:
            return ordered
        kept, promoted = [], 0
        for item in ordered:
            if item.story_id in exclusive_ids:
                if promoted >= cap:
                    continue
                promoted += 1
            kept.append(item)
        return kept

    def _release_claim(self, owner, run, eligibility_key, claim_token):
        """Hand the claim back. Conditional on still holding it, and never fatal."""
        try:
            self._store.release_run_ranking_claim(user_id=owner.user_id,
                run_id=str(run["run_id"]), eligibility_key=eligibility_key, token=str(claim_token))
        except Exception as error:
            _page_suppressed_exception("m2_claim_release_failed", error,
                run_id=str(run["run_id"]))

    def _reserve(self, owner, request_id, estimate, run, eligibility_key, claim_token):
        """Reserve, re-validating the claim in the same transaction when held."""
        if claim_token and run and run.get("run_id"):
            answer = self._store.reserve_budget_claimed(user_id=owner.user_id, request_id=request_id,
                amount_usd=estimate, daily_limit_usd=self._policy.daily_cost_limit_usd,
                run_id=str(run["run_id"]), eligibility_key=eligibility_key,
                claim_token=str(claim_token))
            if not isinstance(answer, Mapping):
                return False
            refusal = answer.get("refusal")
            if refusal:
                # Losing the claim and running out of budget both mean "do not
                # call the provider", and both used to arrive downstream as one
                # generic budget_reservation_failed. They are different facts:
                # one is a race that resolved itself, the other is a day's money
                # gone. Both are named here, in one shape, with the view they
                # happened on and what was left.
                print(json.dumps({"event": "m2_reserve_refused", "reason": str(refusal),
                                  "request_id": request_id, "view": eligibility_key,
                                  "remaining_usd": answer.get("remaining_usd")},
                                 separators=(",", ":")), file=sys.stderr, flush=True)
            return answer.get("reserved") is True
        return self._store.reserve_budget(user_id=owner.user_id, request_id=request_id,
            amount_usd=estimate, daily_limit_usd=self._policy.daily_cost_limit_usd)

    def _abandon_unbound_order(self, owner, request_id, reservation_created, observed_usage):
        """Give back what was reserved and never spent.

        A cost the provider really charged is NEVER erased: that truthfulness is
        the whole point of the settlement path. Only a reservation that bought
        nothing is released.
        """
        if not reservation_created or observed_usage:
            return
        try:
            self._store.settle_budget(user_id=owner.user_id, request_id=request_id,
                                      actual_usd=0.0, status="released")
        except Exception as error:
            log_suppressed_exception("m2_release_failed", error, stream=sys.stderr,
                request_id=request_id)

    def _eligibility_key(self, category_id, query, exclusive, *, history_generation=1,
                         consent_revision=1) -> str:
        """One view: All, a category, a search, or the exclusive section.

        A DIGEST, not the values: a search query is text the owner typed, and
        this key is an index, not a place to keep what she searched for. The
        serving contract is part of the identity too. Otherwise a deploy can
        reuse an open run's old-policy response, which the new reader must
        reject before rendering.
        """
        policy_identity = self._policy.effective_policy_digest
        if not policy_identity:
            policy_identity = hashlib.sha256(json.dumps(asdict(self._policy),
                separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
        raw = json.dumps([
            # A deploy using atomic response progress must never share a view
            # with an in-flight pre-migration worker that can advance the old
            # page counter without its offset.  This is a data-contract version,
            # not a provider/model version, and changes only when that contract
            # changes.
            "atomic-response-progress-v1",
            policy_identity,
            category_id,
            query,
            bool(exclusive),
            history_generation,
            consent_revision,
        ], separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _open_view(self, owner, run, eligibility_key):
        """What this run has already done with THIS view, if anything."""
        if not run or not run.get("run_id"):
            return None
        try:
            view = self._store.open_run_view(user_id=owner.user_id, run_id=str(run["run_id"]),
                                             eligibility_key=eligibility_key)
        except Exception as error:
            _page_suppressed_exception("m2_view_unavailable", error,
                run_id=str(run["run_id"]))
            return None
        return view if isinstance(view, Mapping) else None

    def _claim_ranking(self, owner, run, eligibility_key, composition):
        """Take this view's ranking claim, or report who holds it. Never fatal."""
        if composition is None or not run or not run.get("run_id"):
            return None
        try:
            claim = self._store.claim_run_ranking(user_id=owner.user_id, run_id=str(run["run_id"]),
                eligibility_key=eligibility_key, token=str(uuid.uuid4()),
                ttl_seconds=composition.ranking_claim_seconds)
        except Exception as error:
            # A claim store that is down must not take the feed down with it. The
            # worst case without it is the pre-existing behavior: two concurrent
            # first ranks, which is what this fixes, not what it depends on.
            log_suppressed_exception("m2_claim_unavailable", error, stream=sys.stderr,
                run_id=str(run["run_id"]))
            return None
        return claim if isinstance(claim, Mapping) else None

    def _claim_continuation(self, owner, run_id, eligibility_key, composition,
                            frozen_order_id):
        """Serialize append-at-end continuations on the existing view row.

        The frozen-order append RPC concatenates by design. Reusing the ranking
        claim as a short mutation lock prevents two identical signed cursors
        from appending the same batch twice. Unlike the first-rank compatibility
        path, failure here is fail-closed because serving can safely retry.
        """
        token = str(uuid.uuid4())
        try:
            claim_with_snapshot = getattr(self._store, "claim_continuation_snapshot", None)
            if claim_with_snapshot is None:
                claim = self._store.claim_run_ranking(user_id=owner.user_id, run_id=run_id,
                    eligibility_key=eligibility_key, token=token,
                    ttl_seconds=composition.ranking_claim_seconds)
            else:
                claim = claim_with_snapshot(user_id=owner.user_id, run_id=run_id,
                    eligibility_key=eligibility_key, frozen_order_id=frozen_order_id,
                    token=token, ttl_seconds=composition.ranking_claim_seconds)
        except Exception as error:
            _page_suppressed_exception("m2_claim_unavailable", error,
                run_id=run_id)
            raise RuntimeError("page_budget_unavailable") from error
        if not isinstance(claim, Mapping) or not claim.get("granted"):
            raise RankingInProgressError()
        snapshot = claim.get("frozen_order") if claim_with_snapshot is not None else None
        return token, snapshot

    def _existing_run_page(self, token, owner, view, snapshot, page_size):
        """Page one of the ranking this VIEW already paid for, or None.

        A deleted bound order returns a private sentinel because consent/history
        invalidation may rank again. An expired order returns None and the caller
        ends that view without buying again. A refresh in every valid case is
        free: same request id, no new frozen order, no reservation, no provider
        call.
        """
        if not view or not view.get("frozen_order_id"):
            return None
        frozen = self._store.load_frozen_order(user_id=owner.user_id,
                                               frozen_order_id=str(view["frozen_order_id"]))
        if not frozen:
            return _MISSING_FROZEN_ORDER
        if int(frozen["expires_at"]) < int(self._clock()):
            return None
        bindings = frozen.get("bindings") or {}
        for key in ("history_generation", "consent_revision"):
            if bindings.get(key) != snapshot.get(key):
                return None
        if bindings.get("result_mode") == "model" and not snapshot.get("provider_processing_enabled"):
            return None
        # Read progress AFTER the frozen order.  New responses commit the view
        # high-water mark and frozen offset atomically; this order means a
        # concurrent commit either leaves us with the older consistent pair or
        # exposes a mismatch.  One second frozen read completes the latter pair.
        eligibility_key = view.get("eligibility_key")
        run_id = view.get("run_id")
        fresh_view = self._open_view(owner, {"run_id": run_id}, eligibility_key)
        view_pages = fresh_view.get("pages_served") if isinstance(fresh_view, Mapping) else None
        bound_pages = bindings.get("responses_served")
        if view_pages != bound_pages:
            latest = self._store.load_frozen_order(user_id=owner.user_id,
                frozen_order_id=str(view["frozen_order_id"]))
            if not latest or int(latest["expires_at"]) < int(self._clock()):
                return _MISSING_FROZEN_ORDER if not latest else None
            latest_bindings = latest.get("bindings") or {}
            for key in ("history_generation", "consent_revision"):
                if latest_bindings.get(key) != snapshot.get(key):
                    return None
            frozen, bindings = latest, latest_bindings
            bound_pages = bindings.get("responses_served")
        if view_pages != bound_pages:
            # The only safe repair is the initial page: the saved order already
            # carries its exact start/end offsets, while the failed reservation
            # left the view at zero.  Any other mismatch lacks enough durable
            # information to mint a cursor, so retry instead of duplicating a
            # page or bypassing the response cap.
            last_offset = bindings.get("last_served_offset")
            last_next = bindings.get("last_served_next_offset")
            repaired = (view_pages == 0 and bound_pages == 1
                and isinstance(last_offset, int) and not isinstance(last_offset, bool)
                and isinstance(last_next, int) and not isinstance(last_next, bool)
                and self._reserve_response_slot(owner, str(run_id), eligibility_key,
                    str(view["frozen_order_id"]), 1, last_offset, last_next) is not None)
            if not repaired:
                raise RankingInProgressError()
            view_pages = bound_pages
        size = int(frozen.get("page_size", page_size))
        cards = list(frozen["cards"])
        visible, next_offset, removed = self._slice(
            cards, 0, size, snapshot,
            continuation_offsets=bindings.get("continuation_offsets"),
            event_group_ids=bindings.get("event_group_ids"),
            selected_category=(bindings.get("eligibility") or {}).get("category"))
        visible = self._overlay_owner_states(token, visible)
        self._record_filtered(owner, frozen, removed)
        pages_served = view_pages
        if (not isinstance(pages_served, int) or isinstance(pages_served, bool)
                or pages_served < 0):
            pages_served = 0
        # A refresh replays page one; it never resets the view's durable
        # high-water mark. A visible legacy order proves page one was served
        # even when the old view row says zero. A newly filtered-empty replay
        # consumes no new response, but must still continue after every response
        # the view already served.
        next_response_number = (max(pages_served, 1) + 1
                                if visible else pages_served + 1)
        resume_offset = next_offset
        if pages_served > 1:
            stored_next = bindings.get("last_served_next_offset")
            if (not isinstance(stored_next, int) or isinstance(stored_next, bool)
                    or stored_next < 0 or stored_next > len(cards)):
                last_start = bindings.get("last_served_offset")
                stored_next = (last_start + size
                    if isinstance(last_start, int) and not isinstance(last_start, bool)
                    and last_start >= 0 else next_offset)
            resume_offset = min(len(cards), max(next_offset, stored_next))
        next_cursor = (self._cursor(str(view["frozen_order_id"]), resume_offset,
                                    int(frozen["expires_at"]),
                                    response_number=next_response_number)
                       if resume_offset < len(cards) or bindings.get("corpus_has_more") else None)
        return {"schema_version": 1, **self._public_bindings(bindings), "cards": visible,
                "next_cursor": next_cursor, "end_of_run": False}

    def _overlay_owner_states(self, token, cards):
        """Render mutable owner state live without changing the frozen order."""
        if not cards:
            return cards
        states = self._store.owner_states(token, [str(card["story_id"]) for card in cards])
        rendered = []
        for card in cards:
            state = states.get(str(card["story_id"]), {}) if isinstance(states, Mapping) else {}
            rendered.append({**card,
                "read_at": state.get("read_at", card.get("read_at")),
                "saved_at": state.get("saved_at", card.get("saved_at")),
                "state_revision": state.get("state_revision", card.get("state_revision", 0)),
                "interests": state.get("interests", card.get("interests", []))})
        return rendered

    def _pool_rows(self, category_id, query, profile, composition, before_published, before_story,
                   hot_cursor=None, *, excluded_story_ids=(), owner: AuthenticatedOwner):
        """Ask the corpus for each lane, then merge.

        Returns ``(rows, hot_story_ids, general_boundary, general_has_more)``.
        The provenance matters: the hot lane
        pages on its own sort key, so its cursor may only ever be built from rows
        THE HOT LANE RETURNED. The general pool carries every story, count-1 ones
        included, and letting one of those become the hot boundary makes the SQL
        return only hot rows below it, skipping still-available count-3 stories.

        One "newest N" window can only ever express one ordering, which is why
        today's feed is the newest 50 rows. Each lane orders by its own criterion,
        so each is asked for separately and the recipe merges what comes back.
        """
        if not isinstance(owner, AuthenticatedOwner) or not isinstance(owner.user_id, str):
            raise AuthenticationError("verified owner required for candidate retrieval")
        try:
            valid_owner_id = str(uuid.UUID(owner.user_id)) == owner.user_id
        except ValueError:
            valid_owner_id = False
        if not valid_owner_id or owner.actor_kind is not ActorKind.HUMAN:
            raise AuthenticationError("verified owner required for candidate retrieval")
        owner_id = owner.user_id
        hide_already_opened = composition.hide_already_opened
        categories = sorted({topic for topic, weight in profile.topic_affinity.items() if weight > 0})
        sources = sorted({source for source, weight in profile.source_affinity.items() if weight > 0})
        quotas = lane_window_quotas(composition, composition.candidate_window_size)
        # The general pool is unbounded by any lane's age window. Every lane
        # query carries an age bound (that is what keeps the pools distinct), so
        # asking only for lanes means a reader with no profile is served from the
        # last few hours alone and the page comes back short with hundreds of
        # candidates unread. Python assigns the lanes; this just makes sure the
        # recipe has a corpus to work from.
        # The SQL RPC excludes clicked stories before LIMIT, leaving their
        # publishers and broad topics eligible for every lane.
        excluded = set(excluded_story_ids)
        if composition.immediate_negative_filter:
            excluded.update(profile.hidden_story_ids)
        # Owner history and explicit exclusions can each contain up to 1,000
        # ids, while the candidate RPC accepts 1,200. Keep the full set for
        # Python filtering and bounded keyset scans; send only the SQL maximum.
        sql_excluded = tuple(sorted(excluded))[:1200]
        suppressed_sources = ()
        suppressed_topics = ()
        def fetch_general():
            scan_start = time.perf_counter()
            target = composition.candidate_window_size + composition.page_size
            general_rows: dict[str, Mapping[str, object]] = {}
            eligible_general = 0
            general_boundary = (before_published, before_story)
            general_has_more = False
            general_batches = 0
            batch_limits = []
            batch_sizes = []
            excluded_rows = 0
            suppressed_rows = 0
            # Explicit story exclusions have their own validated 1,000-id bound.
            # Keep that proven scan depth; add at most the configured extra head
            # batch for broad source/topic suppression.
            general_budget = max(composition.pool_scan_max_batches,
                                 (len(excluded) + target + 99) // 100)
            while eligible_general < target and general_batches < general_budget:
                limit = min(composition.general_pool_batch_limit,
                            target - eligible_general)
                batch = self._store.retained_candidates_v2(
                    category_id=category_id, query=query, lane=None,
                    owner_id=owner_id, hide_already_opened=hide_already_opened,
                    profile_categories=(), profile_sources=(),
                    trend_window_hours=composition.trend_window_hours,
                    trend_min_sources=composition.trend_min_independent_sources,
                    max_age_hours=None, min_age_hours=None,
                    limit=limit, before_published_at=general_boundary[0],
                    before_story_id=general_boundary[1], before_source_count=None,
                    excluded_story_ids=sql_excluded,
                    suppressed_sources=suppressed_sources,
                    suppressed_topics=suppressed_topics)
                general_batches += 1
                batch_limits.append(limit)
                batch_sizes.append(len(batch))
                for row in batch:
                    story_id = row.get("story_id")
                    if not isinstance(story_id, str):
                        continue
                    if story_id in excluded:
                        excluded_rows += 1
                        continue
                    if self._suppressed_by_profile(
                            row, profile, composition, selected_category=category_id):
                        suppressed_rows += 1
                        continue
                    general_rows.setdefault(story_id, row)
                    eligible_general += 1
                if batch:
                    general_boundary = (batch[-1]["published_at"], batch[-1]["story_id"])
                # A full batch might have more rows behind it. An exact-end batch
                # permits one harmless empty continuation instead of losing news.
                general_has_more = len(batch) == limit
                if len(batch) < limit:
                    break
            _page_diagnostic({"event": "m2_pool_timing", "lane": "general",
                "duration_ms": round((time.perf_counter() - scan_start) * 1000),
                "rpc_count": general_batches, "rows": len(general_rows),
                "target": target, "excluded_count": len(excluded),
                "batch_limits": batch_limits, "batch_sizes": batch_sizes,
                "excluded_rows": excluded_rows, "suppressed_rows": suppressed_rows})
            return general_rows, general_boundary, general_has_more
        # Each lane has its own keyset and reads the same immutable request
        # inputs. Overlap all I/O, then merge results in policy priority order
        # so the recipe remains byte-for-byte deterministic. A policy value of
        # one keeps the previous serial path available for rollback.
        lanes = tuple(lane for lane in composition.lane_priority
            if lane not in ("interested", "surprise") or categories or sources)

        def fetch_lane(lane):
            scan_start = time.perf_counter()
            rpc_count = 0
            # Over-fetch so caps and spacing have something to choose from, and
            # so a lane whose head is all one source is not silently short.
            limit = self._lane_fetch_limit(quotas[lane])
            # The hot lane orders by independent source count first, so it pages
            # on the WHOLE sort key or on none of it. Half a keyset is refused by
            # the SQL, and sending one is how the first "load more" past a frozen
            # order used to error instead of continuing.
            # The hot lane keeps its OWN cursor, because it does not order by
            # publication time and the general cursor therefore does not describe
            # its boundary at all.
            if lane == "hot":
                lane_cursor = tuple(hot_cursor) if hot_cursor else (None, None, None)
            else:
                lane_cursor = (before_published, before_story, None)
            eligible_lane = 0
            lane_rows = []
            lane_hot_story_ids = set()
            for _ in range(composition.pool_scan_max_batches):
                batch_limit = min(100, limit - eligible_lane)
                if batch_limit <= 0:
                    break
                rows = self._store.retained_candidates_v2(
                    category_id=category_id, query=query, lane=lane,
                    owner_id=owner_id, hide_already_opened=hide_already_opened,
                    profile_categories=categories, profile_sources=sources,
                    trend_window_hours=composition.trend_window_hours,
                    trend_min_sources=composition.trend_min_independent_sources,
                    max_age_hours=(composition.updates_max_age_hours if lane == "updates" else
                                   composition.trend_window_hours if lane == "hot" else
                                   composition.exploration_max_age_hours if lane == "surprise" else None),
                    # Fresh stories own the updates lane. Other lanes begin
                    # beyond it so their fetch budgets do not repeat its head.
                    min_age_hours=None if lane == "updates" else composition.updates_max_age_hours,
                    limit=batch_limit, before_published_at=lane_cursor[0],
                    before_story_id=lane_cursor[1], before_source_count=lane_cursor[2],
                    excluded_story_ids=sql_excluded,
                    suppressed_sources=suppressed_sources,
                    suppressed_topics=suppressed_topics)
                rpc_count += 1
                for row in rows:
                    story_id = row.get("story_id")
                    if (not isinstance(story_id, str) or story_id in excluded
                            or self._suppressed_by_profile(
                                row, profile, composition, selected_category=category_id)):
                        continue
                    eligible_lane += 1
                    lane_rows.append(row)
                    if lane == "hot":
                        lane_hot_story_ids.add(story_id)
                if rows:
                    last = rows[-1]
                    lane_cursor = (last["published_at"], last["story_id"],
                                   last["independent_source_count"] if lane == "hot" else None)
                if len(rows) < batch_limit:
                    break
            _page_diagnostic({"event": "m2_pool_timing", "lane": lane,
                "duration_ms": round((time.perf_counter() - scan_start) * 1000),
                "rpc_count": rpc_count, "rows": len(lane_rows)})
            return lane_rows, lane_hot_story_ids

        if composition.pool_parallel_workers == 1 or not lanes:
            merged, general_boundary, general_has_more = fetch_general()
            lane_results = [fetch_lane(lane) for lane in lanes]
        else:
            with ThreadPoolExecutor(max_workers=min(composition.pool_parallel_workers, len(lanes) + 1)) as pool:
                general_future = pool.submit(fetch_general)
                lane_results = list(pool.map(fetch_lane, lanes))
                merged, general_boundary, general_has_more = general_future.result()
        hot_story_ids: set[str] = set()
        for lane_rows, lane_hot_story_ids in lane_results:
            hot_story_ids.update(lane_hot_story_ids)
            for row in lane_rows:
                merged.setdefault(row["story_id"], row)
        return list(merged.values()), hot_story_ids, general_boundary, general_has_more

    def _suppressed_by_profile(self, row, profile, composition, *, selected_category=None):
        if profile is None or composition is None or not composition.immediate_negative_filter:
            return False
        return row.get("story_id") in profile.hidden_story_ids

    def _card(self, row, owner_state, *, lane=None, composition=None, exclusive=False, also_covered_by=()):
        language = str(row["language"])
        titles = row.get("title_translations") or {}
        summaries = row.get("summary_translations") or {}
        if not isinstance(titles, Mapping) or not isinstance(summaries, Mapping):
            raise ValueError("invalid_translation_overlay")
        # Version 4 is version 3 plus the measured coverage count. The reader
        # accepts every older version during the deploy, so either side can land
        # first without rejecting the feed.
        card = {"card_schema_version": 4 if composition is not None else 2,
            "story_id": row["story_id"], "title": row["title"],
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
        if composition is not None:
            # Every card carries a visible element label. A reader looking at a
            # page can name why each story is in front of her, and a page stays
            # reviewable after the fact because the label is persisted with the
            # frozen order, not recomputed from a profile that has since moved.
            card["lane"] = lane
            card["lane_label"] = composition.label_for(lane) if lane else None
            card["surprise_label"] = (composition.surprise_label_text
                                      if lane == "surprise" and composition.surprise_label_enabled else None)
            card["exclusive_label"] = (composition.exclusive_label(self._policy.display_language)
                                       if exclusive else None)
            card["also_covered_by"] = list(also_covered_by)
            coverage_count = row.get("independent_source_count", 1)
            if type(coverage_count) is not int or coverage_count < 0:
                raise ValueError("invalid_independent_source_count")
            card["coverage_count"] = coverage_count
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
                  "consent_revision", "server_commit_revision", "result_mode", "fallback_reason", "order_origin")
        # run_id, short_lane_reasons, lane_counts and calibration_alarm are
        # PERSISTED on the frozen order and read back by m2_owner_reading_pages.
        # They are deliberately not in the response. Order origin is the one
        # explicit extension the reader accepts while older orders remain valid.
        result = {key: bindings[key] for key in fields if key in bindings}
        if "order_origin" not in result:
            # Frozen orders from the prior release did not persist provenance.
            # Their recipe bindings still contain lane counts, so page turns
            # can describe that order truthfully without rewriting the order.
            result["order_origin"] = ("direct_model" if bindings.get("result_mode") == "model" else
                "recipe" if "lane_counts" in bindings else "freshness")
        return result

    @classmethod
    def _page_response(cls, bindings, cards, cursor, receipt):
        return {"schema_version": receipt.schema_version, **cls._public_bindings(bindings), "cards": cards, "next_cursor": cursor}
