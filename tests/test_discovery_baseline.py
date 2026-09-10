"""Baseline transport contracts. Responses are controlled tests, source facts captured."""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from curator.source_snapshot import load_source_snapshot, write_source_snapshot
from scripts import fetch_discovery_baseline as baseline

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / 'tests/fixtures/discovery-captured.json'


@pytest.fixture
def captures(tmp_path, monkeypatch):
    raw = json.loads(CAPTURE.read_text())
    from datetime import datetime
    now = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    real_loader = load_source_snapshot
    current = real_loader(CAPTURE, current_time=now)
    previous = tmp_path / 'previous.json'
    write_source_snapshot(current.results, previous, generated_at=now-timedelta(hours=1),
                          configuration_digest=current.configuration_digest)
    def at_capture(path, **kwargs):
        kwargs.setdefault('current_time', now)
        return real_loader(path, **kwargs)
    monkeypatch.setattr(baseline, 'load_source_snapshot', at_capture)
    return previous


def transport(previous, *, conclusion='success', branch='main', repository='owner/repo', run_id=9):
    calls = []
    def call(args, **kwargs):
        calls.append(args)
        if args[1] == 'api':
            return SimpleNamespace(stdout=json.dumps({'workflow_runs': [{
                'id': run_id, 'head_branch': branch, 'conclusion': conclusion,
                'repository': {'full_name': repository},
            }]}).encode())
        directory = Path(args[args.index('--dir')+1])
        (directory/'source-snapshot.json').write_bytes(previous.read_bytes())
        return SimpleNamespace(stdout=b'')
    return call, calls


def test_prior_successful_capture_validated_and_created_private(captures, tmp_path):
    call, calls = transport(captures)
    output = tmp_path/'output.json'
    assert baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', run=call)
    assert output.read_bytes() == captures.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert '--name' in calls[-1] and 'source-snapshot' in calls[-1]
    with pytest.raises(ValueError):
        baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', run=call)


@pytest.mark.parametrize('kwargs', [
    {'conclusion': 'failure'}, {'branch': 'feature'}, {'repository': 'foreign/repo'}, {'run_id': 10},
])
def test_other_runs_never_used(captures, tmp_path, kwargs):
    call, calls = transport(captures, **kwargs)
    output = tmp_path/'output.json'
    assert not baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', run=call)
    assert len(calls) == 1 and not output.exists()


def test_equal_clock_not_a_previous_capture(captures, tmp_path):
    call, _ = transport(CAPTURE)
    output = tmp_path/'output.json'
    assert not baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', run=call)
    assert not output.exists()


def test_transport_failure_stays_unavailable(captures, tmp_path):
    def unavailable(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr='Controlled secret-shaped error must stay private')
    assert not baseline.fetch_baseline(ROOT, CAPTURE, tmp_path/'output.json', 'owner/repo', '10', run=unavailable)
