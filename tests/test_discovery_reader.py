from __future__ import annotations
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from curator.render import render_site
from curator.source_snapshot import load_source_snapshot
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def test_discovery_reader_contract():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    result = subprocess.run([node, str(ROOT/'tests/discovery_reader_runner.js')], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'discovery reader contract: PASS' in result.stdout


def test_discovery_reader_browser(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    raw = json.loads((ROOT/'tests/fixtures/discovery-captured.json').read_text())
    now = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    capture = load_source_snapshot(ROOT/'tests/fixtures/discovery-captured.json', current_time=now)
    item = next(i for r in capture.results for i in r.items if i.source_id == 'techcrunch')
    site = tmp_path/'site'
    render_site({'AI':[item]}, list(capture.results), now, site, topic_ids_by_name={'AI':'ai'}, discovery_enabled=True)
    html = (site/'index.html').read_text()
    html,n = re.subn(r'<script src="auth/client\.js\?v=[0-9a-f]{16}" defer></script>', '<script src="auth-stub.js" defer></script>',html)
    assert n == 1
    html = html.replace("connect-src 'self'", "connect-src 'self' https://project-ref.supabase.co")
    (site/'index.html').write_text(html)
    (site/'auth-stub.js').write_text('''window.__signed=true;window.NewsCuratorAuth={config:()=>({url:"https://project-ref.supabase.co",key:"contract-public-key"}),hasSessionCandidate:()=>window.__signed,sessionForRequest:async()=>window.__signed?{access_token:window.__token||"controlled-test-auth-transport"}:null,clearSession:()=>{window.__signed=false},channelName:"contract-discovery"};window.setInterval=(callback)=>{window.__discoveryPoll=callback;return 1;};''')
    result = subprocess.run([node,str(ROOT/'tests/discovery_reader_browser.js'),str(site)],cwd=ROOT,capture_output=True,text=True,timeout=55)
    if result.returncode == 77:
        pytest.skip('Existing Node Playwright runtime unavailable')
    assert result.returncode == 0, result.stdout+'\n'+result.stderr
    assert 'discovery reader browser: PASS' in result.stdout
