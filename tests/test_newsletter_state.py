"""The durable cursor: what it stores, what it refuses to store, when it moves."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from curator.newsletter import state as state_module

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_missing_file_is_a_first_run_not_a_crash(tmp_path):
    st = state_module.load(tmp_path / "newsletter_state.json", now=NOW)
    assert st.hashes == []
    assert st.salt
    assert st.watermark == NOW - timedelta(hours=state_module.DEFAULT_LOOKBACK_HOURS)


def test_unreadable_file_starts_a_fresh_window(tmp_path):
    path = tmp_path / "newsletter_state.json"
    path.write_text("{ not json", encoding="utf-8")
    st = state_module.load(path, now=NOW)
    assert st.hashes == []


def test_written_file_has_exactly_the_four_allowed_keys(tmp_path):
    path = tmp_path / "newsletter_state.json"
    st = state_module.load(path, now=NOW)
    state_module.advance(path, st, watermark=NOW, new_hashes=["a" * 64])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == set(state_module.ALLOWED_KEYS)
    assert payload["version"] == 1
    assert payload["hashes"] == ["a" * 64]


def test_round_trip_preserves_watermark_salt_and_hashes(tmp_path):
    path = tmp_path / "newsletter_state.json"
    first = state_module.load(path, now=NOW)
    state_module.advance(path, first, watermark=NOW, new_hashes=["b" * 64, "c" * 64])
    second = state_module.load(path, now=NOW)
    assert second.salt == first.salt
    assert second.watermark == NOW
    assert second.hashes == ["b" * 64, "c" * 64]


def test_advance_deduplicates_and_prunes_oldest_first(tmp_path):
    path = tmp_path / "newsletter_state.json"
    st = state_module.NewsletterState(watermark=NOW, salt="deadbeef", hashes=[f"{i:064d}" for i in range(10)])
    written = state_module.advance(
        path, st, watermark=NOW, new_hashes=[f"{i:064d}" for i in range(8, 14)], max_hashes=8
    )
    assert len(written.hashes) == 8
    assert written.hashes[-1] == f"{13:064d}"
    assert f"{0:064d}" not in written.hashes, "oldest entries fall off the front"
    assert len(set(written.hashes)) == len(written.hashes)


def test_plan_window_overlaps_the_watermark(tmp_path):
    st = state_module.NewsletterState(watermark=NOW - timedelta(hours=1), salt="x")
    start, end = state_module.plan_window(st, NOW, overlap_hours=6)
    assert start == NOW - timedelta(hours=7)
    assert end == NOW


def test_plan_window_caps_a_long_abandoned_cursor():
    st = state_module.NewsletterState(watermark=NOW - timedelta(days=90), salt="x")
    start, _ = state_module.plan_window(st, NOW, overlap_hours=6, max_lookback_hours=48)
    assert start == NOW - timedelta(hours=48)


def test_hash_depends_on_salt_title_and_url():
    a = state_module.story_hash("salt-one", "A headline", "https://x.example/a")
    b = state_module.story_hash("salt-two", "A headline", "https://x.example/a")
    c = state_module.story_hash("salt-one", "A headline", "")
    assert a != b, "the salt must change the digest"
    assert a != c
    assert a == state_module.story_hash("salt-one", "A headline", "https://x.example/a")


def test_hash_is_stable_across_cosmetic_title_differences():
    straight = state_module.story_hash("s", 'The "big" model', "")
    curly = state_module.story_hash("s", "The “big” model", "")
    assert straight == curly


def test_advance_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "newsletter_state.json"
    st = state_module.load(path, now=NOW)
    state_module.advance(path, st, watermark=NOW, new_hashes=["d" * 64])
    assert not (tmp_path / "newsletter_state.json.tmp").exists()
    assert path.exists()


def test_state_file_never_contains_identifying_material(tmp_path):
    """The file is committed to a public repo. Only the four keys go in it."""
    path = tmp_path / "newsletter_state.json"
    st = state_module.load(path, now=NOW)
    digest = st.story_hash("Regulators publish guidance", "https://newsroom.example/a")
    state_module.advance(path, st, watermark=NOW, new_hashes=[digest])
    raw = path.read_text(encoding="utf-8")
    for forbidden in ("@", "Regulators", "newsroom.example", "Subject", "http"):
        assert forbidden not in raw
