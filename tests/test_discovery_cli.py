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
