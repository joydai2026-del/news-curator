#!/usr/bin/env python3
"""Collect or validate the canonical no-secret source snapshot."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator.config import ConfigError, load_config  # noqa: E402
from curator.pipeline import collect  # noqa: E402
from curator.source_snapshot import (  # noqa: E402
    SourceSnapshotError,
    load_source_snapshot,
    snapshot_config_digest,
    write_source_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "validate"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.root)
        digest = snapshot_config_digest(cfg)
        if args.command == "collect":
            now = datetime.now(timezone.utc)
            results = collect(cfg, offline=args.offline)
            write_source_snapshot(
                results,
                args.snapshot,
                generated_at=now,
                configuration_digest=digest,
            )
            items = sum(len(result.items) for result in results)
            print(f"source snapshot written: {items} items")
        else:
            snapshot = load_source_snapshot(
                args.snapshot, expected_configuration_digest=digest
            )
            items = sum(len(result.items) for result in snapshot.results)
            print(
                "source snapshot valid: "
                f"{items} items, digest {snapshot.content_digest[:12]}"
            )
    except (ConfigError, SourceSnapshotError, OSError) as exc:
        print(f"source snapshot unavailable: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
