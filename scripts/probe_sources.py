#!/usr/bin/env python3
"""Probe configured sources through the production parsers and health rules."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator.config import load_config  # noqa: E402
from curator.pipeline import configured_source_specs  # noqa: E402
from curator.sources import (  # noqa: E402
    SafeHttpPolicy,
    SafeHttpTransport,
    SourceContext,
    SourceQuery,
    build_builtin_registry,
    collect_sources,
)


ACCEPTED_STATUSES = frozenset({"fresh", "link_resolution_degraded", "disabled"})
DEGRADED_STATUSES = frozenset(
    {
        "stale",
        "empty",
        "unavailable",
        "malformed",
        "degraded",
        "link_resolution_degraded",
    }
)


def _accepted(status: str) -> bool:
    return status in ACCEPTED_STATUSES


def _health_row(health) -> dict:
    return {
        "id": health.source_id,
        "type": health.source_type,
        "language": health.language,
        "entries": health.usable_items,
        "newest_at": health.newest_at.isoformat() if health.newest_at else None,
        "age_hours": round(health.age_hours, 2) if health.age_hours is not None else None,
        "max_age_hours": health.max_age_hours,
        "status": health.status,
        "echo_eligible": health.echo_eligible,
        "reason_code": health.reason_code,
        "fresh": health.status == "fresh",
        "ok": _accepted(health.status),
    }


def probe_specs(cfg, specs, *, registry=None, transport=None) -> list[dict]:
    """Exercise the exact registry, transport, and adapters used by collect()."""

    selected_registry = registry or build_builtin_registry()
    selected_transport = transport or SafeHttpTransport(
        policy=SafeHttpPolicy(
            total_timeout_seconds=cfg.timeout,
            max_wire_bytes=cfg.default_source_max_response_bytes,
            max_decoded_bytes=cfg.default_source_max_response_bytes,
            per_host_concurrency=cfg.default_source_per_host_concurrency,
        )
    )
    context = SourceContext(
        registry=selected_registry,
        transport=selected_transport,
        clock=lambda: datetime.now(timezone.utc),
        environment=os.environ.get,
        user_agent=cfg.user_agent,
        queries=tuple(
            SourceQuery(category.id, tuple(category.search_terms))
            for category in cfg.categories
            if category.search_terms
        ),
        default_max_age_hours=cfg.default_source_max_age_hours,
    )
    results = collect_sources(specs, context, max_workers=cfg.fetch_workers)
    return [_health_row(result.health) for result in results]


def build_receipt(rows: list[dict], *, probed_at: datetime | None = None) -> dict:
    """Build the stable machine-readable coverage summary."""

    checked = probed_at or datetime.now(timezone.utc)
    configured = len(rows)
    return {
        "probed_at": checked.isoformat(),
        "configured": configured,
        "attempted": sum(1 for row in rows if row["status"] != "disabled"),
        "total": configured,  # compatibility alias for older receipts
        "fresh": sum(1 for row in rows if row["status"] == "fresh"),
        "stale": sum(1 for row in rows if row["status"] == "stale"),
        "empty": sum(1 for row in rows if row["status"] == "empty"),
        "degraded": sum(1 for row in rows if row["status"] in DEGRADED_STATUSES),
        "sources": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--json", action="store_true", help="emit a machine-readable receipt")
    ap.add_argument("--ids", help="comma-separated configured source ids")
    args = ap.parse_args(argv)

    cfg = load_config(args.root)
    wanted = {entry.strip() for entry in (args.ids or "").split(",") if entry.strip()}
    registry = build_builtin_registry()
    specs = configured_source_specs(cfg, registry)
    known = {spec.id for spec in specs}
    unknown = sorted(wanted - known)
    if unknown:
        ap.error(f"unknown source ids: {', '.join(unknown)}")

    selected = tuple(spec for spec in specs if not wanted or spec.id in wanted)
    rows = probe_specs(cfg, selected, registry=registry)

    receipt = build_receipt(rows)
    configured = receipt["configured"]

    if args.json:
        print(json.dumps(receipt, indent=2))
        return 0 if all(row["ok"] for row in rows) else 1

    print(f"Probed {len(rows)} sources at {receipt['probed_at']}\n")
    print(f"{'id':<18}{'type':<15}{'lang':<7}{'items':>7}{'age':>10}  status")
    print("-" * 76)
    for row in rows:
        age = "-" if row.get("age_hours") is None else f"{row['age_hours']:.1f}h"
        print(
            f"{row['id']:<18}{row.get('type', '-'):<15}{row.get('language', '-'):<7}"
            f"{row.get('entries', 0):>7}{age:>10}  {row.get('status', 'unknown')}"
        )
    print("-" * 76)
    print(
        f"{receipt['fresh']}/{configured} fresh, {receipt['stale']} stale, "
        f"{receipt['empty']} empty, {receipt['degraded']} degraded"
    )
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
