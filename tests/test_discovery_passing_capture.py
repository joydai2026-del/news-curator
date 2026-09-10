"""Replay the compact real-source M2 capture with every active band enabled."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from curator.config import load_config
from curator.discovery import build_discovery, load_discovery_policy, replay_discovery
from curator.identity import story_id_for_item
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest


ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / 'tests' / 'fixtures' / 'discovery-passing-current.json'
PREVIOUS = ROOT / 'tests' / 'fixtures' / 'discovery-passing-previous.json'


def _snapshot(path, digest):
    raw = json.loads(path.read_text(encoding='utf-8'))
    clock = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    return load_source_snapshot(path, expected_configuration_digest=digest, current_time=clock)


def test_compact_real_capture_passes_all_active_bands_and_replays():
    cfg = load_config(ROOT)
    digest = snapshot_config_digest(cfg)
    policy = load_discovery_policy(ROOT / 'config' / 'discovery-policy-r2.yaml')
    assert all(band['active'] for band in policy['bands'].values())

    current = _snapshot(CURRENT, digest)
    previous = _snapshot(PREVIOUS, digest)
    receipt = build_discovery(
        cfg, current, policy, previous_snapshot=previous, now=current.generated_at,
        history=[], first_edition=True,
    )

    assert receipt['history_baseline'] == 'first_local_edition'
    assert receipt['history_available'] is True
    assert receipt['verdict'] == 'PASS'
    assert all(band['verdict'] == 'PASS' for band in receipt['bands'])
    assert replay_discovery(receipt)

    lanes = {entry['primary_lane'] for entry in receipt['entries']}
    assert lanes == {'updates', 'hot', 'interested', 'surprise'}
    story_ids = [entry['story_id'] for entry in receipt['entries']]
    assert len(story_ids) == len(set(story_ids))
    for entry in receipt['entries']:
        assert entry['plain_reason'] == entry['lane_reasons'][entry['primary_lane']]
        assert entry['plain_reason']
        assert entry['evidence']['observations']
        assert all(row in receipt['observations'] for row in entry['evidence']['observations'])

    source_story_ids = {
        story_id_for_item(item)
        for result in current.results
        for item in result.items
    }
    assert source_story_ids
    assert all(entry['story_id'] in source_story_ids for entry in receipt['entries'])
