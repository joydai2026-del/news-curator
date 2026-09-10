#!/usr/bin/env python3
"""Fetch only a validated public-source baseline from this repository's main workflow.

Missing/expired/incompatible captures mean Updates has no baseline. Private
receipts are never read or uploaded by this command. The bounded `gh` transport
uses existing GitHub authentication and never prints subprocess error bodies.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curator.config import ConfigError, load_config  # noqa: E402
from curator.discovery import load_discovery_policy, validate_discovery_policy  # noqa: E402
from curator.source_snapshot import (  # noqa: E402
    MAX_SNAPSHOT_BYTES, SourceSnapshotError, load_source_snapshot, snapshot_config_digest,
)


def fetch_baseline(root: Path, current: Path, output: Path, repository: str,
                   current_run_id: str, *, anchor_before: datetime | None = None,
                   attempts: int | None = None, timeout: int = 30, policy=None,
                   evaluation_clock: datetime | None = None,
                   run=subprocess.run) -> bool:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository')
    if not current_run_id.isdigit() or (attempts is not None and not 1 <= attempts <= 99) or not 1 <= timeout <= 120:
        raise ValueError('fetch_bounds')
    if output.exists() or output.is_symlink():
        raise ValueError('output_exists')
    cfg = load_config(root)
    digest = snapshot_config_digest(cfg)
    snapshot = load_source_snapshot(current, expected_configuration_digest=digest)
    policy = load_discovery_policy(root / 'config/discovery-policy-r2.yaml') if policy is None else validate_discovery_policy(policy)
    max_age = max(1, int(policy['windows']['updates'] * 3600))
    evaluation_clock = snapshot.generated_at if evaluation_clock is None else evaluation_clock
    if (not isinstance(evaluation_clock, datetime) or evaluation_clock.tzinfo is None
            or evaluation_clock < snapshot.generated_at):
        raise ValueError('evaluation_clock')
    evaluation_clock = evaluation_clock.astimezone(timezone.utc)
    if anchor_before is not None:
        if not isinstance(anchor_before, datetime) or anchor_before.tzinfo is None:
            raise ValueError('anchor_before')
        anchor_before = anchor_before.astimezone(timezone.utc)
    # One complete GitHub API page avoids outcome-sampling a burst. A full
    # page is ambiguous because more relevant runs might exist, so it fails
    # closed. Callers may impose a lower configurable candidate bound.
    search_limit = attempts if attempts is not None else 99
    lower = evaluation_clock - timedelta(seconds=max_age)
    upper = min(evaluation_clock, anchor_before or evaluation_clock)
    def window(value):
        return value.isoformat(timespec='seconds').replace('+00:00', 'Z')

    query = 'repos/{}/actions/workflows/curate.yml/runs?{}'.format(
        repository, urlencode({'branch': 'main', 'status': 'success', 'per_page': 100,
                               'created': f'{window(lower)}..{window(upper)}'}),
    )
    try:
        response = run(['gh', 'api', query], check=True, capture_output=True, timeout=timeout)
        if len(response.stdout) > 2_000_000:
            return False
        payload = json.loads(response.stdout)
        runs = payload['workflow_runs']
        total_count = payload['total_count']
        if not isinstance(runs, list) or type(total_count) is not int or total_count != len(runs) or total_count >= 100:
            return False
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError):
        return False
    candidates = []
    for row in runs:
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            return False
        run_id = row['id']
        if (not isinstance(row.get('head_branch'), str) or not isinstance(row.get('conclusion'), str)
                or not isinstance(row.get('repository'), dict)
                or not isinstance(row['repository'].get('full_name'), str)):
            return False
        try:
            created_at = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            if created_at.tzinfo is None:
                return False
            created_at = created_at.astimezone(timezone.utc)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        # The explicit API filters make contradictory metadata untrustworthy.
        # Refuse the whole page rather than outcome-selecting its remaining rows.
        if row['head_branch'] != 'main' or row['conclusion'] != 'success' or row['repository']['full_name'] != repository:
            return False
        # The current or a future run is an intentional dependency exclusion.
        if run_id >= int(current_run_id):
            continue
        # Creation time bounds a complete transport window. The validated source
        # snapshot clock remains the authority for final acceptance and order.
        # Whole-second API rounding can place a valid edge row just outside it.
        if not lower <= created_at <= upper:
            continue
        candidates.append(run_id)
    if len(candidates) > search_limit:
        return False

    selected = None
    for run_id in candidates:
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
                    current_time=evaluation_clock, max_age_seconds=max_age)
                if previous.generated_at >= snapshot.generated_at:
                    continue
                if anchor_before is not None and previous.generated_at > anchor_before:
                    continue
                raw = candidate.read_bytes()
            except (OSError, subprocess.SubprocessError, SourceSnapshotError):
                continue
            # Snapshot timestamps, not API order or workflow metadata, determine
            # the anchor. Keep only the current best raw artifact in memory.
            candidate_row = (previous.generated_at, previous.content_digest, run_id, raw)
            if selected is None or (
                candidate_row[:3] < selected[:3] if anchor_before is None else candidate_row[:3] > selected[:3]
            ):
                selected = candidate_row
    if selected is None:
        return False
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(selected[3])
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', ''))
    parser.add_argument('--current-run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
    parser.add_argument('--anchor-before', help='UTC ISO timestamp of the last stored edition, if one exists.')
    parser.add_argument('--attempts', type=int, help='Maximum prior successful runs to inspect.')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        anchor = None if args.anchor_before is None else datetime.fromisoformat(args.anchor_before.replace('Z', '+00:00'))
        found = fetch_baseline(args.root, args.source_snapshot, args.output, args.repository,
                               args.current_run_id, anchor_before=anchor, attempts=args.attempts, timeout=args.timeout)
    except (OSError, ValueError, ConfigError):
        print('Public source baseline could not be validated. Updates remains unavailable.', file=sys.stderr)
        return 2
    print('Public source baseline validated.' if found else 'No compatible public baseline. Updates remains unavailable.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
