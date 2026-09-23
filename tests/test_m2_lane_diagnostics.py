"""Count-only composition receipts using the captured public corpus."""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from curator.recommendation.composition import load_composition_policy
from curator.recommendation.profile import BehaviorProfile
from curator.recommendation.recipe import build_window


ROOT = Path(__file__).resolve().parents[1]


def captured_rows():
    return json.loads((ROOT / "tests/fixtures/m2-retained-public.json").read_text())["rows"]


def test_window_counts_explain_source_caps_without_counting_backfill_retries():
    from curator.recommendation.lane_diagnostics import LaneDiagnostics

    groups = defaultdict(list)
    for row in captured_rows():
        groups[row["source_id"]].append(row)
    rows = sorted(max(groups.values(), key=len),
                  key=lambda row: row["published_at"], reverse=True)[:4]
    now = datetime.fromisoformat(rows[0]["published_at"])
    policy = load_composition_policy(ROOT / "config/ranking-policy-r2.yaml")
    baseline = build_window(rows, profile=BehaviorProfile(), policy=policy, now=now)
    trace = LaneDiagnostics()
    actual = build_window(rows, profile=BehaviorProfile(), policy=policy, now=now,
                          diagnostics=trace)

    assert actual == baseline
    assert len(actual) == 3
    assert trace.counts["window_input"]["updates"] == 4
    assert trace.counts["window_selected"]["updates"] == 3
    # The same rejected story is tried by both quota admission and backfill.
    assert trace.counts["source_cap_rejected"]["updates"] == 1


def test_finalization_counts_opened_and_duplicate_losses_once_across_pages():
    from curator.recommendation.finalize import finalize_order
    from curator.recommendation.lane_diagnostics import LaneDiagnostics
    from curator.recommendation.recipe import LanedCandidate

    rows = captured_rows()
    first = rows[0]
    second = next(row for row in rows if row["canonical_url"] != first["canonical_url"])
    # Replay real captured content under a controlled lane/owner-state scenario.
    candidate = LanedCandidate(first["story_id"], "hot", 0.0, first)
    opened = LanedCandidate(second["story_id"], "hot", 0.0, second)
    ordered = (candidate, candidate, opened)
    states = {opened.story_id: {"read_at": second["published_at"]}}
    policy = load_composition_policy(ROOT / "config/ranking-policy-r2.yaml")
    baseline = finalize_order(ordered, policy=policy, owner_states=states,
                              page_size=25, pages=4)
    trace = LaneDiagnostics()
    actual = finalize_order(ordered, policy=policy, owner_states=states,
                            page_size=25, pages=4, diagnostics=trace)

    assert actual == baseline
    assert trace.counts["pre_finalize"]["hot"] == 3
    assert trace.counts["opened_removed"]["hot"] == 1
    assert trace.counts["duplicate_removed"]["hot"] == 1
    assert trace.counts["finalized"]["hot"] == 1
    assert trace.counts["finalize_remaining"]["hot"] == 0


def test_service_emits_only_fixed_counts_for_rank_and_continuation(capsys, monkeypatch):
    from tests import test_m2_phase2_service as harness
    from curator.recommendation.lane_diagnostics import LANES, STAGES

    capture = json.loads((ROOT / "tests/fixtures/m2-retained-public.json").read_text())
    now = datetime.fromisoformat(capture["generated_at"])
    monkeypatch.setattr(harness, "NOW", now)
    monkeypatch.setattr(harness, "CLOCK", int(now.timestamp()))
    rows = [{**row, "source_is_aggregator": False,
             "independent_source_count": 0} for row in capture["rows"]]
    store = harness.Store(rows)
    subject = harness.build(store)
    response = harness.rank(subject, store)
    assert response["cards"]
    for _ in range(4):
        if not response.get("next_cursor"):
            break
        response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    receipts = [event for event in events if event.get("event") == "m2_lane_counts"]
    assert {event["phase"] for event in receipts} == {"rank", "continuation"}
    assert store.reservations == []
    for event in receipts:
        assert set(event) == {"event", "phase", "counts"}
        assert set(event["counts"]) == set(STAGES)
        for counts in event["counts"].values():
            assert set(counts) == set(LANES)
            assert all(type(value) is int and value >= 0 for value in counts.values())
        payload = json.dumps(event)
        for row in rows:
            for field in ("story_id", "title", "canonical_url", "source_id"):
                assert row[field] not in payload


def test_finalization_counts_hard_spacing_deferred_candidates_not_as_dropped():
    from curator.recommendation.finalize import finalize_order
    from curator.recommendation.lane_diagnostics import LaneDiagnostics
    from curator.recommendation.recipe import LanedCandidate

    rows = captured_rows()
    first = rows[0]
    second = next(row for row in rows if row["source_id"] == first["source_id"]
                  and row["title"] != first["title"]
                  and row["canonical_url"] != first["canonical_url"])
    ordered = tuple(LanedCandidate(row["story_id"], "hot", 0.0, row)
                    for row in (first, second))
    trace = LaneDiagnostics()
    page = finalize_order(ordered,
        policy=load_composition_policy(ROOT / "config/ranking-policy-r2.yaml"),
        owner_states={}, page_size=25, pages=1, diagnostics=trace)

    assert len(page.cards) == 1
    assert trace.counts["pre_finalize"]["hot"] == 2
    assert trace.counts["opened_removed"]["hot"] == 0
    assert trace.counts["duplicate_removed"]["hot"] == 0
    assert trace.counts["finalized"]["hot"] == 1
    assert trace.counts["finalize_remaining"]["hot"] == 1


def test_log_allowlist_refuses_dynamic_keys_values_and_phase(capsys):
    from curator.recommendation.lane_diagnostics import LaneDiagnostics

    row = captured_rows()[0]
    trace = LaneDiagnostics()
    trace.counts[row["story_id"]] = row
    trace.counts["window_input"][row["source_id"]] = row["canonical_url"]
    trace.counts["window_input"]["hot"] = row["title"]
    trace.counts["window_input"]["updates"] = True
    trace.emit(row["title"])
    assert capsys.readouterr().err == ""
    trace.emit("rank")
    output = capsys.readouterr().err
    assert len(output.splitlines()) == 1
    assert len(output.encode()) < 1500
    receipt = json.loads(output)
    assert receipt["counts"]["window_input"]["hot"] == 0
    assert receipt["counts"]["window_input"]["updates"] == 0
    for key in ("story_id", "source_id", "title", "canonical_url"):
        assert row[key] not in output


@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_unavailable_log_sink_does_not_fail_composition(monkeypatch, error_type):
    from curator.recommendation.lane_diagnostics import LaneDiagnostics

    class UnavailableSink:
        def write(self, value):
            raise error_type(captured_rows()[0]["title"])

    monkeypatch.setattr("sys.stderr", UnavailableSink())
    LaneDiagnostics().emit("continuation")


@pytest.mark.parametrize("failure_at", ("write", "flush"))
def test_sink_failure_preserves_feed_order_and_exactly_one_provider_attempt(monkeypatch, failure_at):
    import sys
    from tests import test_m2_phase2_service as harness

    capture = json.loads((ROOT / "tests/fixtures/m2-retained-public.json").read_text())
    now = datetime.fromisoformat(capture["generated_at"])
    monkeypatch.setattr(harness, "NOW", now)
    monkeypatch.setattr(harness, "CLOCK", int(now.timestamp()))
    rows = [{**row, "source_is_aggregator": False,
             "independent_source_count": 0} for row in capture["rows"]]

    def read_run():
        store = harness.PaidStore(rows)
        subject = harness.paid(store)  # Existing local counting adapter. No network.
        response = harness.rank(subject, store)
        pages = [response["cards"]]
        for _ in range(4):
            if not response.get("next_cursor"):
                break
            response = subject.page(authorization="Bearer valid", cursor=response["next_cursor"])
            pages.append(response["cards"])
        return pages, subject._adapter.calls, len(store.reservations)

    baseline = read_run()
    original = sys.stderr

    class FailingDiagnosticSink:
        fail_flush = False
        failures = 0

        def write(self, value):
            if value.startswith('{"event":"m2_lane_counts",'):
                self.failures += 1
                if failure_at == "write":
                    raise OSError("diagnostic sink unavailable")
                self.fail_flush = True
                return len(value)
            return original.write(value)

        def flush(self):
            if self.fail_flush:
                self.fail_flush = False
                raise ValueError("diagnostic sink closed")
            return original.flush()

    sink = FailingDiagnosticSink()
    monkeypatch.setattr(sys, "stderr", sink)
    actual = read_run()
    assert sink.failures >= 2  # Rank and at least one continuation.
    assert actual == baseline
    assert actual[1:] == (1, 1)


def test_metric_construction_errors_are_not_swallowed():
    from curator.recommendation.lane_diagnostics import LaneDiagnostics

    trace = LaneDiagnostics()
    trace.counts["window_input"] = None
    with pytest.raises(AttributeError):
        trace.emit("rank")


def test_metric_serialization_errors_are_not_mistaken_for_sink_failures(monkeypatch):
    from curator.recommendation.lane_diagnostics import LaneDiagnostics

    def invalid_metric(_value, **_kwargs):
        raise ValueError("metric serialization failed")

    monkeypatch.setattr("curator.recommendation.lane_diagnostics.json.dumps", invalid_metric)
    with pytest.raises(ValueError, match="metric serialization failed"):
        LaneDiagnostics().emit("rank")
