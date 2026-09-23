"""Opened stories must not spend the unread feed's admission capacity."""

from dataclasses import replace

import pytest

from tests import test_m2_phase2_service as harness


def hot_rows():
    # Established corpus fixture: one aggregator, two capped slots, unread tail.
    return [harness.corpus_row(900 + index, hours=12, source="hot-wire",
                              categories=[f"hot-topic-{index}"], aggregator=True,
                              independent=10 - index)
            for index in range(6)]


class OpenedStore(harness.PaidStore):
    def __init__(self, rows, opened):
        super().__init__(rows, events=harness.liked_events())
        self.opened = set(opened)

    def owner_states(self, token, story_ids):
        return {story_id: {"read_at": harness.NOW.isoformat()}
                for story_id in story_ids if story_id in self.opened}

    def opened_candidate_ids(self, token, story_ids):
        return self.opened.intersection(story_ids)


def test_rank_opened_hot_does_not_consume_source_cap_or_paid_window():
    hot = hot_rows()
    opened = {row["story_id"] for row in hot[:2]}
    rows = [row for row in harness.default_corpus()
            if row["independent_source_count"] < 2] + hot
    store = OpenedStore(rows, opened)
    subject = harness.paid(store)
    response = harness.rank(subject, store)
    cards = store.frozen["frozen-1"]["cards"]
    served_hot = [card for card in cards if card["lane"] == "hot"]
    assert len(served_hot) == 2
    assert not opened.intersection(card["story_id"] for card in cards)
    assert subject._adapter.calls == len(store.reservations) == 1
    store.opened.clear()
    replay = harness.rank(subject, store)
    assert replay["cards"] == response["cards"]
    assert subject._adapter.calls == len(store.reservations) == 1


def test_unavailable_opened_lookup_fails_before_paid_reservation():
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    def unavailable(*_args):
        raise RuntimeError("lookup unavailable")
    store.opened_candidate_ids = unavailable
    with pytest.raises(RuntimeError, match="lookup unavailable"):
        harness.rank(subject, store)
    assert subject._adapter.calls == 0 and store.reservations == []


def test_open_during_provider_is_still_removed_by_authoritative_final_check():
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    original = subject._adapter.rank
    opened = store.rows[0]["story_id"]
    def racing_provider(*args, **kwargs):
        store.opened.add(opened)
        return original(*args, **kwargs)
    subject._adapter.rank = racing_provider
    harness.rank(subject, store)
    assert opened not in {card["story_id"] for card in store.frozen["frozen-1"]["cards"]}
    assert subject._adapter.calls == len(store.reservations) == 1


def test_full_policy_range_fits_one_opened_lookup():
    from curator.recommendation.composition import _NUMERIC_RANGES
    # Conservative independent maxima exceed every legal ratio combination:
    # four lane fetches <=100 each, window<=100, page<=25, promotion<=20,
    # and pending grows for at most20 pages. Even this envelope fits10000.
    # Use the coupled window bound instead of combining contradictory extrema.
    _, minimum_window, maximum_window = _NUMERIC_RANGES["composition.candidate_window_size"]
    maximum_page = _NUMERIC_RANGES["composition.page_size"][2]
    maximum_pages = _NUMERIC_RANGES["run.max_pages_per_run"][2]
    maximum_promotion = _NUMERIC_RANGES["lane.exclusive_promote_to_all_max"][2] * 4
    maximum_lane = harness.RankingService._lane_fetch_limit(maximum_window)
    for window in range(minimum_window, maximum_window + 1):
        fetched = window + maximum_page + 4 * maximum_lane + maximum_promotion
        pending = (fetched - window) * maximum_pages
        assert pending + fetched <= 10000


def test_opened_lookup_claim_budget_includes_two_extra_continuation_calls():
    from curator.recommendation import runtime
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    _, policy = runtime.load_ranker_policy({}, root=root)
    # Config gate: two non-retried pre-admission reads add two calls to the
    # existing accepted-policy envelope. The approved claim is220 seconds.
    assert runtime.claimed_transport_call_budget(policy) >= 33
    composition = harness.load_composition_policy(harness.POLICY_PATH)
    required = (policy["deadline_seconds"] + policy["settle_window_seconds"]
                + (33 + runtime.supabase_timeout_retries(policy))
                * runtime.supabase_timeout_seconds(policy)
                + composition.ranking_claim_margin_seconds)
    assert composition.ranking_claim_seconds > required


def test_four_unread_hot_are_served_when_two_source_buckets_make_it_feasible():
    hot = hot_rows()
    other = [dict(row, story_id=f"story:{1200 + index:064x}",
                  canonical_url=f"https://example.test/other-{index}",
                  title=f"Other headline {index}", source_id="other-hot-wire")
             for index, row in enumerate(hot)]
    rows = [row for row in harness.default_corpus() if row["independent_source_count"] < 2]
    opened = [row["story_id"] for row in hot[:2] + other[:2]]
    store = OpenedStore(rows + hot + other, opened)
    subject = harness.paid(store)
    result = harness.rank(subject, store)
    assert sum(card["lane"] == "hot" for card in result["cards"]) == 4
    assert subject._adapter.calls == len(store.reservations) == 1


def test_opened_prefilter_can_be_disabled_by_existing_policy():
    hot = hot_rows()
    store = OpenedStore(hot, [row["story_id"] for row in hot[:2]])
    subject = harness.paid(store)
    subject._policy = replace(subject._policy, composition=replace(
        subject._policy.composition, hide_already_opened=False))
    def unexpected(*_args):
        raise AssertionError("disabled policy must not read opened IDs")
    store.opened_candidate_ids = unexpected
    result = harness.rank(subject, store)
    assert store.opened.intersection(card["story_id"] for card in result["cards"])


def test_sql_filtered_opened_hot_tail_cannot_resurrect_as_pending():
    hot = hot_rows()
    baseline = [row for row in harness.default_corpus() if row["independent_source_count"] < 2]
    baseline += [harness.corpus_row(1100 + index, hours=50, source=f"tail-{index}",
                                  categories=[f"tail-{index}"]) for index in range(30)]
    store = OpenedStore(baseline + hot, [row["story_id"] for row in hot])
    subject = harness.paid(store)
    harness.rank(subject, store)
    bindings = store.frozen["frozen-1"]["bindings"]
    # SQL returned no unread Hot row, so there is no Hot keyset to advance.
    assert "hot" not in bindings["corpus_cursor"]
    assert not store.opened.intersection(row["story_id"] for row in bindings["pending_candidates"])


def test_opened_exclusive_rows_advance_safe_cursor():
    exclusive = harness.exclusive_corpus(60)
    store = OpenedStore([], [row["story_id"] for row in exclusive[:51]])
    store.exclusive = exclusive
    subject = harness.paid(store, exclusive_category="only-other-language-press")
    result = harness.rank(subject, store,
                          eligibility={"category": "only-other-language-press", "query": None})
    bindings = store.frozen["frozen-1"]["bindings"]
    assert result["cards"] == []
    # The 51st row is the existing look-ahead sentinel, not a consumed row.
    assert bindings["corpus_cursor"]["before_story_id"] == exclusive[49]["story_id"]
    assert not store.opened.intersection(row["story_id"] for row in bindings["pending_candidates"])
    assert subject._adapter.calls == 0 and store.reservations == []


def test_continuation_opened_hot_does_not_consume_source_cap():
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    harness.rank(subject, store)
    subject._policy = replace(subject._policy, composition=replace(
        subject._policy.composition, continuation_refill_max_passes=1))
    hot = hot_rows()
    store.opened = {row["story_id"] for row in hot[:2]}
    frozen = store.frozen["frozen-1"]
    separators = [harness.corpus_row(950 + index, hours=30, source=f"separator-{index}",
                                    categories=[f"separator-{index}"])
                  for index in range(5)]
    frozen["bindings"].update(pending_candidates=hot + separators,
                              corpus_cursor={"pending_only": True}, corpus_has_more=True)
    before = len(frozen["cards"])
    response = subject.page(authorization="Bearer valid", cursor=subject._cursor(
        "frozen-1", before, int(frozen["expires_at"]), response_number=2))
    served_hot = [card for card in response["cards"] if card["lane"] == "hot"]
    assert len(served_hot) == 2
    assert not store.opened.intersection(card["story_id"] for card in response["cards"])
    assert not store.opened.intersection(row["story_id"] for row in
        store.frozen["frozen-1"]["bindings"]["pending_candidates"])
    assert subject._adapter.calls == len(store.reservations) == 1


def test_continuation_late_open_still_uses_authoritative_owner_states():
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    harness.rank(subject, store)
    hot = hot_rows()
    frozen = store.frozen["frozen-1"]
    frozen["bindings"].update(pending_candidates=hot, corpus_cursor={"pending_only": True},
                              corpus_has_more=True)
    def race(_token, ids):
        store.opened.update(ids)
        return set()
    store.opened_candidate_ids = race
    response = subject.page(authorization="Bearer valid", cursor=subject._cursor(
        "frozen-1", len(frozen["cards"]), int(frozen["expires_at"]), response_number=2))
    assert response["cards"] == []
    assert subject._adapter.calls == len(store.reservations) == 1


def test_continuation_opened_lookup_timeout_releases_claim_and_retries_same_cursor():
    import copy
    from curator.recommendation.supabase_http import SupabaseHTTPError

    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    harness.rank(subject, store)
    frozen = store.frozen["frozen-1"]
    pending = [harness.corpus_row(1500 + index, hours=30, source=f"retry-{index}",
                                 categories=[f"retry-{index}"]) for index in range(25)]
    frozen["bindings"].update(pending_candidates=pending,
                              corpus_cursor={"pending_only": True}, corpus_has_more=True)
    cursor = subject._cursor("frozen-1", len(frozen["cards"]),
                             int(frozen["expires_at"]), response_number=2)
    view = next(iter(store.views.values()))
    before_order, before_view = copy.deepcopy(frozen), copy.deepcopy(view)
    original = store.opened_candidate_ids
    attempts = []
    def timeout(token, story_ids):
        assert view["claim_token"] is not None
        attempts.append(len(story_ids))
        raise SupabaseHTTPError("Supabase request failed") from TimeoutError("injected timeout")
    store.opened_candidate_ids = timeout

    with pytest.raises(SupabaseHTTPError) as caught:
        subject.page(authorization="Bearer valid", cursor=cursor)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert attempts == [25]
    assert view["claim_token"] is None
    assert view["pages_served"] == before_view["pages_served"]
    assert frozen == before_order
    assert store.extensions == []
    assert subject._adapter.calls == len(store.reservations) == 1

    store.opened_candidate_ids = original
    response = subject.page(authorization="Bearer valid", cursor=cursor)
    assert len(response["cards"]) == 25
    assert view["pages_served"] == 2 and view["claim_token"] is None
    assert subject._decode_cursor(cursor)["response_number"] == 2
    replay = subject.page(authorization="Bearer valid", cursor=cursor)
    assert replay["cards"] == response["cards"]
    assert view["pages_served"] == 2
    assert subject._adapter.calls == len(store.reservations) == 1


def test_opened_hot_fetch_head_does_not_hide_unread_hot_beyond_pool_limit():
    # Residual discovery gate: 25-window policy fetches12 Hot rows. The
    # general pool fills with newer Updates, so the13th Hot has no other path.
    updates = [harness.corpus_row(2000 + index, hours=1, source=f"fresh-{index}",
                                  categories=[f"fresh-{index}"]) for index in range(50)]
    hot = [harness.corpus_row(2100 + index, hours=12, source=f"boundary-hot-{index}",
                              categories=[f"boundary-hot-{index}"], independent=26 - index,
                              aggregator=True)
           for index in range(13)]
    store = OpenedStore(updates + hot, [row["story_id"] for row in hot[:12]])
    subject = harness.paid(store)
    subject._policy = replace(subject._policy, composition=replace(
        subject._policy.composition, candidate_window_size=25))
    calls = []
    original = store.retained_candidates_v2
    def capture(**kwargs):
        rows = original(**kwargs)
        if kwargs["lane"] == "hot":
            calls.append((kwargs["limit"], len(rows)))
        return rows
    store.retained_candidates_v2 = capture
    result = harness.rank(subject, store)
    assert calls == [(12, 1)]
    later = subject.page(authorization="Bearer valid", cursor=result["next_cursor"])
    assert hot[-1]["story_id"] not in {card["story_id"] for card in later["cards"]}
    assert subject._adapter.calls == len(store.reservations) == 1
    # Opened rows must not consume the unchanged 12-row SQL budget.
    assert hot[-1]["story_id"] in {card["story_id"] for card in result["cards"]}


def test_candidate_owner_is_bound_to_verified_auth_not_client_body():
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    received = []
    original = store.retained_candidates_v2
    def capture(**kwargs):
        received.append((kwargs.get("owner_id"), kwargs.get("hide_already_opened")))
        return original(**kwargs)
    store.retained_candidates_v2 = capture
    harness.rank(subject, store, owner_id="22222222-2222-2222-2222-222222222222",
                 user_id="22222222-2222-2222-2222-222222222222",
                 hide_already_opened=False,
                 eligibility={"category": None, "query": None,
                              "owner_id": "22222222-2222-2222-2222-222222222222"})
    assert received and set(received) == {(harness.OWNER_ID, True)}


def test_continuation_candidate_owner_comes_from_authentication():
    rows = harness.default_corpus() + [harness.corpus_row(
        3000 + index, hours=50 + index, source=f"owner-tail-{index}",
        categories=[f"owner-tail-{index}"]) for index in range(70)]
    store = OpenedStore(rows, ())
    subject = harness.paid(store)
    harness.rank(subject, store)
    frozen = store.frozen["frozen-1"]
    received = []
    original = store.retained_candidates_v2
    def capture(**kwargs):
        received.append((kwargs.get("owner_id"), kwargs.get("hide_already_opened")))
        return original(**kwargs)
    store.retained_candidates_v2 = capture
    cursor = subject._cursor("frozen-1", len(frozen["cards"]),
                             int(frozen["expires_at"]), response_number=2)
    subject.page(authorization="Bearer valid", cursor=cursor)
    assert received and set(received) == {(harness.OWNER_ID, True)}
    assert subject._adapter.calls == len(store.reservations) == 1


def test_two_verified_owners_share_one_service_without_pool_identity_bleed():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    other = "22222222-2222-2222-2222-222222222222"
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    subject._policy = replace(subject._policy, preview_owner_ids=(harness.OWNER_ID, other))
    class TwoOwnerAuth:
        def get_user(self, token):
            return {"id": {"owner-a": harness.OWNER_ID, "owner-b": other}[token]}
    subject._auth = TwoOwnerAuth()
    barrier = Barrier(2, timeout=2)
    received = []
    def capture(**kwargs):
        if kwargs["lane"] is None:
            barrier.wait()
        received.append((kwargs["query"], kwargs.get("owner_id")))
        return []
    store.retained_candidates_v2 = capture
    def request(token):
        _, owner = subject._authenticate("Bearer " + token)
        return subject._pool_rows(None, token, harness.BehaviorProfile(),
            subject._policy.composition, None, None, owner=owner)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(request, ("owner-a", "owner-b")))
    assert set(received) == {("owner-a", harness.OWNER_ID), ("owner-b", other)}
    assert subject._adapter.calls == 0 and store.reservations == []


def test_missing_verified_pool_owner_fails_before_any_acquisition():
    from curator.recommendation.service import AuthenticationError
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    calls = []
    store.retained_candidates_v2 = lambda **kwargs: calls.append(kwargs) or []
    with pytest.raises((TypeError, AuthenticationError)):
        subject._pool_rows(None, None, harness.BehaviorProfile(),
                           subject._policy.composition, None, None)
    assert calls == [] and store.reservations == []


def test_forged_cursor_owner_cannot_trigger_acquisition():
    import base64
    import json
    store = OpenedStore(harness.default_corpus(), ())
    subject = harness.paid(store)
    result = harness.rank(subject, store)
    cursor = result["next_cursor"]
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = json.loads(decoded[:-32])
    payload["owner_id"] = "22222222-2222-2222-2222-222222222222"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode() + decoded[-32:]).decode()
    calls = []
    store.retained_candidates_v2 = lambda **kwargs: calls.append(kwargs) or []
    with pytest.raises(harness.StaleRankingError, match="invalid_cursor"):
        subject.page(authorization="Bearer valid", cursor=forged)
    assert calls == [] and subject._adapter.calls == len(store.reservations) == 1
