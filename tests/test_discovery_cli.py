"""Exercise the actual M2 replay command using captured public-source records."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'discovery_cli.py'
CAPTURE = ROOT / 'tests' / 'fixtures' / 'discovery-captured.json'


def call(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=30)


def test_cli_real_snapshot_build_and_replay(tmp_path):
    output = tmp_path / 'receipt.json'
    built = call('build', '--snapshot', CAPTURE, '--output', output)
    assert built.returncode == 0, built.stderr
    summary = json.loads(built.stdout)
    assert summary['production_changed'] is False
    assert set(summary['lane_counts']) == {'updates', 'hot', 'interested', 'surprise'}
    assert os.stat(output).st_mode & 0o777 == 0o600
    verified = call('verify', '--receipt', output)
    assert verified.returncode == 0, verified.stderr
    assert 'internal consistency verified' in verified.stdout
    receipt = json.loads(output.read_text())
    receipt['entries'] = []
    output.write_text(json.dumps(receipt))
    assert call('verify', '--receipt', output).returncode == 2


def test_cli_default_history_is_unknown(tmp_path):
    output = tmp_path / 'receipt.json'
    result = call('build', '--snapshot', CAPTURE, '--output', output)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text())
    assert receipt['history_available'] is False
    assert receipt['history_baseline'] == 'unavailable'
    repetition = next(band for band in receipt['bands'] if band['band'] == 'repetition')
    assert repetition['achieved'] is None
    assert repetition['verdict'] == 'UNKNOWN'


def test_cli_first_local_edition_declares_empty_history(tmp_path):
    output = tmp_path / 'first-edition.json'
    result = call('build', '--snapshot', CAPTURE, '--first-local-edition', '--output', output)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text())
    assert receipt['history_available'] is True
    assert receipt['history_baseline'] == 'first_local_edition'
    assert receipt['history_input'] == []


def test_cli_supplied_history_is_validated_and_bound(tmp_path):
    history = tmp_path / 'history.json'
    baseline = tmp_path / 'baseline.json'
    baseline_result = call('build', '--snapshot', CAPTURE, '--output', baseline)
    assert baseline_result.returncode == 0, baseline_result.stderr
    baseline_receipt = json.loads(baseline.read_text())
    entry = baseline_receipt['entries'][0]
    history.write_text(json.dumps([{
        'story_id': entry['story_id'],
        'source_id': entry['independent_source'],
        'shown_at': baseline_receipt['generated_at'],
    }]))
    output = tmp_path / 'supplied-history.json'
    result = call('build', '--snapshot', CAPTURE, '--history', history, '--output', output)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text())
    assert receipt['history_available'] is True
    assert receipt['history_baseline'] == 'supplied_history'
    assert receipt['history_input'] == json.loads(history.read_text())


def test_cli_history_controls_are_mutually_exclusive(tmp_path):
    result = call('build', '--snapshot', CAPTURE, '--first-local-edition',
                  '--history', tmp_path / 'history.json', '--output', tmp_path / 'out.json')
    assert result.returncode == 2
    assert not (tmp_path / 'out.json').exists()


def test_cli_rejects_oversize_or_malformed_history(tmp_path):
    oversize = tmp_path / 'oversize.json'
    oversize.write_text('x' * (16 * 1024 * 1024 + 1))
    malformed = tmp_path / 'malformed.json'
    malformed.write_text('{"history": []}')
    too_many_rows = tmp_path / 'too-many-rows.json'
    too_many_rows.write_text(json.dumps([{}] * 10_001))
    for source in (oversize, malformed, too_many_rows):
        result = call('build', '--snapshot', CAPTURE, '--history', source,
                      '--output', tmp_path / (source.stem + '-out.json'))
        assert result.returncode == 2
        assert 'history' not in result.stdout + result.stderr


def test_cli_rejects_malformed_history_row_without_output(tmp_path):
    history = tmp_path / 'bad-row.json'
    history.write_text(json.dumps([{
        'story_id': 'story:bad-row',
        'source_id': 'local',
        'shown_at': 'not-an-iso-time',
    }]))
    output = tmp_path / 'out.json'
    result = call('build', '--snapshot', CAPTURE, '--history', history, '--output', output)
    assert result.returncode == 2
    assert not output.exists()


def test_cli_does_not_overwrite_existing_output(tmp_path):
    output = tmp_path / 'receipt.json'
    output.write_text('preserve this existing artifact')
    result = call('build', '--snapshot', CAPTURE, '--output', output)
    assert result.returncode == 2
    assert output.read_text() == 'preserve this existing artifact'


def test_cli_malformed_input_does_not_echo_payload(tmp_path):
    invalid = tmp_path / 'invalid.json'
    marker = 'private-input-marker-should-never-print'
    invalid.write_text(json.dumps({'generated_at': marker}))
    result = call('build', '--snapshot', invalid, '--output', tmp_path / 'out.json')
    assert result.returncode == 2
    assert marker not in result.stdout + result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path / 'out.json').exists()
