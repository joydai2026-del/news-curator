#!/usr/bin/env python3
"""Build and settle a passing private discovery edition for the configured owner."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from curator.config import load_config  # noqa: E402
from curator.discovery import load_discovery_policy  # noqa: E402
from curator.personalization.materializer import SecretPreferenceConfig  # noqa: E402
from curator.private_discovery import materialize_private_discovery  # noqa: E402
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest  # noqa: E402
from scripts.fetch_discovery_baseline import fetch_baseline  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--previous-source-snapshot', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--code-revision', required=True)
    parser.add_argument('--language', choices=('en', 'zh'), default='en')
    parser.add_argument('--baseline-attempts', type=int)
    parser.add_argument('--baseline-timeout', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        revision = subprocess.run(['git', '-C', str(args.root), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True, timeout=15).stdout.strip()
        if revision != args.code_revision:
            raise ValueError('Code revision does not match this checkout.')
        cfg = load_config(args.root)
        expected = snapshot_config_digest(cfg)
        snapshot = load_source_snapshot(args.source_snapshot, expected_configuration_digest=expected,
                                        current_time=now, max_age_seconds=cfg.source_snapshot_max_age_seconds)
        policy = load_discovery_policy(args.policy or args.root / 'config' / 'discovery-policy-r2.yaml')
        previous = None if args.previous_source_snapshot is None else load_source_snapshot(
            args.previous_source_snapshot, expected_configuration_digest=expected, current_time=now,
            max_age_seconds=int(max(policy['windows'].values()) * 3600))
        baseline_loader = None
        if args.previous_source_snapshot is None:
            def baseline_loader(anchor_before):
                with tempfile.TemporaryDirectory(prefix='discovery-materializer-baseline-') as temporary:
                    output = Path(temporary) / 'source-snapshot.json'
                    found = fetch_baseline(args.root, args.source_snapshot, output,
                        os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_RUN_ID', ''),
                        anchor_before=anchor_before, attempts=args.baseline_attempts,
                        timeout=args.baseline_timeout, policy=policy, evaluation_clock=now)
                    if not found:
                        return None
                    return load_source_snapshot(output, expected_configuration_digest=expected,
                        current_time=now, max_age_seconds=int(max(policy['windows'].values()) * 3600))
        secret = SecretPreferenceConfig(os.environ.get('NEWS_CURATOR_SUPABASE_URL', ''),
            os.environ.get('NEWS_CURATOR_SUPABASE_SECRET_KEY', ''), os.environ.get('NEWS_CURATOR_OWNER_USER_ID', ''))
        result = materialize_private_discovery(cfg, snapshot, policy, secret, previous_snapshot=previous,
                    code_revision=args.code_revision, now=now, language=args.language,
                    baseline_loader=baseline_loader)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in ('stored', 'already_stored') else 3
    except Exception:
        print('Private discovery materialization failed safely; the prior edition is retained.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
