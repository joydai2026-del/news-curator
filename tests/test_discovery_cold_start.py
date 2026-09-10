"""Cold-start subject affinity over captured public source evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.config import Category, Config
from curator.discovery import (DiscoveryError, build_discovery, load_discovery_policy,
                               validate_discovery_policy)
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest, write_source_snapshot


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 10, 1, 17, 22, 44155, tzinfo=timezone.utc)
AUTONOMOUS_CARS = 'https://spectrum.ieee.org/are-self-driving-cars-safe'


def _capture(tmp_path, cfg):
    original = load_source_snapshot(ROOT / 'tests/fixtures/discovery-captured.json', current_time=NOW)
    rebound = tmp_path / 'capture-trending-config.json'
    write_source_snapshot(original.results, rebound, generated_at=original.generated_at,
                          configuration_digest=snapshot_config_digest(cfg))
    return load_source_snapshot(rebound, current_time=NOW)


def test_trending_utility_topic_does_not_become_cold_start_interest(tmp_path):
    cfg = Config(categories=[Category('trending', [])], rss=[], settings={}, ranking={},
                 dedup={}, hackernews={}, reddit={})
    policy = load_discovery_policy(ROOT / 'config/discovery-policy-r2.yaml')
    receipt = build_discovery(cfg, _capture(tmp_path, cfg), policy, now=NOW,
                              history=[], first_edition=True)

    story = next(row for row in receipt['candidates'] if row['url'] == AUTONOMOUS_CARS)
    assert story['topic_ids'] == ['trending'], 'the utility topic remains available as a display chip'
    assert story['primary_lane'] == 'surprise'
    assert 'interested' not in story['lane_scores']
    assert len(story['evidence']['importance']) == policy['gates']['surprise_min_sources']
    assert story['history_novelty_count'] == 0
    assert 'shared configured subject topics' in story['plain_reason']


def test_cold_start_excluded_topics_may_be_empty():
    policy = load_discovery_policy(ROOT / 'config/discovery-policy-r2.yaml')
    policy['gates']['cold_start_excluded_topic_ids'] = []
    assert validate_discovery_policy(policy)['gates']['cold_start_excluded_topic_ids'] == []


@pytest.mark.parametrize('excluded', [[''], [' trending'], ['trending', 'trending'], 'trending'])
def test_cold_start_excluded_topics_rejects_ambiguous_policy(excluded):
    policy = load_discovery_policy(ROOT / 'config/discovery-policy-r2.yaml')
    policy['gates']['cold_start_excluded_topic_ids'] = excluded
    with pytest.raises(DiscoveryError, match='discovery_cold_start_excluded_topics'):
        validate_discovery_policy(policy)
