"""One run's share of the pairing work, and the corpus write that precedes it.

2026-09-17: the first key-backed ingest read 6,443 corpus rows and then paired
hundreds of stories one call at a time. The job hit its 14-minute timeout and was
cancelled, and because the corpus rows were written AFTER translation, the hour's
corpus write went with it. Two rules come out of that, and these tests hold them:

  1. the corpus is written before any pairing or translation, so a cancel after
     that point costs an overlay, never the stories, and
  2. the pairing loop takes a bounded share of the run, persists every decision
     it makes, and leaves the rest for the next run.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.config import load_config
from curator.grouping import GroupingCandidate
from curator.models import Item, TierResult
from curator.pipeline import configured_source_specs
from curator.source_snapshot import snapshot_config_digest, write_source_snapshot
from curator.translation.pairing import (PairingPolicy, UNDECIDED, decide_exclusivity)

import scripts.retained_corpus_ingest as ingest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 1. Corpus first.
# --------------------------------------------------------------------------

def _snapshot(tmp_path):
    cfg = load_config(ROOT)
    spec = next(spec for spec in configured_source_specs(cfg) if spec.enabled)
    generated_at = datetime.now(timezone.utc)
    item = Item(title="A corpus row that must survive a cancelled run",
                url="https://example.com/survivor", canonical_url="https://example.com/survivor",
                source_id=spec.id, source_name="Fixture", published_at=generated_at,
                language="en", description="Body text.")
    path = tmp_path / "source-snapshot.json"
    write_source_snapshot([TierResult(tier="rss", items=[item])], path,
                          generated_at=generated_at,
                          configuration_digest=snapshot_config_digest(cfg))
    return path


def test_a_translator_that_raises_still_leaves_the_corpus_ingested(tmp_path, monkeypatch):
    """The red test for the 2026-09-17 loss: everything after the corpus write
    may fail, be cancelled or time out, and the rows are already in."""
    snapshot = _snapshot(tmp_path)
    written = []
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr(ingest, "ingest_corpus_rows",
                        lambda url, key, rows: written.extend(rows))
    monkeypatch.setattr(ingest, "read_corpus_window", lambda *a, **k: ((), False))
    monkeypatch.setattr(ingest, "read_exclusivity_decisions", lambda *a, **k: {})
    monkeypatch.setattr(ingest, "apply_overlay_rows",
                        lambda url, key, rows: pytest.fail("overlay ran after a failed translation"))

    def explode(*args, **kwargs):
        raise RuntimeError("the model pairing loop died")

    monkeypatch.setattr(ingest, "translate_rows", explode)
    monkeypatch.setattr("sys.argv", ["retained_corpus_ingest.py", "ingest", "--root", str(ROOT),
                                     "--source-snapshot", str(snapshot)])
    with pytest.raises(RuntimeError):
        ingest.main()
    assert [row["canonical_url"] for row in written] == ["https://example.com/survivor"]
    # The corpus write carries no overlay: that is step two's only job.
    assert "title_translations" not in written[0]


def test_the_overlay_is_a_narrow_second_write_not_a_second_corpus_ingest(tmp_path, monkeypatch):
    """A second m2_ingest_retained_corpus call with the same rows would be a
    no-op (its upsert requires a NEWER source_observed_at), so the overlay has
    its own RPC, and it carries only the columns pairing decided."""
    snapshot = _snapshot(tmp_path)
    overlay_calls, corpus_calls = [], []
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr(ingest, "ingest_corpus_rows",
                        lambda url, key, rows: corpus_calls.append(rows))
    monkeypatch.setattr(ingest, "read_corpus_window", lambda *a, **k: ((), False))
    monkeypatch.setattr(ingest, "read_exclusivity_decisions", lambda *a, **k: {})
    monkeypatch.setattr(ingest, "apply_overlay_rows",
                        lambda url, key, rows: overlay_calls.append(rows) or len(rows))

    def translated(cfg, rows, **kwargs):
        from dataclasses import replace
        return tuple(replace(row, title_translations={"en": "Translated"}) for row in rows), "translated=1"

    monkeypatch.setattr(ingest, "translate_rows", translated)
    monkeypatch.setattr("sys.argv", ["retained_corpus_ingest.py", "ingest", "--root", str(ROOT),
                                     "--source-snapshot", str(snapshot)])
    assert ingest.main() == 0
    assert len(corpus_calls) == 1
    assert overlay_calls == [[{"story_id": corpus_calls[0][0]["story_id"],
                               "title_translations": {"en": "Translated"}}]]


def test_the_overlay_rpc_merges_and_never_erases():
    sql = (ROOT / "supabase/migrations/202609170001_m2_retained_overlay.sql").read_text(encoding="utf-8")
    assert "title_translations = o.title_translations || coalesce(row->'title_translations'" in sql
    assert "summary_translations = o.summary_translations || coalesce(row->'summary_translations'" in sql
    assert "event_group_id = coalesce(o.event_group_id, row->>'event_group_id')" in sql
    # It updates rows the corpus write created; it never inserts one itself.
    assert "insert into public.retained_corpus_observations" not in sql
    assert "service role required" in sql


# --------------------------------------------------------------------------
# 2. The per-run bound.
# --------------------------------------------------------------------------

class CountingPairing:
    provider_id = "openai"
    model_version = "gpt-5-mini:pairing-json-v1"

    def __init__(self, *, seconds_per_call=0.0, clock=None):
        self.asked, self._seconds, self._clock = [], seconds_per_call, clock

    def decide(self, *, story, context):
        self.asked.append(story.story_id)
        if self._clock is not None:
            self._clock.advance(self._seconds)
        return None, 500, 6


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def _story(index, *, language="zh", published_at=None):
    return GroupingCandidate(story_id=f"story-{index:04d}", language=language,
                             title=f"标题 {index}", summary="摘要",
                             published_at=published_at or (NOW - timedelta(minutes=index)),
                             canonical_url=f"https://e.cn/{index}", category_ids=("tech",))


def _english_pool(count=3):
    return [GroupingCandidate(story_id=f"en-{index}", language="en", title=f"English {index}",
                              summary="Summary", published_at=NOW - timedelta(minutes=index),
                              canonical_url=f"https://e.com/{index}", category_ids=("tech",))
            for index in range(count)]


def _policy(**overrides):
    values = {"max_calls_per_run": 40, "run_time_budget_seconds": 300, "model": "gpt-5-mini"}
    values.update(overrides)
    return PairingPolicy(**values)


def test_a_backlog_of_three_hundred_stops_at_the_call_cap_and_persists_every_answer():
    stories = [_story(index) for index in range(300)]
    provider = CountingPairing()
    persisted = []
    result = decide_exclusivity(stories, _english_pool(), display_language="en",
                                policy=_policy(), provider=provider, now=NOW,
                                persist=persisted.append)
    assert len(provider.asked) == 40
    assert result.attempted_calls == 40 and result.calls == 40
    assert len(persisted) == 40 and len(result.decisions) == 40
    assert result.budget_stop == "calls"
    assert result.budget_skipped == 260
    # Nothing is claimed for a story we never asked about.
    assert len(result.undecided) == 260
    assert not (set(result.decisions) & result.undecided)


def test_the_next_run_resumes_with_the_stories_the_last_run_did_not_reach():
    stories = [_story(index) for index in range(300)]
    pool = _english_pool()
    first_provider, first_persisted = CountingPairing(), []
    first = decide_exclusivity(stories, pool, display_language="en", policy=_policy(),
                               provider=first_provider, now=NOW, persist=first_persisted.append)
    decided = {decision.story_id: decision for decision in first_persisted}
    second_provider = CountingPairing()
    second = decide_exclusivity(stories, pool, display_language="en", policy=_policy(),
                                provider=second_provider, now=NOW + timedelta(minutes=15),
                                already_decided=decided, persist=lambda decision: None)
    assert not set(first_provider.asked) & set(second_provider.asked)
    assert len(second_provider.asked) == 40
    # 80 of the 300 are settled after two runs, and the run kept the newest work
    # first: run one asked the newest 40, run two the next 40.
    assert first_provider.asked == [f"story-{index:04d}" for index in range(40)]
    assert second_provider.asked == [f"story-{index:04d}" for index in range(40, 80)]
    # Run two serves 80 settled stories: the 40 it reused and the 40 it asked.
    assert second.budget_stop == "calls" and len(second.decisions) == 80


def test_a_slow_provider_trips_the_time_budget_and_keeps_what_it_answered():
    clock = FakeClock()
    stories = [_story(index) for index in range(300)]
    provider = CountingPairing(seconds_per_call=7.0, clock=clock)
    persisted = []
    result = decide_exclusivity(stories, _english_pool(), display_language="en",
                                policy=_policy(run_time_budget_seconds=30),
                                provider=provider, now=NOW, persist=persisted.append,
                                monotonic=clock)
    # Five 7-second calls fit inside 30 seconds; the sixth is refused.
    assert len(provider.asked) == 5
    assert result.budget_stop == "time"
    assert len(persisted) == 5 and len(result.decisions) == 5
    assert result.budget_skipped == 295
    assert result.elapsed_seconds == pytest.approx(35.0)


def test_the_window_is_worked_before_the_tail_and_newest_first():
    inside = [_story(index, published_at=NOW - timedelta(hours=index)) for index in (5, 1, 3)]
    outside = [_story(90 + index, published_at=NOW - timedelta(hours=60 + index)) for index in (0, 1)]
    provider = CountingPairing()
    decide_exclusivity(outside + inside, _english_pool(), display_language="en",
                       policy=_policy(max_calls_per_run=500, window_hours=48),
                       provider=provider, now=NOW, persist=lambda decision: None)
    assert provider.asked[:3] == ["story-0001", "story-0003", "story-0005"]


def test_a_zero_call_budget_asks_nothing_and_claims_nothing():
    stories = [_story(index) for index in range(3)]
    provider = CountingPairing()
    result = decide_exclusivity(stories, _english_pool(), display_language="en",
                                policy=_policy(max_calls_per_run=0), provider=provider, now=NOW,
                                persist=lambda decision: None)
    assert provider.asked == [] and result.decisions == {}
    assert result.budget_stop == "calls" and result.budget_skipped == 3


@pytest.mark.parametrize("overrides", [
    {"max_calls_per_run": -1}, {"max_calls_per_run": 501},
    {"run_time_budget_seconds": 29}, {"run_time_budget_seconds": 781},
    {"run_time_budget_seconds": 300.0},
])
def test_an_out_of_range_bound_fails_the_boot(overrides):
    with pytest.raises(ValueError):
        _policy(**overrides)


def test_the_bound_is_read_from_config_not_hardcoded():
    policy = PairingPolicy.from_config({"pairing_max_calls_per_run": 7, "run_time_budget_seconds": 45})
    assert policy.max_calls_per_run == 7 and policy.run_time_budget_seconds == 45
    shipped = PairingPolicy.from_config(load_config(ROOT).translation)
    assert shipped.max_calls_per_run == load_config(ROOT).translation["pairing_max_calls_per_run"]


def test_the_default_bound_matches_the_documented_defaults():
    policy = PairingPolicy.from_config({})
    assert policy.max_calls_per_run == 40 and policy.run_time_budget_seconds == 300
    assert decide_exclusivity((), (), display_language="en", policy=policy,
                              provider=CountingPairing(), now=NOW).budget_stop is None
