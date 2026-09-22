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
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from curator.contracts.enums import ActorKind, EventType, M2HistoryEventType
from curator.contracts.ranking_request import (
    AuthenticatedOwner,
    OrderedHistoryEvent,
    RankingCandidate,
    RankingRequest,
)

from .composition import BACKFILL_LANE, CompositionPolicy
from .diagnostics import log_suppressed_exception
from .finalize import finalize_order
from .profile import BehaviorProfile, build_profile
from .rankllm_adapter import BudgetState, RankLLMAdapter
from .recipe import LanedCandidate, build_window, lane_window_quotas
from .supabase_http import SupabaseAuthenticationError


# The most Supabase round trips one /rank can make WHILE HOLDING the run's
# ranking claim. It sizes run.ranking_claim_seconds: the claim may not expire
# while its holder is still working, or a second caller takes over and pays for
# the same view. Measured, not guessed, by
# tests/test_ranker_claimed_section_budget.py, which walks the longest path with
# a counting transport and refuses a count above this number.
#
# The longest measured path is 14: the claim itself, five retained_candidates_v2
# calls (the general pool plus one per lane in lane_priority), one
# retained_candidates_language_exclusive for the promotion, reserve_budget,
# reserve_budget_claimed, settle_budget, history_snapshot, owner_states,
# save_frozen_order and bind_run_frozen_order. Two more are allowed for the
# branches that harness cannot reach in one pass (record_run_page, and the
# release_run_ranking_claim on the failure path).
CLAIMED_SECTION_MAX_TRANSPORT_CALLS = 16


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
                            min_age_hours: int | None, limit: int, before_published_at: str | None = None,
                            before_story_id: str | None = None,
                            before_source_count: int | None = None) -> Sequence[Mapping[str, object]]: ...
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
    def release_run_ranking_claim(self, *, user_id: str, run_id: str, eligibility_key: str,
                                  token: str) -> bool: ...
    def record_run_page(self, *, user_id: str, run_id: str, eligibility_key: str,
                        pages: int) -> int: ...
    def owner_states(self, access_token: str, story_ids: Sequence[str]) -> Mapping[str, Mapping[str, object]]: ...
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
    # The M2.1 Phase 2 feed recipe. None keeps the pre-Phase-2 window (the newest
    # `candidate_limit` rows), which is the documented rollback: the recipe is
    # turned off by unsetting one config path, not by reverting code.
    composition: CompositionPolicy | None = None
    # Hash of the complete effective ranking contract. The production runtime
    # binds checked-in policies, prompt content, and ranking code into this.
    # Alternate composition roots receive a deterministic dataclass digest.
    effective_policy_digest: str = ""

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
        eligibility_key = self._eligibility_key(category_id, query, exclusive)
        view = self._open_view(owner, run, eligibility_key)
        existing = self._existing_run_page(token, owner, view, snapshot, page_size)
        if existing is not None:
            return existing
        # CLAIM BEFORE PAYING. Reusing an order the run has already bound closes
        # the refresh hole; it does not close the race, because a second request
        # arriving while the first is still in flight sees no bound order yet.
        # Exactly one caller wins this compare-and-set, and only the winner may
        # reserve, call the provider and bind.
        claim = self._claim_ranking(owner, run, eligibility_key, composition)
        if claim is not None and not claim.get("granted"):
            served = self._existing_run_page(token, owner,
                {"frozen_order_id": claim.get("frozen_order_id")}, snapshot, page_size)
            if served is not None:
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
        profile = BehaviorProfile.from_snapshot(run.get("profile_snapshot")) if run else BehaviorProfile()
        # The language-exclusive section is served by the same M2 path: same
        # recipe, same pagination, same frozen order. Only the corpus narrows.
        if exclusive:
            rows = self._store.retained_candidates_language_exclusive(
                display_language=self._policy.display_language, query=query,
                limit=self._policy.candidate_limit + len(excluded_set) + 1,
                before_published_at=before_published, before_story_id=before_story,
                policy_id=self._policy.exclusivity_policy_id,
            )
        elif composition is not None:
            rows, hot_story_ids = self._pool_rows(category_id, query, profile, composition,
                                                  before_published, before_story)
            # Capped promotion: a few stories only the other language's press
            # carried get to compete for a place in All, on merit. They do NOT
            # get extra slots; they enter the same pool and take their own
            # lane's quota like any other candidate.
            promotion = self._promotion_rows(query, composition, before_published, before_story)
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
        filtered = [row for row in rows if row.get("story_id") not in excluded_set]
        has_more = len(filtered) > self._policy.candidate_limit
        rows = filtered[:self._policy.candidate_limit]
        laned: tuple[LanedCandidate, ...] = ()
        if composition is not None:
            # THIS is the product: four labeled pools with quotas and caps. The
            # model only reorders what the recipe hands it.
            laned = build_window(filtered, profile=profile, policy=composition,
                                 now=self._now(), size=composition.candidate_window_size)
            rows = [item.row for item in laned]
            has_more = len(filtered) > len(rows)
        next_corpus = None
        if composition is not None:
            # The SAME cursor shape the continuation writes, hot key included, so
            # the first "load more" resumes from a cursor of the shape it expects
            # rather than from a narrower one written by a different code path.
            if has_more and filtered:
                next_corpus = self._next_corpus_cursor(filtered, hot_story_ids)
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
        # The reservation re-checks the claim in its OWN transaction. A config
        # rule keeps a claim from expiring while its holder may still be calling
        # the provider; this is the backstop for everything that rule cannot see,
        # and a caller whose claim has moved on spends nothing at all.
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
                page_size=page_size, pages=composition.max_pages_per_run, profile=profile)
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
        expires_at = int(self._clock()) + self._policy.cursor_ttl_seconds
        bindings = self._bindings(receipt)
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
        bindings.update({"eligibility": {"category": category_id, "query": query},
            "eligibility_key": eligibility_key,
            "corpus_cursor": next_corpus, "corpus_has_more": has_more,
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
                if served is not None:
                    return served
                raise RankingInProgressError()
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
        run_id = (frozen.get("bindings") or {}).get("run_id")
        if composition is not None and size > 0:
            page_index = offset // size
            # The budget is counted per RUN, not per frozen order. Counting it on
            # the cursor let a refresh (which used to mint a new order and a new
            # cursor) hand the whole budget back.
            served_before = page_index
            if run_id:
                # Per VIEW: the pages she has read of All say nothing about how
                # many of a category she has read.
                bindings = frozen.get("bindings") or {}
                key = bindings.get("eligibility_key")
                # Orders created before policy-bound views did not persist the
                # exact key. Reconstructing it with the CURRENT deploy poisons
                # that deploy's page budget, so a legacy cursor keeps its own
                # page-index cap but never writes into a different view.
                if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key):
                    served_before = max(page_index,
                                        self._record_run_page(owner, str(run_id), key, page_index + 1))
            if page_index >= composition.max_pages_per_run or served_before >= composition.max_pages_per_run:
                # The run has served every page it promises. Reaching further
                # would keep returning older and older stories that met no pool's
                # rule, so the honest answer is that this run is over.
                return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                        "cards": [], "next_cursor": None, "end_of_run": True}
        if offset >= len(cards) and frozen["bindings"].get("corpus_has_more"):
            # F7, the branch that used to re-rank. Inside a reading run there is
            # never a second provider call: load more browses OLDER news, in the
            # same deterministic recipe order, with the same pools and labels and
            # no model. The new cards are APPENDED to this frozen order, so every
            # already-signed cursor keeps pointing at the same card.
            if self._policy.composition is not None:
                added = self._continue_frozen_order(token, owner, frozen, str(payload["frozen_order_id"]), size)
                if added:
                    cards = cards + list(added)
                    frozen["cards"] = cards
            else:
                # The documented rollback: with no recipe configured there is no
                # deterministic continuation to fall back on, so the pre-Phase-2
                # behavior stands unchanged rather than silently ending the feed.
                return self.rank(authorization=authorization, body={**current_bindings,
                    "history_revision": current.get("included_history_revision", 0),
                    "eligibility": frozen["bindings"].get("eligibility", {}), "exclude_story_ids": [],
                    "corpus_cursor": frozen["bindings"].get("corpus_cursor"), "page_size": size})
        visible, next_offset, removed = self._slice(cards, offset, size, current)
        self._record_filtered(owner, frozen, removed)
        next_cursor = self._cursor(str(payload["frozen_order_id"]), next_offset, int(frozen["expires_at"])) if next_offset < len(cards) else None
        if offset >= len(cards):
            # End of the run, said explicitly rather than as an empty page that
            # looks like a failure. There is nothing more to serve from this
            # frozen order, and a new reading run is what brings new stories.
            return {"schema_version": 1, **self._public_bindings(frozen["bindings"]),
                    "cards": [], "next_cursor": None, "end_of_run": True}
        if next_cursor is None and frozen["bindings"].get("corpus_has_more"):
            next_cursor = self._cursor(str(payload["frozen_order_id"]), len(cards), int(frozen["expires_at"]))
        return {"schema_version": 1, **self._public_bindings(frozen["bindings"]), "cards": visible, "next_cursor": next_cursor}

    def _continue_frozen_order(self, token, owner, frozen, frozen_order_id, size):
        """Older news, composed by the recipe alone. No provider call, ever.

        Returns the cards appended, or an empty tuple when there is nothing more
        to add. Any failure here degrades to "no more cards" rather than to a
        paid ranking: a page turn that quietly bills is the bug being fixed.
        """
        composition = self._policy.composition
        bindings = frozen.get("bindings", {})
        cursor = bindings.get("corpus_cursor") or {}
        if composition is None or not isinstance(cursor, Mapping) or not cursor:
            return ()
        eligibility = bindings.get("eligibility") or {}
        category_id = eligibility.get("category") if isinstance(eligibility, Mapping) else None
        query = eligibility.get("query") if isinstance(eligibility, Mapping) else None
        profile = BehaviorProfile.from_snapshot(bindings.get("profile_snapshot"))
        seen = {str(card.get("story_id")) for card in frozen.get("cards", ())}
        pooled, hot_story_ids = self._pool_rows(category_id, query, profile, composition,
                                                cursor.get("before_published_at"),
                                                cursor.get("before_story_id"),
                                                self._hot_cursor(cursor))
        rows = [row for row in pooled if str(row.get("story_id")) not in seen]
        if not rows:
            return ()
        laned = build_window(rows, profile=profile, policy=composition, now=self._now(),
                             size=composition.candidate_window_size)
        if not laned:
            return ()
        exclusive = self._is_exclusive_category(category_id)
        owner_states = self._store.owner_states(token, [item.story_id for item in laned])
        finalization = finalize_order(laned, policy=composition, owner_states=owner_states,
            page_size=min(size, composition.page_size), pages=composition.max_pages_per_run,
            profile=profile)
        added = [self._card(item.row, owner_states.get(item.story_id, {}), lane=item.lane,
                            composition=composition, exclusive=exclusive,
                            also_covered_by=finalization.also_covered_by.get(item.story_id, ()))
                 for item in finalization.cards]
        if not added:
            return ()
        # The return is the whole point: the RPC refuses to grow an order past
        # its cap and returns 0, and a transport failure raises. Serving cards
        # this store did not accept would show her the same stories again on the
        # next page turn, and letting the error out would 500 a page turn.
        try:
            total = self._store.extend_frozen_order(user_id=owner.user_id,
                frozen_order_id=frozen_order_id, cards=added,
                bindings={"corpus_cursor": self._next_corpus_cursor(rows, hot_story_ids),
                          "corpus_has_more": len(rows) > len(added),
                          "continuation_mode": "recipe_only"})
        except Exception as error:
            log_suppressed_exception("m2_continuation_failed", error, stream=sys.stderr,
                reason="store_unavailable", frozen_order_id=frozen_order_id)
            return ()
        if not isinstance(total, int) or total <= len(frozen.get("cards", ())):
            # The order did not grow: it has reached its cap, or the row was not
            # matched. Either way this run is over, and saying so is better than
            # silently repeating the page she just read.
            print(json.dumps({"event": "m2_continuation_exhausted", "reason": "order_at_capacity"},
                             separators=(",", ":")), file=sys.stderr, flush=True)
            return ()
        return tuple(added)

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
                print(json.dumps({"event": "m2_filter_not_recorded", "run_id": str(run_id)},
                                 separators=(",", ":")), file=sys.stderr, flush=True)
        except Exception as error:
            # A page must render even when the audit write fails. The filter
            # itself already happened; this only records it.
            log_suppressed_exception("m2_filter_record_failed", error, stream=sys.stderr,
                run_id=str(run_id))

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
    def _next_corpus_cursor(rows, hot_story_ids=()):
        """Where the next continuation resumes, carrying BOTH orderings.

        The general lanes resume from the oldest row read. The hot lane resumes
        from its OWN sort key, built ONLY from rows the hot lane itself returned.
        Every row in the general pool carries an independent_source_count, most
        of them 1, and letting one of those become the boundary told the SQL to
        return only hot rows below it, so the hot lane emptied after the first
        continuation while count-3 stories were still waiting.
        """
        if not rows:
            return {}
        oldest = min(rows, key=lambda item: (str(item["published_at"]), str(item["story_id"])))
        cursor = {"before_published_at": oldest["published_at"], "before_story_id": oldest["story_id"]}
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

    def _slice(self, cards, offset, size, snapshot):
        """One page of the frozen order, with "less like this" applied at RENDER.

        The frozen array itself is never mutated, so the HMAC-signed cursor stays
        valid and an offset minted before the filter still resolves to the same
        position. A filtered slice is topped up by walking further into the same
        array, bounded by one extra page of look-ahead, and the reported next
        offset is the position actually reached.
        """
        composition = self._policy.composition
        if composition is None or not composition.immediate_negative_filter:
            return cards[offset:offset + size], offset + size, []
        profile = build_profile(snapshot, policy=composition, now=self._now())
        if not profile.suppressed_sources and not profile.suppressed_topics:
            return cards[offset:offset + size], offset + size, []
        visible, removed, position = [], [], offset
        limit = min(len(cards), offset + size * 2)
        while position < limit and len(visible) < size:
            card = cards[position]
            position += 1
            categories = card.get("category_ids") or []
            if (card.get("source_id") in profile.suppressed_sources
                    or any(category in profile.suppressed_topics for category in categories)):
                removed.append(str(card.get("story_id")))
                continue
            visible.append(card)
        return visible, position, removed

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

    def _now(self) -> datetime:
        from datetime import timezone
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def _open_run(self, owner, snapshot, composition):
        """Open or join the owner's reading run and freeze the profile on it."""
        if composition is None:
            return None
        fresh = build_profile(snapshot, policy=composition, now=self._now())
        run = self._store.open_reading_run(user_id=owner.user_id, idle_minutes=composition.idle_minutes,
                                           max_minutes=composition.max_run_minutes,
                                           profile=fresh.as_snapshot())
        return run if isinstance(run, Mapping) else {}

    def _promotion_rows(self, query, composition, before_published, before_story):
        """Language-exclusive candidates allowed to compete for a place in All.

        Off entirely at a cap of zero, and then not even fetched: a switch that
        still costs a round trip is not off.
        """
        cap = composition.exclusive_promote_to_all_max
        if not (self._policy.other_lane_enabled and self._policy.exclusive_category_id and cap > 0):
            return []
        return list(self._store.retained_candidates_language_exclusive(
            display_language=self._policy.display_language, query=query,
            limit=min(100, max(cap * 4, cap)), before_published_at=before_published,
            before_story_id=before_story, policy_id=self._policy.exclusivity_policy_id))

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
            log_suppressed_exception("m2_claim_release_failed", error, stream=sys.stderr,
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

    def _eligibility_key(self, category_id, query, exclusive) -> str:
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
            policy_identity,
            category_id,
            query,
            bool(exclusive),
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
            log_suppressed_exception("m2_view_unavailable", error, stream=sys.stderr,
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

    def _existing_run_page(self, token, owner, view, snapshot, page_size):
        """Page one of the ranking this VIEW already paid for, or None.

        Returns None when there is no run, no bound order, the order has expired,
        or the scoped staleness values have moved, which are exactly the cases
        where a new ranking is the right answer. A refresh in every other case is
        free: same request id, no new frozen order, no reservation, no provider
        call.
        """
        if not view or not view.get("frozen_order_id"):
            return None
        frozen = self._store.load_frozen_order(user_id=owner.user_id,
                                               frozen_order_id=str(view["frozen_order_id"]))
        if not frozen or int(frozen["expires_at"]) < int(self._clock()):
            return None
        bindings = frozen.get("bindings") or {}
        for key in ("history_generation", "consent_revision"):
            if bindings.get(key) != snapshot.get(key):
                return None
        if bindings.get("result_mode") == "model" and not snapshot.get("provider_processing_enabled"):
            return None
        size = int(frozen.get("page_size", page_size))
        cards = list(frozen["cards"])
        visible, next_offset, removed = self._slice(cards, 0, size, snapshot)
        self._record_filtered(owner, frozen, removed)
        next_cursor = (self._cursor(str(view["frozen_order_id"]), next_offset, int(frozen["expires_at"]))
                       if next_offset < len(cards) or bindings.get("corpus_has_more") else None)
        return {"schema_version": 1, **self._public_bindings(bindings), "cards": visible,
                "next_cursor": next_cursor, "end_of_run": False}

    def _pool_rows(self, category_id, query, profile, composition, before_published, before_story,
                   hot_cursor=None):
        """Ask the corpus for each lane, then merge.

        Returns ``(rows, hot_story_ids)``. The provenance matters: the hot lane
        pages on its own sort key, so its cursor may only ever be built from rows
        THE HOT LANE RETURNED. The general pool carries every story, count-1 ones
        included, and letting one of those become the hot boundary makes the SQL
        return only hot rows below it, skipping still-available count-3 stories.

        One "newest N" window can only ever express one ordering, which is why
        today's feed is the newest 50 rows. Each lane orders by its own criterion,
        so each is asked for separately and the recipe merges what comes back.
        """
        categories = sorted({topic for topic, weight in profile.topic_affinity.items() if weight > 0})
        sources = sorted({source for source, weight in profile.source_affinity.items() if weight > 0})
        quotas = lane_window_quotas(composition, composition.candidate_window_size)
        merged: dict[str, Mapping[str, object]] = {}
        # The general pool first, unbounded by any lane's age window. Every lane
        # query carries an age bound (that is what keeps the pools distinct), so
        # asking only for lanes means a reader with no profile is served from the
        # last few hours alone and the page comes back short with hundreds of
        # candidates unread. Python assigns the lanes; this just makes sure the
        # recipe has a corpus to work from.
        for row in self._store.retained_candidates_v2(
                category_id=category_id, query=query, lane=None,
                profile_categories=(), profile_sources=(),
                trend_window_hours=composition.trend_window_hours,
                trend_min_sources=composition.trend_min_independent_sources,
                max_age_hours=None, min_age_hours=None,
                # Deliberately WIDER than the window. The window now fills every
                # page the run promises, so a pool the same size as the window
                # would always be consumed whole and "load more" would never have
                # older news to reach for.
                # Always one page wider than the window, never clamped: the
                # config validator refuses a window that cannot be served this
                # way, rather than letting "wider" silently become "the same".
                limit=composition.candidate_window_size + composition.page_size,
                before_published_at=before_published, before_story_id=before_story,
                before_source_count=None):
            story_id = row.get("story_id")
            if isinstance(story_id, str):
                merged.setdefault(story_id, row)
        hot_story_ids: set[str] = set()
        for lane in composition.lane_priority:
            if lane in ("interested", "surprise") and not (categories or sources):
                # No profile: the aligned pool degrades to fresh and nothing is
                # "off profile", so neither lane is worth a round trip.
                continue
            # Over-fetch so caps and spacing have something to choose from, and
            # so a lane whose head is all one source is not silently short.
            limit = min(100, max(quotas[lane] * 3, 10))
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
            rows = self._store.retained_candidates_v2(
                category_id=category_id, query=query, lane=lane,
                profile_categories=categories, profile_sources=sources,
                trend_window_hours=composition.trend_window_hours,
                trend_min_sources=composition.trend_min_independent_sources,
                max_age_hours=(composition.updates_max_age_hours if lane == "updates" else
                               composition.trend_window_hours if lane == "hot" else
                               composition.exploration_max_age_hours if lane == "surprise" else None),
                # Lane priority means a story fresh enough to be "fresh" IS fresh.
                # The other three lanes therefore ask for stories past the
                # freshness window, instead of spending their fetch budget on
                # rows the updates lane will claim.
                min_age_hours=None if lane == "updates" else composition.updates_max_age_hours,
                limit=limit, before_published_at=lane_cursor[0], before_story_id=lane_cursor[1],
                before_source_count=lane_cursor[2])
            for row in rows:
                story_id = row.get("story_id")
                if not isinstance(story_id, str):
                    continue
                if lane == "hot":
                    hot_story_ids.add(story_id)
                merged.setdefault(story_id, row)
        return list(merged.values()), hot_story_ids

    def _card(self, row, owner_state, *, lane=None, composition=None, exclusive=False, also_covered_by=()):
        language = str(row["language"])
        titles = row.get("title_translations") or {}
        summaries = row.get("summary_translations") or {}
        if not isinstance(titles, Mapping) or not isinstance(summaries, Mapping):
            raise ValueError("invalid_translation_overlay")
        # Version 3 is version 2 plus the element labels. The reader accepts both
        # for one release, so reader and ranker deploy in any order.
        card = {"card_schema_version": 3 if composition is not None else 2,
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
        # run_id, short_lane_reasons, lane_counts and calibration_alarm are
        # PERSISTED on the frozen order and read back by m2_owner_reading_pages.
        # They are deliberately not in the response: the reader validates its
        # fields exactly, so widening the wire shape would break every card.
        return {key: bindings[key] for key in fields if key in bindings}

    @classmethod
    def _page_response(cls, bindings, cards, cursor, receipt):
        return {"schema_version": receipt.schema_version, **cls._public_bindings(bindings), "cards": cards, "next_cursor": cursor}
