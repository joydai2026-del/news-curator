"""Baseline transport contracts. Responses are controlled tests, source facts captured."""
from datetime import datetime, timedelta
from copy import deepcopy
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
            return SimpleNamespace(stdout=json.dumps({'total_count': 1, 'workflow_runs': [{
                'id': run_id, 'head_branch': branch, 'conclusion': conclusion,
                'created_at': json.loads(previous.read_bytes())['generated_at'],
                'repository': {'full_name': repository},
            }]}).encode())
        directory = Path(args[args.index('--dir')+1])
        (directory/'source-snapshot.json').write_bytes(previous.read_bytes())
        return SimpleNamespace(stdout=b'')
    return call, calls


def multi_transport(captures, *, repository='owner/repo'):
    """Return a run transport whose API order is deliberately caller-controlled."""
    calls = []

    def call(args, **kwargs):
        calls.append(args)
        if args[1] == 'api':
            runs = [{'id': run_id, 'head_branch': 'main', 'conclusion': 'success',
                     'created_at': json.loads(captures['bytes'][run_id])['generated_at'],
                     'repository': {'full_name': repository}} for run_id in captures['order']]
            return SimpleNamespace(stdout=json.dumps({'total_count': len(runs), 'workflow_runs': runs}).encode())
        run_id = int(args[args.index('download') + 1])
        directory = Path(args[args.index('--dir') + 1])
        (directory / 'source-snapshot.json').write_bytes(captures['bytes'][run_id])
        return SimpleNamespace(stdout=b'')

    return call, calls


def snapshot_bytes(directory, current, when, *, configuration_digest=None):
    path = directory / f"capture-{when.timestamp()}.json"
    write_source_snapshot(
        current.results,
        path,
        generated_at=when,
        configuration_digest=current.configuration_digest if configuration_digest is None else configuration_digest,
    )
    return path.read_bytes()


def current_capture():
    raw = json.loads(CAPTURE.read_text())
    now = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    return load_source_snapshot(CAPTURE, current_time=now), now


def freeze_loader(monkeypatch, now):
    real_loader = load_source_snapshot

    def at_capture(path, **kwargs):
        kwargs.setdefault('current_time', now)
        return real_loader(path, **kwargs)

    monkeypatch.setattr(baseline, 'load_source_snapshot', at_capture)


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


def test_first_edition_selects_oldest_valid_capture_independent_of_api_order(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    oldest = snapshot_bytes(tmp_path, current, now - timedelta(hours=3))
    newest = snapshot_bytes(tmp_path, current, now - timedelta(hours=1))
    fixtures = {'order': [9, 7], 'bytes': {9: newest, 7: oldest}}
    call, _ = multi_transport(fixtures)
    output = tmp_path / 'baseline.json'

    assert baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', attempts=2, run=call)
    assert output.read_bytes() == oldest


def test_existing_edition_selects_newest_capture_at_or_before_anchor(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    oldest = snapshot_bytes(tmp_path, current, now - timedelta(hours=4))
    anchor_match = snapshot_bytes(tmp_path, current, now - timedelta(hours=2))
    after_anchor = snapshot_bytes(tmp_path, current, now - timedelta(hours=1))
    # Deliberately unordered to prove the snapshot clock, not API order, decides.
    fixtures = {'order': [9, 6, 8], 'bytes': {9: after_anchor, 6: oldest, 8: anchor_match}}
    call, _ = multi_transport(fixtures)
    output = tmp_path / 'baseline.json'

    assert baseline.fetch_baseline(
        ROOT, CAPTURE, output, 'owner/repo', '10', attempts=3,
        anchor_before=now - timedelta(hours=2), run=call,
    )
    assert output.read_bytes() == anchor_match


def test_invalid_clock_and_configuration_captures_are_excluded(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    valid = snapshot_bytes(tmp_path, current, now - timedelta(hours=2))
    equal_clock = snapshot_bytes(tmp_path, current, now)
    wrong_config = snapshot_bytes(tmp_path, current, now - timedelta(hours=3), configuration_digest='0' * 64)
    fixtures = {'order': [9, 8, 7], 'bytes': {9: equal_clock, 8: wrong_config, 7: valid}}
    call, _ = multi_transport(fixtures)
    output = tmp_path / 'baseline.json'

    assert baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '10', attempts=3, run=call)
    assert output.read_bytes() == valid


def test_supplied_updates_window_rejects_older_capture(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    older = snapshot_bytes(tmp_path, current, now - timedelta(hours=2))
    call, _ = multi_transport({'order': [9], 'bytes': {9: older}})
    policy = deepcopy(baseline.load_discovery_policy(ROOT / 'config/discovery-policy-r2.yaml'))
    policy['windows']['updates'] = 1

    assert not baseline.fetch_baseline(
        ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '10',
        attempts=1, policy=policy, run=call,
    )


def test_complete_window_includes_candidate_beyond_old_hourly_cutoff(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    order = list(range(26, 0, -1))
    captures = {
        'order': order,
        'bytes': {run_id: snapshot_bytes(tmp_path, current, now - timedelta(minutes=27-run_id))
                  for run_id in order},
    }
    call, _ = multi_transport(captures)
    output = tmp_path / 'baseline.json'

    assert baseline.fetch_baseline(ROOT, CAPTURE, output, 'owner/repo', '27', run=call)
    assert output.read_bytes() == captures['bytes'][1]


def test_saturated_workflow_page_fails_before_download(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    raw = snapshot_bytes(tmp_path, current, now - timedelta(minutes=1))
    captures = {'order': list(range(100, 0, -1)), 'bytes': {run_id: raw for run_id in range(1, 101)}}
    call, calls = multi_transport(captures)

    assert not baseline.fetch_baseline(ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '101', run=call)
    assert len(calls) == 1


def test_query_is_limited_to_the_declared_created_window(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    raw = snapshot_bytes(tmp_path, current, now - timedelta(minutes=1))
    call, calls = multi_transport({'order': [9], 'bytes': {9: raw}})

    assert baseline.fetch_baseline(ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '10', run=call)
    query = calls[0][2]
    assert 'per_page=100' in query and 'created=' in query
    assert '%3A' in query and '..' in query


def test_baseline_age_uses_the_materializer_evaluation_clock(tmp_path, monkeypatch):
    current, now = current_capture()
    evaluation = now + timedelta(hours=1)
    freeze_loader(monkeypatch, now)
    raw = snapshot_bytes(tmp_path, current, now - timedelta(hours=23, minutes=30))
    calls = []

    def call(args, **kwargs):
        calls.append(args)
        if args[1] == 'api':
            return SimpleNamespace(stdout=json.dumps({'total_count': 1, 'workflow_runs': [{
                'id': 9, 'head_branch': 'main', 'conclusion': 'success',
                'created_at': (now - timedelta(hours=22)).isoformat().replace('+00:00', 'Z'),
                'repository': {'full_name': 'owner/repo'},
            }]}).encode())
        directory = Path(args[args.index('--dir') + 1])
        (directory / 'source-snapshot.json').write_bytes(raw)
        return SimpleNamespace(stdout=b'')

    assert not baseline.fetch_baseline(
        ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '10',
        evaluation_clock=evaluation, run=call,
    )
    assert len(calls) == 2


def test_partial_workflow_response_fails_before_download(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    calls = []

    def call(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({'total_count': 2, 'workflow_runs': []}).encode())

    assert not baseline.fetch_baseline(ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '10', run=call)
    assert len(calls) == 1


def test_malformed_row_refuses_complete_page_before_valid_candidate_download(tmp_path, monkeypatch):
    current, now = current_capture()
    freeze_loader(monkeypatch, now)
    calls = []

    def call(args, **kwargs):
        calls.append(args)
        if args[1] == 'api':
            return SimpleNamespace(stdout=json.dumps({'total_count': 2, 'workflow_runs': [
                {'id': 9, 'head_branch': 'main', 'conclusion': 'success',
                 'created_at': (now - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z'),
                 'repository': {'full_name': 'owner/repo'}},
                {'id': 'invalid'},
            ]}).encode())
        raise AssertionError('malformed metadata must prevent every artifact download')

    assert not baseline.fetch_baseline(ROOT, CAPTURE, tmp_path / 'baseline.json', 'owner/repo', '10', run=call)
    assert len(calls) == 1
