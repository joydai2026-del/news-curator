from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from curator.config import load_config
from curator.discovery import build_discovery, load_discovery_policy
from curator.private_discovery import _source_cards
from curator.render import render_html
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest


ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / 'tests' / 'fixtures' / 'discovery-passing-current.json'
PREVIOUS = ROOT / 'tests' / 'fixtures' / 'discovery-passing-previous.json'
SCRIPT = ROOT / 'tests' / 'discovery_passing_reader_browser.js'


def _snapshot(path, digest):
    raw = json.loads(path.read_text(encoding='utf-8'))
    clock = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    return load_source_snapshot(path, expected_configuration_digest=digest, current_time=clock)


def _envelope(tmp_path):
    cfg = load_config(ROOT)
    digest = snapshot_config_digest(cfg)
    current = _snapshot(CURRENT, digest)
    previous = _snapshot(PREVIOUS, digest)
    policy = load_discovery_policy(ROOT / 'config' / 'discovery-policy-r2.yaml')
    receipt = build_discovery(
        cfg, current, policy, previous_snapshot=previous, now=current.generated_at,
        history=[], first_edition=True,
    )
    assert receipt['verdict'] == 'PASS'
    assert all(band['active'] and band['verdict'] == 'PASS' for band in receipt['bands'])
    cards = _source_cards(receipt)
    entries = []
    for entry in receipt['entries']:
        card = {
            **cards[entry['story_id']],
            'story_id': entry['story_id'],
            'position': 0,
            'publication_seq': 0,
            'score_components': entry['components'],
            'ordering_mode': 'weighted_total',
            'ordering_key': {},
            'page_order_mode': 'discovery',
            'next_cursor': None,
            'topic_ranks': {},
            'ranking_explanation': entry['plain_reason'],
            'read_at': None,
            'saved_at': None,
            'state_revision': 0,
            'interests': [],
        }
        entries.append({
            'position': entry['position'],
            'primary_lane': entry['primary_lane'],
            'reason': entry['plain_reason'],
            'secondary_reasons': [
                {'lane': lane, 'reason': entry['lane_reasons'][lane]}
                for lane in entry['secondary_lanes']
            ],
            'card': card,
        })
    envelope = {
        'schema_version': 1,
        'status': 'ready',
        'reason_code': '',
        'edition': {
            'edition_id': 'm2-local-passing-reader',
            'generated_at': receipt['generated_at'],
            'code_revision': 'a' * 40,
            'policy_revision': receipt['policy']['revision'],
            'policy_digest': receipt['bindings']['policy_digest'],
            'snapshot_digest': receipt['bindings']['snapshot_digest'],
            'profile_revision': 0,
            'receipt_digest': receipt['receipt_digest'],
            'stale': False,
            'disclosures': receipt['disclosures'],
            'shortfalls': receipt['shortfalls'],
            'entries': entries,
        },
    }
    path = tmp_path / 'passing-reader-envelope.json'
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
    return current, previous, path


def test_passing_receipt_reaches_reader_lanes_on_desktop_and_mobile(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    current, previous, envelope = _envelope(tmp_path)
    site = tmp_path / 'site'
    public_item = next(
        item for result in current.results for item in result.items if item.description
    )
    site.mkdir()
    (site / 'index.html').write_text(render_html(
            {'AI': [public_item]}, list(current.results), current.generated_at,
            built_at=previous.generated_at, timezone_name='UTC',
        topic_ids_by_name={'AI': 'ai'}, discovery_enabled=True,
    ), encoding='utf-8')
    (site / 'reader.js').write_bytes((ROOT / 'static' / 'reader.js').read_bytes())
    (site / 'auth').mkdir()
    (site / 'auth' / 'client.js').write_bytes((ROOT / 'static' / 'auth' / 'client.js').read_bytes())
    html = (site / 'index.html').read_text(encoding='utf-8')
    html, count = re.subn(
        r'<script src="auth/client\.js(?:\?v=[0-9a-f]{16})?" defer></script>',
        '<script src="auth-stub.js" defer></script>', html,
    )
    assert count == 1
    html = html.replace("connect-src 'self'", "connect-src 'self' https://project-ref.supabase.co")
    (site / 'index.html').write_text(html, encoding='utf-8')
    (site / 'auth-stub.js').write_text(
        'window.__signed=true;window.NewsCuratorAuth={'
        'config:()=>({url:"https://project-ref.supabase.co",key:"controlled-m2-key"}),'
        'hasSessionCandidate:()=>window.__signed,'
        'sessionForRequest:async()=>window.__signed?{access_token:window.__account==="other"?"other-account-auth":"controlled-m2-reader-auth"}:null,'
        'clearSession:()=>{window.__signed=false},channelName:"controlled-m2-reader"};'
        'window.setInterval=(callback)=>{window.__discoveryPoll=callback;return 1;};',
        encoding='utf-8',
    )
    artifact_dir = Path(os.environ.get('M2_READER_ARTIFACT_DIR', str(tmp_path / 'reader-artifacts')))
    public_built = 'Built ' + previous.generated_at.strftime('%b %d, %Y at %I:%M %p UTC').replace(' 0', ' ')
    private_built = 'Built ' + current.generated_at.strftime('%b %d, %Y at %I:%M %p UTC').replace(' 0', ' ')
    private_iso = json.loads(envelope.read_text(encoding='utf-8'))['edition']['generated_at']
    result = subprocess.run(
        [node, str(SCRIPT), str(site), str(envelope), str(artifact_dir), public_built, '1 stories',
         private_built, previous.generated_at.isoformat(), private_iso],
        cwd=ROOT, capture_output=True, text=True, timeout=50,
    )
    if result.returncode == 77:
        pytest.skip('Existing Node Playwright runtime unavailable')
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    assert 'discovery passing reader: PASS' in result.stdout
    report = json.loads((artifact_dir / 'reader-regression-report.json').read_text(encoding='utf-8'))
    assert report['total'] == 16
    assert set(report['lanes']) == {'updates', 'hot', 'interested', 'surprise'}
