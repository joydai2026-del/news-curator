"""Structured source health receipts and warning-only Actions reporting."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import SourceHealth, TierResult


def _record(health: SourceHealth) -> dict:
    return {
        "source_id": health.source_id,
        "status": health.status,
        "usable_items": health.usable_items,
        "newest_at": health.newest_at.isoformat() if health.newest_at else None,
        "age_hours": round(health.age_hours, 2) if health.age_hours is not None else None,
        "max_age_hours": health.max_age_hours,
        "language": health.language,
        "source_type": health.source_type,
        "echo_eligible": health.echo_eligible,
        "reason_code": health.reason_code,
    }


def write_report(results: list[TierResult], path: Path, *, now: datetime | None = None) -> Path:
    """Write only safe structured fields. URLs and raw exceptions never enter."""
    checked = now or datetime.now(timezone.utc)
    records = [_record(health) for result in results for health in result.source_health]
    records.sort(key=lambda row: row["source_id"])
    payload = {"generated_at": checked.isoformat(), "sources": records}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _format_hours(value) -> str:
    if value is None:
        return "unknown"
    number = float(value)
    return f"{number:g}h"


def _warning_message(row: dict) -> str:
    bits = [str(row.get("status") or "unknown")]
    if row.get("age_hours") is not None:
        bits.append(f"newest {_format_hours(row['age_hours'])} old")
    bits.append(f"threshold {_format_hours(row.get('max_age_hours'))}")
    reason = str(row.get("reason_code") or "")
    if reason:
        bits.append(reason.replace("_", " "))
    return ", ".join(bits)


def render_summary(payload: dict) -> str:
    rows = [
        row
        for row in list(payload.get("sources") or [])
        if row.get("status") != "disabled"
    ]
    fresh = sum(1 for row in rows if row.get("status") == "fresh")
    degraded = [row for row in rows if row.get("status") != "fresh"]
    lines = [
        "## Source freshness",
        "",
        f"{fresh} fresh, {len(degraded)} warning, {len(rows)} checked.",
    ]
    if degraded:
        lines.extend(["", "| Source | Status | Newest age | Threshold |", "|---|---|---:|---:|"])
        for row in sorted(degraded, key=lambda entry: str(entry.get("source_id") or "")):
            lines.append(
                f"| {row.get('source_id', '')} | {row.get('status', '')} | "
                f"{_format_hours(row.get('age_hours'))} | {_format_hours(row.get('max_age_hours'))} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)

    payload = json.loads(args.report.read_text(encoding="utf-8"))
    rows = list(payload.get("sources") or [])
    for row in rows:
        if row.get("status") in {"fresh", "disabled"}:
            continue
        source_id = str(row.get("source_id") or "unknown").replace("%", "%25").replace("\n", "%0A")
        message = _warning_message(row).replace("%", "%25").replace("\n", "%0A")
        print(f"::warning title=News source {source_id}::{message}")

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.summary.open("a", encoding="utf-8") as handle:
        handle.write(render_summary(payload))

    # P1 warning policy is locked: one degraded source never blocks surviving
    # healthy-source publication. The zero-visible guard remains in pipeline.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
