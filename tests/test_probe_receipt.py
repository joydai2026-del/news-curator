from datetime import datetime, timezone

from curator.models import SourceHealth
from scripts.probe_sources import _health_row, build_receipt


def test_probe_receipt_has_all_promised_coverage_counters() -> None:
    rows = [
        {"status": "fresh"},
        {"status": "stale"},
        {"status": "empty"},
        {"status": "unavailable"},
        {"status": "link_resolution_degraded"},
        {"status": "degraded"},
        {"status": "disabled"},
    ]

    receipt = build_receipt(
        rows, probed_at=datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)
    )

    assert receipt["configured"] == receipt["total"] == 7
    assert receipt["attempted"] == 6
    assert receipt["fresh"] == 1
    assert receipt["stale"] == 1
    assert receipt["empty"] == 1
    assert receipt["degraded"] == 5


def test_probe_row_does_not_claim_per_source_timing_from_a_concurrent_batch() -> None:
    row = _health_row(
        SourceHealth(
            source_id="source-a",
            status="fresh",
            usable_items=2,
            newest_at=datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc),
            age_hours=1.0,
            max_age_hours=24.0,
        )
    )

    assert "elapsed_s" not in row
