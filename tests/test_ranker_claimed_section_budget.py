"""The claim must outlive everything its holder does, Supabase calls included.

Red before the fix: composition.py Check 10 sized run.ranking_claim_seconds
against the PROVIDER deadline plus the settle window only. The claim holder also
makes a dozen-plus Supabase round trips, each bounded by
supabase.timeout_seconds, so a slow-but-successful request could outlive its own
claim. A second caller then took the claim over and paid the provider again for
the same view, bounded only by the daily USD cap. Found by Codex review of PR
#52 on 2026-09-21, when raising the per-call timeout widened that window.

CLAIMED_SECTION_MAX_TRANSPORT_CALLS is the term Check 10 needs, and a constant
nobody measures is a guess. This file measures it.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

# The harness that already models the whole paid path lives beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from curator.recommendation.composition import CompositionPolicyError, parse_composition_policy
from curator.recommendation.service import (CLAIMED_SECTION_MAX_TRANSPORT_CALLS,
    CONTINUATION_CLAIMED_MAX_TRANSPORT_CALLS)
from curator.recommendation.runtime import claimed_transport_call_budget
from curator.recommendation import runtime

from test_m2_phase2_service import (PaidStore, corpus_row, default_corpus,
    exclusive_corpus, liked_events, paid, rank)

CLAIM_METHOD = "claim_run_ranking"


class CountingStore(PaidStore):
    """Counts every transport method called from the claim onward.

    Wrapping the store rather than the HTTP layer measures base calls. Each
    owner-state read can add configured timeout retries, counted separately
    by the runtime when it sizes the claim and full-request budgets.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "claimed_calls", [])
        object.__setattr__(self, "holding_claim", False)

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name.startswith("_") or name in {"claimed_calls", "holding_claim"} or not callable(value):
            return value

        def counted(*args, **kwargs):
            if name == CLAIM_METHOD:
                object.__setattr__(self, "holding_claim", True)
            if object.__getattribute__(self, "holding_claim"):
                object.__getattribute__(self, "claimed_calls").append(name)
            return value(*args, **kwargs)

        return counted


def _longest_path_calls(capsys):
    """The longest measured paid path, including the accepted exclusion limit."""
    store = CountingStore(events=liked_events())
    store.exclusive = list(store.rows[:5])
    subject = paid(store, exclusive_category="only-other-language-press", promote=5)
    result = rank(subject, store)
    assert result["result_mode"] == "model", "the paid path must actually be reached"
    general = list(store.claimed_calls)

    rows = exclusive_corpus(1151)
    for row in rows[:1100]:
        row["title_translations"] = {}
        row["summary_translations"] = {}
    exclusive_store = CountingStore(events=liked_events(), exclusive=rows)
    exclusive_subject = paid(exclusive_store, exclusive_category="only-other-language-press")
    result = rank(exclusive_subject, exclusive_store,
                  eligibility={"category": "only-other-language-press", "query": None})
    assert result["result_mode"] == "model"

    rows = [corpus_row(index, hours=1 + index / 100,
                       source=f"excluded-{index}", categories=[f"topic-{index}"])
            for index in range(1125)]
    excluded_store = CountingStore(rows, events=liked_events())
    excluded_subject = paid(excluded_store)
    result = rank(excluded_subject, excluded_store,
                  exclude_story_ids=[row["story_id"] for row in rows[:1000]])
    assert result["result_mode"] == "model" and len(result["cards"]) == 25

    rows = [corpus_row(index, hours=1 + index / 100,
                       source=f"src-{index}",
                       categories=["world"] if 700 <= index < 800 else [f"topic-{index}"],
                       independent=4 if 600 <= index < 700 else 1)
            for index in range(1125)]
    exclusive = exclusive_corpus(115)
    for index, row in enumerate(exclusive):
        row["story_id"] = f"story:{2000 + index:064x}"
    for row in exclusive[:100]:
        row["title_translations"] = {}
        row["summary_translations"] = {}
    combined_store = CountingStore(rows, events=liked_events(), exclusive=exclusive)
    combined_subject = paid(combined_store, exclusive_category="only-other-language-press")
    result = rank(combined_subject, combined_store,
                  exclude_story_ids=[row["story_id"] for row in rows[:1000]])
    assert result["result_mode"] == "model" and len(result["cards"]) == 25
    assert combined_store.claimed_calls.count("retained_candidates_v2") == 5
    assert len(combined_store.claimed_calls) == 17
    assert combined_store.claimed_calls.count("opened_candidate_ids") == 1
    rollback_store = CountingStore(rows, events=liked_events(), exclusive=exclusive)
    rollback_subject = paid(rollback_store, exclusive_category="only-other-language-press")
    rollback_subject._policy = replace(rollback_subject._policy,
        composition=replace(rollback_subject._policy.composition,
                            general_pool_batch_limit=100))
    result = rank(rollback_subject, rollback_store,
                  exclude_story_ids=[row["story_id"] for row in rows[:1000]])
    assert result["result_mode"] == "model" and len(result["cards"]) == 25
    assert rollback_store.claimed_calls.count("retained_candidates_v2") == 5
    assert len(rollback_store.claimed_calls) == 17
    return max((general, list(exclusive_store.claimed_calls),
                list(excluded_store.claimed_calls),
                list(combined_store.claimed_calls),
                list(rollback_store.claimed_calls)), key=len)


def test_the_claimed_section_call_count_is_measured_not_assumed(capsys):
    calls = _longest_path_calls(capsys)
    breakdown = Counter(calls)
    with capsys.disabled():
        print("\n===== claimed-section Supabase calls on the longest path =====")
        for name, count in sorted(breakdown.items()):
            print(f"  {count:>2} x {name}")
        print(f"  total: {len(calls)}   constant: {CLAIMED_SECTION_MAX_TRANSPORT_CALLS}")

    assert len(calls) <= CLAIMED_SECTION_MAX_TRANSPORT_CALLS, (
        f"the claimed section makes {len(calls)} Supabase calls, above the "
        f"CLAIMED_SECTION_MAX_TRANSPORT_CALLS of {CLAIMED_SECTION_MAX_TRANSPORT_CALLS} "
        "that sizes run.ranking_claim_seconds. Raise the constant AND the claim "
        "together (composition.py Check 10 will refuse the boot otherwise), or "
        "take the call back out of the claimed section.")
    # This first-page path is no longer the maximum claim holder. The same
    # floor must cover a valid high-continuation setting and its retries.
    assert CLAIMED_SECTION_MAX_TRANSPORT_CALLS == CONTINUATION_CLAIMED_MAX_TRANSPORT_CALLS
    assert calls[0] == CLAIM_METHOD, "the count must start at the claim itself"


def test_broad_negative_scan_stays_inside_claim_window():
    rows = [corpus_row(index, hours=1 + index, source=f"blocked-{index}",
                       categories=["blocked-topic"])
            for index in range(500)]
    feedback = {"event_id": "broad-feedback", "event_type": "less_like_this",
                "event_revision": 10, "occurred_at": rows[0]["published_at"],
                "payload": {"story_id": rows[0]["story_id"],
                            "topic_id": "blocked-topic", "surface": "reader"},
                "story_title": "", "story_summary": "", "source_id": rows[0]["source_id"]}
    store = CountingStore(rows, events=liked_events() + [feedback])
    subject = paid(store)

    response = rank(subject, store)

    assert response["cards"] == []
    assert len(store.claimed_calls) <= CLAIMED_SECTION_MAX_TRANSPORT_CALLS


def test_paid_rank_with_suppressed_heads_stays_inside_claim_window():
    rows = []
    for lane, count, hour, independent, categories in (
        ("fresh", 42, 1, 1, []),
        ("hot", 24, 12, 4, []),
        ("interested", 66, 20, 1, ["world"]),
        ("surprise", 18, 30, 1, []),
    ):
        for hidden in (True, False):
            for ordinal in range(count):
                index = len(rows)
                row = corpus_row(index, hours=hour + int(not hidden),
                                 source=f"{lane}-{hidden}-{ordinal}",
                                 categories=categories + (["blocked-topic"] if hidden else [f"topic-{index}"]),
                                 independent=independent)
                rows.append(row)
    feedback = {"event_id": "broad-feedback", "event_type": "less_like_this",
                "event_revision": 10, "occurred_at": rows[0]["published_at"],
                "payload": {"story_id": rows[0]["story_id"],
                            "topic_id": "blocked-topic", "surface": "reader"},
                "story_title": "", "story_summary": "", "source_id": rows[0]["source_id"]}
    store = CountingStore(rows, events=liked_events() + [feedback])
    subject = paid(store)

    response = rank(subject, store)

    assert response["result_mode"] == "model"
    assert len(response["cards"]) == 25
    assert store.claimed_calls.count("retained_candidates_v2") == 5
    # SQL now removes suppressed heads before each lane's LIMIT, so each pool
    # fills in one call on this fixture without weakening the claim ceiling.
    assert len(store.claimed_calls) <= CLAIMED_SECTION_MAX_TRANSPORT_CALLS


def test_the_claim_covers_the_measured_section_at_the_shipped_values():
    """The shipped numbers satisfy the rule they are validated by."""
    _, policy = runtime.load_ranker_policy({}, root=Path(__file__).resolve().parents[1])
    calls = claimed_transport_call_budget(policy) + runtime.supabase_timeout_retries(policy)
    deadline, settle, timeout, margin, claim = 25, 5, 5, 10, 210
    assert claim > deadline + settle + calls * timeout + margin


def test_a_continuation_reads_owner_state_twice_but_ranking_reads_it_once():
    store = CountingStore(default_corpus() + [
        corpus_row(300 + index, hours=40 + index,
                   source=f"older{index}", categories=[f"o{index % 7}"])
        for index in range(60)], events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    assert first["result_mode"] == "model"
    assert store.claimed_calls.count("owner_states") == 1
    frozen = store.frozen["frozen-1"]
    assert frozen["bindings"]["corpus_has_more"]
    store.claimed_calls.clear()
    response = subject.page(authorization="Bearer valid", cursor=subject._cursor(
        "frozen-1", len(frozen["cards"]), int(frozen["expires_at"]), response_number=2))
    assert response["cards"] and store.extensions
    assert store.claimed_calls.count("owner_states") == 2
    assert runtime.MAX_OWNER_STATE_READS_PER_REQUEST == 2


def test_two_real_continuation_scans_share_one_claim_and_one_response_slot():
    # Seven repeated sources make the first actual corpus append short. The
    # next older, diverse tail must top it up without another paid rank.
    rows = [corpus_row(index, hours=1 + index,
                       source=(f"head-{index}" if index < 50 else
                               f"cluster-{index % 7}" if index < 225 else f"tail-{index}"),
                       categories=[f"topic-{index}"])
            for index in range(350)]
    store = CountingStore(rows, events=liked_events())
    subject = paid(store)
    first = rank(subject, store)
    second = subject.page(authorization="Bearer valid", cursor=first["next_cursor"])
    store.claimed_calls.clear()
    store.extensions.clear()

    third = subject.page(authorization="Bearer valid", cursor=second["next_cursor"])
    assert len(third["cards"]) == 25
    assert len(store.extensions) == 2, store.extensions
    claimed = store.claimed_calls[
        store.claimed_calls.index("claim_run_ranking"):
        store.claimed_calls.index("release_run_ranking_claim") + 1]
    assert len(claimed) == 21, Counter(claimed)
    assert claimed.count("opened_candidate_ids") == 2
    assert len(claimed) <= CONTINUATION_CLAIMED_MAX_TRANSPORT_CALLS
    assert store.claimed_calls.count("claim_run_ranking") == 1
    assert next(iter(store.views.values()))["pages_served"] == 3
    replay = subject.page(authorization="Bearer valid", cursor=second["next_cursor"])
    assert [card["story_id"] for card in replay["cards"]] == [
        card["story_id"] for card in third["cards"]]
    assert next(iter(store.views.values()))["pages_served"] == 3
    assert subject._adapter.calls == 1


def test_empty_high_exclusion_scan_does_not_add_a_claimed_progress_read():
    store = CountingStore(events=liked_events())
    subject = paid(store)
    rank(subject, store)
    frozen = store.frozen["frozen-1"]
    frozen["bindings"]["corpus_has_more"] = True
    frozen["bindings"]["excluded_story_ids"] = [
        f"story:{index + 10000:064x}" for index in range(1000)]
    next(iter(store.views.values()))["pages_served"] = 2
    store.claimed_calls.clear()
    subject._continue_frozen_order = lambda *args, **kwargs: ((), True)

    subject.page(authorization="Bearer valid", cursor=subject._cursor(
        "frozen-1", len(frozen["cards"]), int(frozen["expires_at"]), response_number=3))

    claimed = store.claimed_calls[
        store.claimed_calls.index("claim_run_ranking"):
        store.claimed_calls.index("release_run_ranking_claim") + 1]
    assert claimed.count("load_frozen_order") == 2, Counter(claimed)
    assert len(claimed) <= CONTINUATION_CLAIMED_MAX_TRANSPORT_CALLS


def test_lowering_exclusive_scan_never_under_sizes_the_general_paid_path():
    policy = {"exclusive_scan_max_batches": 1,
              "exclusive_continuation_max_batches": 10}
    assert claimed_transport_call_budget(policy) == CLAIMED_SECTION_MAX_TRANSPORT_CALLS
    policy["exclusive_scan_max_batches"] = 20
    assert claimed_transport_call_budget(policy) == CLAIMED_SECTION_MAX_TRANSPORT_CALLS


def _document(**overrides):
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    document = yaml.safe_load((root / "config/ranking-policy-r2.yaml").read_text())
    for dotted, value in overrides.items():
        section, key = dotted.split(".", 1)
        document[section][key] = value
    return document


def test_a_claim_that_cannot_cover_the_supabase_budget_refuses_the_boot():
    """The red this whole file exists for."""
    with pytest.raises(CompositionPolicyError, match="claimed-section Supabase budget"):
        parse_composition_policy(_document(**{"run.ranking_claim_seconds": 60}),
            provider_deadline_seconds=6, settle_window_seconds=5,
            supabase_timeout_seconds=5,
            claimed_section_transport_calls=CLAIMED_SECTION_MAX_TRANSPORT_CALLS)


def test_raising_the_per_call_timeout_alone_refuses_the_boot():
    """Raising supabase.timeout_seconds without raising the claim is the exact
    mistake PR #52 shipped in its first draft. It is now a refused boot."""
    with pytest.raises(CompositionPolicyError, match="claimed-section Supabase budget"):
        parse_composition_policy(_document(),
            provider_deadline_seconds=6, settle_window_seconds=5,
            supabase_timeout_seconds=10,
            claimed_section_transport_calls=CLAIMED_SECTION_MAX_TRANSPORT_CALLS)


def test_the_shipped_policy_passes_the_check_it_is_validated_by():
    policy = parse_composition_policy(_document(), provider_deadline_seconds=6,
        settle_window_seconds=5, supabase_timeout_seconds=5,
        claimed_section_transport_calls=CLAIMED_SECTION_MAX_TRANSPORT_CALLS)
    assert policy is not None


def test_the_error_names_every_term_so_the_fix_is_obvious():
    with pytest.raises(CompositionPolicyError) as raised:
        parse_composition_policy(_document(**{"run.ranking_claim_seconds": 60}),
            provider_deadline_seconds=6, settle_window_seconds=5, supabase_timeout_seconds=5,
            claimed_section_transport_calls=CLAIMED_SECTION_MAX_TRANSPORT_CALLS)
    message = str(raised.value)
    for term in ("run.ranking_claim_seconds", "provider deadline", "settle window",
                 "claimed-section Supabase budget", "run.ranking_claim_margin_seconds"):
        assert term in message, f"the error does not name {term}: {message}"
