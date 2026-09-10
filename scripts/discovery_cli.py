#!/usr/bin/env python3
"""Replay public source snapshots into a local M2 discovery receipt.

This command never logs into an account, changes production, or publishes a
receipt. Receipt contents can be private when profile inputs are supplied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator.config import ConfigError, load_config  # noqa: E402
from curator.discovery import (  # noqa: E402
    DiscoveryError, build_discovery, load_discovery_policy, replay_discovery,
)
from curator.source_snapshot import (  # noqa: E402
    load_source_snapshot, snapshot_config_digest,
)


def _write_new(path: Path, payload: dict) -> None:
    """Create a private file without following or overwriting an existing path."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(data + '\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='Build a local receipt at the captured snapshot clock.')
    build.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument('--snapshot', type=Path, required=True)
    build.add_argument('--previous-snapshot', type=Path)
    build.add_argument('--policy', type=Path)
    build.add_argument('--language', choices=('en', 'zh'), default='en')
    build.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify', help='Verify deterministic receipt replay without recollecting.')
    verify.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            if args.receipt.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('receipt exceeds size limit')
            receipt = json.loads(args.receipt.read_text(encoding='utf-8'))
            replay_discovery(receipt)
            print('Discovery receipt internal consistency verified. Source authenticity and production deployment are not certified.')
            return 0
        root = args.root.resolve()
        cfg = load_config(root)
        config_digest = snapshot_config_digest(cfg)
        # Historical replay uses the captured clock, not the age of the file
        # today. The loader still validates shape, digest and configuration.
        if args.snapshot.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("snapshot exceeds size limit")
        raw = json.loads(args.snapshot.read_text(encoding='utf-8'))
        clock = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
        if clock.tzinfo is None or clock > datetime.now(timezone.utc):
            raise ValueError('snapshot claims a future observation')
        snapshot = load_source_snapshot(
            args.snapshot, expected_configuration_digest=config_digest, current_time=clock,
        )
        previous = None
        if args.previous_snapshot:
            if args.previous_snapshot.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("snapshot exceeds size limit")
            prior_raw = json.loads(args.previous_snapshot.read_text(encoding='utf-8'))
            prior_clock = datetime.fromisoformat(prior_raw['generated_at'].replace('Z', '+00:00'))
            previous = load_source_snapshot(
                args.previous_snapshot, expected_configuration_digest=config_digest, current_time=prior_clock,
            )
        policy_path = args.policy or root / 'config/discovery-policy-r2.yaml'
        policy = load_discovery_policy(policy_path)
        receipt = build_discovery(
            cfg, snapshot, policy, previous_snapshot=previous,
            now=clock, language=args.language,
        )
        replay_discovery(receipt)
        _write_new(args.output, receipt)
        # Aggregate-only reporting. Never emit story locators or profile values.
        counts = {lane: 0 for lane in ('updates', 'hot', 'interested', 'surprise')}
        for entry in receipt['entries']:
            counts[entry['primary_lane']] += 1
        print(json.dumps({'verdict': receipt['verdict'], 'lane_counts': counts,
                          'production_changed': False}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ConfigError, DiscoveryError) as error:
        # Config and input errors may include private data. Do not echo them.
        print(f'Discovery operation failed ({type(error).__name__}). No publication occurred.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
