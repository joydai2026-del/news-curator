#!/usr/bin/env python3
"""Fetch only a validated public-source baseline from this repository's main workflow.

Missing/expired/incompatible captures mean Updates has no baseline. Private
receipts are never read or uploaded by this command. The bounded `gh` transport
uses existing GitHub authentication and never prints subprocess error bodies.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curator.config import ConfigError, load_config  # noqa: E402
from curator.discovery import load_discovery_policy  # noqa: E402
from curator.source_snapshot import (  # noqa: E402
    MAX_SNAPSHOT_BYTES, SourceSnapshotError, load_source_snapshot, snapshot_config_digest,
)


def fetch_baseline(root: Path, current: Path, output: Path, repository: str,
                   current_run_id: str, *, attempts: int = 3, timeout: int = 30,
                   run=subprocess.run) -> bool:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository')
    if not current_run_id.isdigit() or not 1 <= attempts <= 20 or not 1 <= timeout <= 120:
        raise ValueError('fetch_bounds')
    if output.exists() or output.is_symlink():
        raise ValueError('output_exists')
    cfg = load_config(root)
    digest = snapshot_config_digest(cfg)
    snapshot = load_source_snapshot(current, expected_configuration_digest=digest)
    policy = load_discovery_policy(root / 'config/discovery-policy-r2.yaml')
    max_age = max(1, int(policy['windows']['updates'] * 3600))
    query = f'repos/{repository}/actions/workflows/curate.yml/runs?branch=main&status=success&per_page={attempts + 1}'
    try:
        response = run(['gh', 'api', query], check=True, capture_output=True, timeout=timeout)
        if len(response.stdout) > 2_000_000:
            return False
        runs = json.loads(response.stdout)['workflow_runs']
        if not isinstance(runs, list):
            return False
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError):
        return False
    tried = 0
    for row in runs:
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            continue
        run_id = row['id']
        if run_id >= int(current_run_id) or row.get('head_branch') != 'main' or row.get('conclusion') != 'success':
            continue
        if row.get('repository', {}).get('full_name') != repository:
            continue
        if tried >= attempts:
            break
        tried += 1
        with tempfile.TemporaryDirectory(prefix='discovery-public-baseline-') as temporary:
            directory = Path(temporary)
            try:
                run(['gh', 'run', 'download', str(run_id), '--repo', repository,
                     '--name', 'source-snapshot', '--dir', str(directory)],
                    check=True, capture_output=True, timeout=timeout)
                candidate = directory / 'source-snapshot.json'
                if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_SNAPSHOT_BYTES:
                    continue
                previous = load_source_snapshot(candidate, expected_configuration_digest=digest,
                    current_time=snapshot.generated_at, max_age_seconds=max_age)
                if previous.generated_at >= snapshot.generated_at:
                    continue
                raw = candidate.read_bytes()
            except (OSError, subprocess.SubprocessError, SourceSnapshotError):
                continue
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--current-run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        found = fetch_baseline(args.root, args.source_snapshot, args.output, args.repository,
                               args.current_run_id, attempts=args.attempts, timeout=args.timeout)
    except (OSError, ValueError, ConfigError):
        print('Public source baseline could not be validated. Updates remains unavailable.', file=sys.stderr)
        return 2
    print('Public source baseline validated.' if found else 'No compatible public baseline. Updates remains unavailable.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
