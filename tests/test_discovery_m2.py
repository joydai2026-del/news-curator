"""Discovery safety contracts using captured public records, never demo fiction.

The fixture is a subset of a real public-source capture at 2026-09-10T01:17:22Z.
Mutations below deliberately remove/corrupt evidence for negative contract tests.
Controlled prior observations change text/times only to exercise update validation;
they are not historical reporting and must never become an edition/demo input.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.config import Category, Config
from curator.discovery import (DiscoveryError, _bands, _select,
                               _select_with_final_band_backfill, build_discovery,
                               load_discovery_policy, replay_discovery,
                               validate_discovery_policy)
from curator.models import TierResult
from curator.source_snapshot import (SourceSnapshotError, load_source_snapshot,
                                     snapshot_config_digest, write_source_snapshot)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 10, 1, 17, 22, 44155, tzinfo=timezone.utc)
CNN = 'https://cnn.com/2026/09/09/tech/ai-anthropic-safety'
WIRED = 'https://wired.com/story/i-used-ai-to-hack-my-home-network'
STEEL = 'https://technologyreview.com/2026/09/08/1142094/laureen-meroueh-makes-cheaper-cleaner-steel'
TC = 'https://techcrunch.com/2026/09/09/openai-adds-a-prominent-ai-doomer-to-its-board-of-directors'
ARS = 'https://arstechnica.com/ai/2026/09/anthropic-researcher-quits-with-a-warning-self-improving-ai-could-kill-us-all'


@pytest.fixture
def capture(cfg, tmp_path):
    path = Path(__file__).parent / 'fixtures' / 'discovery-captured.json'
    if not path.exists():
        path = Path(__file__).parent / 'discovery-captured.json'
    original = load_source_snapshot(path, current_time=NOW)
    # Controlled contract configuration, retaining every public captured Item.
    rebound = tmp_path / 'capture-controlled-config.json'
    write_source_snapshot(original.results, rebound, generated_at=original.generated_at,
                          configuration_digest=snapshot_config_digest(cfg))
    return load_source_snapshot(rebound, current_time=NOW)


@pytest.fixture
def cfg():
    return Config(categories=[Category('AI', ['AI', 'artificial intelligence', 'Anthropic']),
                              Category('Politics', ['Fetterman', 'Democratic'])],
                  rss=[], settings={}, ranking={}, dedup={}, hackernews={}, reddit={})


@pytest.fixture
def policy():
    path = ROOT / 'config' / 'discovery-policy-r2.yaml'
    if not path.exists():
        path = Path(__file__).parent / 'discovery-policy-r2.yaml'
    return load_discovery_policy(path)


def records(capture, url):
    return deepcopy([i for r in capture.results for i in r.items if i.canonical_url == url])


def snapshot(tmp_path, capture, items, *, at=NOW):
    """Re-sign controlled input changes through the real snapshot writer."""
    path = tmp_path / ('snapshot-' + str(int(at.timestamp() * 1_000_000)) + '.json')
    write_source_snapshot([TierResult('sources', items)], path, generated_at=at,
                          configuration_digest=capture.configuration_digest)
    return load_source_snapshot(path, current_time=NOW)


def run(cfg, capture, policy, **kwargs):
    return build_discovery(cfg, capture, policy, now=NOW, history=[], **kwargs)


def member(receipt, lane):
    return [c for c in receipt['candidates'] if lane in c['lane_scores']]


def test_native_rank_does_not_prove_hot(tmp_path, capture, cfg, policy):
    items = records(capture, CNN)
    publisher = next(i for i in items if not i.is_aggregator)
    # Corruption deliberately exaggerates source-local metadata, not live reach.
    publisher.native_rank = 1
    publisher.score = 1_000_000
    receipt = run(cfg, snapshot(tmp_path, capture, [publisher]), policy)
    assert not member(receipt, 'hot')


def test_duplicate_publisher_routes_are_one_source(tmp_path, capture, cfg, policy):
    items = records(capture, TC)
    assert {i.source_id for i in items} == {'techcrunch', 'tc-ai'}
    assert not member(run(cfg, snapshot(tmp_path, capture, items), policy), 'hot')


def test_echo_ineligible_aggregator_cannot_create_hot(tmp_path, capture, cfg, policy):
    items = records(capture, ARS)
    assert any(not i.echo_eligible for i in items)
    assert not member(run(cfg, snapshot(tmp_path, capture, items), policy), 'hot')


def test_newsletter_marked_records_cannot_create_hot(tmp_path, capture, cfg, policy):
    items = records(capture, CNN)
    for i in items:
        i.is_newsletter = True  # Controlled type corruption, not a captured email.
    # The source-only snapshot boundary explicitly rejects newsletter records.
    with pytest.raises(SourceSnapshotError, match='snapshot_newsletter_forbidden'):
        snapshot(tmp_path, capture, items)


def test_real_independent_reach_is_hot_once_and_retains_interest_reason(tmp_path, capture, cfg, policy):
    receipt = run(cfg, snapshot(tmp_path, capture, records(capture, CNN)), policy)
    hot = member(receipt, 'hot')
    assert len(hot) == 1
    assert hot[0]['primary_lane'] == 'hot'
    assert 'interested' in hot[0]['secondary_lanes']
    assert hot[0]['lane_reasons']['hot']
    assert hot[0]['lane_reasons']['interested']
    assert len(receipt['entries']) == 1
    assert len({e['story_id'] for e in receipt['entries']}) == len(receipt['entries'])
    assert receipt['shortfalls']['hot'] == policy['lane_quotas']['hot'] - 1
    assert receipt['shortfalls']['interested'] == policy['lane_quotas']['interested']


def test_new_mention_and_later_publication_do_not_create_updates(tmp_path, capture, cfg, policy):
    rows = records(capture, WIRED)
    publisher = next(i for i in rows if not i.is_aggregator)
    before = deepcopy(publisher)
    before.published_at -= timedelta(hours=1)
    previous = snapshot(tmp_path, capture, [before], at=NOW-timedelta(hours=1))
    current = snapshot(tmp_path, capture, rows)
    assert not member(run(cfg, current, policy, previous_snapshot=previous), 'updates')


def test_publisher_change_wins_overlap_and_binds_before_after(tmp_path, capture, cfg, policy):
    rows = records(capture, WIRED)
    publisher = next(i for i in rows if not i.is_aggregator)
    before = deepcopy(publisher)
    before.description = ''  # Controlled removal of publisher evidence.
    previous = snapshot(tmp_path, capture, [before], at=NOW-timedelta(hours=1))
    receipt = run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous)
    updates = member(receipt, 'updates')
    assert len(updates) == 1
    row = updates[0]
    assert row['primary_lane'] == 'updates'
    assert 'hot' in row['secondary_lanes']
    assert 'text' in str(row['lane_reasons']['updates']).lower()
    assert 'change' in str(row['lane_reasons']['updates']).lower()
    assert len(receipt['entries']) == 1
    assert receipt['shortfalls']['hot'] == policy['lane_quotas']['hot']


def test_old_baseline_cannot_claim_an_update_outside_the_updates_window(tmp_path, capture, cfg, policy):
    rows = records(capture, WIRED)
    publisher = next(i for i in rows if not i.is_aggregator)
    before = deepcopy(publisher)
    before.description = ''
    previous_path = tmp_path / 'manual-older-baseline.json'
    write_source_snapshot([TierResult('sources', [before])], previous_path,
                          generated_at=NOW-timedelta(hours=policy['windows']['updates'] + 1),
                          configuration_digest=capture.configuration_digest)
    previous = load_source_snapshot(previous_path, current_time=NOW,
                                    max_age_seconds=int(max(policy['windows'].values()) * 3600))
    receipt = run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous)
    assert not member(receipt, 'updates')
    assert replay_discovery(receipt)


@pytest.mark.parametrize('invalid_evidence', ['estimated', 'future', 'same_observation_time'])
def test_invalid_time_evidence_cannot_create_update(tmp_path, capture, cfg, policy, invalid_evidence):
    rows = records(capture, WIRED)
    publisher = next(i for i in rows if not i.is_aggregator)
    before = deepcopy(publisher)
    before.description = ''
    previous_at = NOW-timedelta(hours=1)
    if invalid_evidence == 'estimated':
        publisher.time_is_estimated = True
    elif invalid_evidence == 'future':
        publisher.published_at = NOW+timedelta(minutes=1)
    else:
        previous_at = NOW
    previous = snapshot(tmp_path, capture, [before], at=previous_at)
    if invalid_evidence == 'same_observation_time':
        with pytest.raises(DiscoveryError, match='discovery_previous_order'):
            run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous)
    else:
        receipt = run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous)
        assert not member(receipt, 'updates')


def test_aggregator_text_change_is_not_publisher_update(tmp_path, capture, cfg, policy):
    rows = records(capture, WIRED)
    before = deepcopy(rows)
    next(i for i in before if i.is_aggregator).description = ''
    # Title removal is the controlled corruption since captured HN description is empty.
    next(i for i in before if i.is_aggregator).title = next(i for i in before if i.is_aggregator).title[:-1]
    previous = snapshot(tmp_path, capture, before, at=NOW-timedelta(hours=1))
    assert not member(run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous), 'updates')


def test_surprise_requires_importance_quality_and_measured_history(tmp_path, capture, cfg, policy):
    rows = records(capture, STEEL)
    current = snapshot(tmp_path, capture, rows)
    receipt = run(cfg, current, policy)
    assert member(receipt, 'surprise')
    assert not member(receipt, 'hot')
    unknown_history = build_discovery(cfg, current, policy, now=NOW, history=None)
    assert not member(unknown_history, 'surprise')
    assert unknown_history['verdict'] == 'FAIL'
    publisher = next(i for i in rows if not i.is_aggregator)
    assert not member(run(cfg, snapshot(tmp_path, capture, [publisher]), policy), 'surprise')
    for i in rows:
        i.source_weight = 0.1  # Controlled degradation below source-quality gate.
    assert not member(run(cfg, snapshot(tmp_path, capture, rows), policy), 'surprise')


def test_active_failed_band_keeps_receipt_unpublishable(capture, cfg, policy):
    receipt = run(cfg, capture, policy)
    assert len(receipt['bands']) == 7
    assert any(b['active'] and b['verdict'] == 'FAIL' for b in receipt['bands'])
    assert receipt['verdict'] == 'FAIL'
    assert all(isinstance(n, int) and n >= 0 for n in receipt['shortfalls'].values())


@pytest.mark.parametrize('path,value', [
    (('size',), True), (('size',), float('nan')), (('size',), -1),
    (('windows', 'hot'), float('inf')), (('windows', 'updates'), True),
    (('gates', 'interest_threshold'), float('nan')),
    (('gates', 'hot_min_sources'), 1),
    (('components', 'freshness', 'weight'), -0.2),
    (('components', 'trend', 'enabled'), 1),
    (('lane_priority',), ['hot', 'hot', 'interested', 'surprise']),
    (('lane_quotas', 'hot'), 100),
])
def test_invalid_operational_policy_fails_closed(policy, path, value):
    altered = deepcopy(policy)
    target = altered
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises((DiscoveryError, ValueError)):
        validate_discovery_policy(altered)


def test_missing_unknown_policy_keys_rejected(policy):
    missing = deepcopy(policy)
    del missing['gates']['interest_threshold']
    with pytest.raises((DiscoveryError, ValueError)):
        validate_discovery_policy(missing)
    unknown = deepcopy(policy)
    unknown['gates']['unreviewed_override'] = True
    with pytest.raises((DiscoveryError, ValueError)):
        validate_discovery_policy(unknown)


def test_receipt_replay_is_deterministic_and_rejects_tamper(capture, cfg, policy):
    receipt = run(cfg, capture, policy)
    assert receipt == run(cfg, capture, policy)
    assert replay_discovery(receipt)
    altered = deepcopy(receipt)
    altered['shortfalls']['updates'] += 1
    with pytest.raises((DiscoveryError, ValueError)):
        replay_discovery(altered)
    altered = deepcopy(receipt)
    altered['entries'][0]['primary_lane'] = 'surprise'
    with pytest.raises((DiscoveryError, ValueError)):
        replay_discovery(altered)
    altered = deepcopy(receipt)
    altered['verdict'] = 'PASS'
    with pytest.raises((DiscoveryError, ValueError)):
        replay_discovery(altered)


def test_without_baseline_updates_stay_short_and_do_not_borrow(capture, cfg, policy):
    receipt = run(cfg, capture, policy)
    assert not member(receipt, 'updates')
    assert receipt['shortfalls']['updates'] == policy['lane_quotas']['updates']
    assert not [e for e in receipt['entries'] if e['primary_lane'] == 'updates']


def test_unknown_history_cannot_pass_repetition_band(capture, cfg, policy):
    receipt = build_discovery(cfg, capture, policy, now=NOW, history=None)
    repetition = next(b for b in receipt['bands'] if b['band'] == 'repetition')
    assert repetition['active'] is True
    assert repetition['verdict'] != 'PASS'
    assert receipt['verdict'] == 'FAIL'


def test_profile_absence_uses_explicit_shared_topic_reason(tmp_path, capture, cfg, policy):
    receipt = run(cfg, snapshot(tmp_path, capture, records(capture, TC)), policy)
    interested = member(receipt, 'interested')
    assert interested
    reasons = str(interested[0]['lane_reasons']['interested']).lower()
    assert 'topic' in reasons
    assert 'shared' in reasons or 'no personal' in reasons or 'no profile' in reasons


def test_replay_rejects_removed_hot_evidence_even_with_new_outer_checksum(tmp_path, capture, cfg, policy):
    import hashlib
    import json
    receipt = run(cfg, snapshot(tmp_path, capture, records(capture, CNN)), policy)
    assert member(receipt, 'hot')
    # Controlled evidence erasure preserves the existing input/code bindings.
    for row in receipt['candidates'] + receipt['entries']:
        row['evidence']['hot'] = []
    unsigned = {k: v for k, v in receipt.items() if k != 'receipt_digest'}
    receipt['receipt_digest'] = hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
    ).encode()).hexdigest()
    with pytest.raises(DiscoveryError):
        replay_discovery(receipt, expected_bindings=receipt['bindings'])


def test_absent_profile_is_unavailable_not_zero_count(capture, cfg, policy):
    receipt = run(cfg, capture, policy)
    assert receipt['profile_available'] is False
    assert receipt['bindings']['profile_revision'] is None
    assert receipt['bindings']['profile_digest'] is None


def test_source_cap_backfills_from_same_primary_lane(policy):
    # Constraint-only rows isolate the same-lane selection contract.
    policy['constraints']['max_per_source'] = 1
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 2, 'surprise': 0}
    rows = [
        {'story_id': 'source-cap-first', 'primary_lane': 'interested',
         'components': {'final_score': 3}, 'independent_source': 'source-a',
         'is_aggregator': False, 'display_cluster_id': 'one', 'topic_ids': [],
         'history_source_count': None, 'history_story_count': None},
        {'story_id': 'source-cap-rejected', 'primary_lane': 'interested',
         'components': {'final_score': 2}, 'independent_source': 'source-a',
         'is_aggregator': False, 'display_cluster_id': 'two', 'topic_ids': [],
         'history_source_count': None, 'history_story_count': None},
        {'story_id': 'source-cap-backfill', 'primary_lane': 'interested',
         'components': {'final_score': 1}, 'independent_source': 'source-b',
         'is_aggregator': False, 'display_cluster_id': 'three', 'topic_ids': [],
         'history_source_count': None, 'history_story_count': None},
    ]
    entries, shortfalls, rejected = _select(rows, policy)
    assert [entry['story_id'] for entry in entries] == ['source-cap-first', 'source-cap-backfill']
    assert entries[1]['backfilled'] is True
    assert rejected == [{'story_id': 'source-cap-rejected', 'reason': 'source_cap'}]
    assert shortfalls['interested'] == 0


def test_final_source_band_backfills_same_lane_without_shortening(policy):
    # Constraint-only rows isolate selection mechanics. They are not a news
    # receipt or source fixture and cannot be rendered as discovery content.
    policy['size'] = 17
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 17, 'surprise': 0}

    def row(rank, source):
        return {'story_id': f'selection-contract-{rank}', 'primary_lane': 'interested',
                'components': {'final_score': rank}, 'independent_source': source,
                'is_aggregator': False, 'display_cluster_id': f'cluster-{rank}',
                'topic_ids': [], 'history_source_count': None, 'history_story_count': None,
                'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}}

    candidates = [row(30-index, 'source-over-cap') for index in range(3)]
    candidates.extend(row(27-index, f'source-{index}') for index in range(15))
    initial, _, _ = _select(candidates, policy)
    initial_bands, _ = _bands(initial, policy, history_available=True)
    assert next(b for b in initial_bands if b['band'] == 'source_diversity')['verdict'] == 'FAIL'

    entries, _, rejected, bands, _ = _select_with_final_band_backfill(candidates, policy, True)
    source_band = next(b for b in bands if b['band'] == 'source_diversity')
    assert len(entries) == len(initial) == 17
    assert source_band['verdict'] == 'PASS'
    assert any(r['reason'] == 'source_diversity_cap' for r in rejected)
    assert all(entry['primary_lane'] == 'interested' for entry in entries)


def test_final_source_band_records_honest_shortfall_without_backfill(policy):
    # Constraint-only rows isolate an exhausted primary lane from news content.
    policy['size'] = 17
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 17, 'surprise': 0}

    def row(rank, source):
        return {'story_id': f'exhausted-selection-contract-{rank}', 'primary_lane': 'interested',
                'components': {'final_score': rank}, 'independent_source': source,
                'is_aggregator': False, 'display_cluster_id': f'exhausted-cluster-{rank}',
                'topic_ids': [], 'history_source_count': None, 'history_story_count': None,
                'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}}

    candidates = [row(30-index, 'source-over-cap') for index in range(3)]
    candidates.extend(row(27-index, f'source-{index}') for index in range(14))
    entries, shortfalls, rejected, bands, _ = _select_with_final_band_backfill(candidates, policy, True)
    source_band = next(b for b in bands if b['band'] == 'source_diversity')
    assert len(entries) == 16
    assert shortfalls['interested'] == 1
    assert source_band['verdict'] == 'PASS'
    assert any(r['reason'] == 'source_diversity_cap' for r in rejected)


def test_final_source_distinct_backfills_lowest_same_lane_duplicate(policy):
    policy['size'] = 5
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 5, 'surprise': 0}
    policy['bands']['source_diversity'].update({'cap': 1.0, 'min_distinct': 4})

    def row(rank, source):
        return {'story_id': f'distinct-selection-contract-{rank}', 'primary_lane': 'interested',
                'components': {'final_score': rank}, 'independent_source': source,
                'is_aggregator': False, 'display_cluster_id': f'distinct-cluster-{rank}',
                'topic_ids': [], 'history_source_count': None, 'history_story_count': None,
                'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}}

    candidates = [row(5, 'source-a'), row(4, 'source-a'), row(3, 'source-b'),
                  row(2, 'source-b'), row(1, 'source-c'), row(0, 'source-d')]
    entries, _, rejected, bands, _ = _select_with_final_band_backfill(candidates, policy, True)
    source_band = next(b for b in bands if b['band'] == 'source_diversity')
    assert source_band['verdict'] == 'PASS'
    assert source_band['distinct'] == 4
    assert any(r['reason'] == 'source_diversity_distinct' for r in rejected)
    assert any(r['story_id'] == 'distinct-selection-contract-0' and r['backfilled'] for r in entries)


def test_final_topic_distinct_backfills_lowest_same_lane_duplicate(policy):
    policy['size'] = 5
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 5, 'surprise': 0}
    policy['bands']['source_diversity'].update({'cap': 1.0, 'min_distinct': 0})
    policy['bands']['topic_diversity'].update({'cap': 1.0, 'min_distinct': 4})

    def row(rank, topic):
        return {'story_id': f'topic-distinct-contract-{rank}', 'primary_lane': 'interested',
                'components': {'final_score': rank}, 'independent_source': f'source-{rank}',
                'is_aggregator': False, 'display_cluster_id': f'topic-cluster-{rank}',
                'topic_ids': [topic], 'history_source_count': None, 'history_story_count': None,
                'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}}

    candidates = [row(5, 'a'), row(4, 'a'), row(3, 'b'), row(2, 'b'), row(1, 'c'), row(0, 'd')]
    entries, _, rejected, bands, _ = _select_with_final_band_backfill(candidates, policy, True)
    topic_band = next(b for b in bands if b['band'] == 'topic_diversity')
    assert topic_band['verdict'] == 'PASS'
    assert topic_band['distinct'] == 4
    assert any(r['reason'] == 'topic_diversity_distinct' for r in rejected)
    assert any(r['story_id'] == 'topic-distinct-contract-0' and r['backfilled'] for r in entries)


def test_zero_topic_share_cap_rejects_tagged_rows_without_relaxation(policy):
    policy['size'] = 2
    policy['lane_quotas'] = {'updates': 0, 'hot': 0, 'interested': 2, 'surprise': 0}
    policy['bands']['source_diversity'].update({'cap': 1.0, 'min_distinct': 0})
    policy['bands']['topic_diversity'].update({'cap': 0.0, 'min_distinct': 0})
    candidates = [
        {'story_id': 'zero-topic-tagged', 'primary_lane': 'interested',
         'components': {'final_score': 2}, 'independent_source': 'source-a',
         'is_aggregator': False, 'display_cluster_id': 'tagged', 'topic_ids': ['topic-a'],
         'history_source_count': None, 'history_story_count': None,
         'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}},
        {'story_id': 'zero-topic-untagged', 'primary_lane': 'interested',
         'components': {'final_score': 1}, 'independent_source': 'source-b',
         'is_aggregator': False, 'display_cluster_id': 'untagged', 'topic_ids': [],
         'history_source_count': None, 'history_story_count': None,
         'raw_components': {'relevance': 1}, 'age_hours': 0, 'lane_scores': {'interested': 1}},
    ]
    entries, shortfalls, rejected, bands, _ = _select_with_final_band_backfill(candidates, policy, True)
    topic_band = next(b for b in bands if b['band'] == 'topic_diversity')
    assert [row['story_id'] for row in entries] == ['zero-topic-untagged']
    assert shortfalls['interested'] == 1
    assert topic_band['verdict'] == 'PASS'
    assert any(r['reason'] == 'topic_diversity_cap' for r in rejected)


def test_local_selected_edition_history_blocks_repeat_surprise(tmp_path, capture, cfg, policy):
    current = snapshot(tmp_path, capture, records(capture, STEEL))
    first = run(cfg, current, policy)
    assert member(first, 'surprise')
    # History comes from the actual preceding local engine run, no user activity invented.
    history = [{'story_id': r['story_id'], 'source_id': r['independent_source'],
                'shown_at': first['generated_at']} for r in first['entries']]
    second = build_discovery(cfg, current, policy, now=NOW, history=history)
    assert not member(second, 'surprise')


def test_input_order_does_not_change_selected_story_order(tmp_path, capture, cfg, policy):
    rows = [deepcopy(i) for result in capture.results for i in result.items]
    left = run(cfg, snapshot(tmp_path, capture, rows), policy)
    right = run(cfg, snapshot(tmp_path, capture, list(reversed(rows))), policy)
    assert [r['story_id'] for r in left['entries']] == [r['story_id'] for r in right['entries']]
    assert [r['primary_lane'] for r in left['entries']] == [r['primary_lane'] for r in right['entries']]


def test_unknown_inputs_stay_distinct_from_measured_zero(capture, cfg, policy):
    missing = build_discovery(cfg, capture, policy, now=NOW, history=None)
    measured = build_discovery(cfg, capture, policy, now=NOW, history=[])
    assert missing['candidates'] and measured['candidates']
    for row in missing['candidates']:
        assert row['raw_components']['repetition_penalty'] is None
        assert row['raw_components']['source_fatigue_penalty'] is None
        assert row['raw_components']['editor_consensus'] is None
    for row in measured['candidates']:
        assert row['raw_components']['repetition_penalty'] == 0
        assert row['raw_components']['source_fatigue_penalty'] == 0
        assert row['raw_components']['editor_consensus'] is None
    assert missing['bindings']['profile_revision'] is None
    assert measured['bindings']['profile_revision'] is None


def test_scores_preserve_all_weighted_components(capture, cfg, policy):
    receipt = run(cfg, capture, policy)
    expected = set(policy['components']) | {'final_score'}
    for row in receipt['candidates']:
        assert set(row['components']) == expected
        assert row['components']['freshness'] <= policy['components']['freshness']['weight']
        assert row['components']['relevance'] <= policy['components']['relevance']['weight']
        assert row['components']['editor_consensus'] == 0
        positive = sum(row['components'][k] for k in policy['components'] if not k.endswith('_penalty'))
        penalties = sum(row['components'][k] for k in policy['components'] if k.endswith('_penalty'))
        assert row['components']['final_score'] == pytest.approx(positive - penalties)


def _resign(receipt):
    import hashlib
    import json
    unsigned = {key: value for key, value in receipt.items() if key != 'receipt_digest'}
    receipt['receipt_digest'] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
    ).encode()).hexdigest()


@pytest.mark.parametrize('field,value', [
    ('history_available', False), ('history_baseline', 'unavailable'),
    ('profile_available', True), ('profile_status', 'settled'),
    ('updates_baseline', 'available'),
])
def test_replay_derives_availability_from_bound_inputs(capture, cfg, policy, field, value):
    receipt = run(cfg, capture, policy)
    trusted = deepcopy(receipt['bindings'])
    receipt[field] = value
    _resign(receipt)
    with pytest.raises(DiscoveryError):
        replay_discovery(receipt, expected_bindings=trusted)


@pytest.mark.parametrize('field,value', [
    ('title', 'Controlled forged headline'), ('published_at', '2026-09-10T01:17:22Z'),
    ('source_id', 'controlled-forged-source'), ('topic_ids', []),
    ('lane_reasons', {'hot': 'Controlled false claim of 99 sources'}),
])
def test_replay_rejects_forged_selected_facts(capture, cfg, policy, field, value):
    receipt = run(cfg, capture, policy)
    trusted = deepcopy(receipt['bindings'])
    receipt['candidates'][0][field] = value
    _resign(receipt)
    with pytest.raises(DiscoveryError):
        replay_discovery(receipt, expected_bindings=trusted)


def test_replay_rejects_candidate_erasure_and_reordering(capture, cfg, policy):
    original = run(cfg, capture, policy)
    for candidates in (original['candidates'][1:], list(reversed(original['candidates']))):
        altered = deepcopy(original)
        altered['candidates'] = candidates
        _resign(altered)
        with pytest.raises(DiscoveryError):
            replay_discovery(altered, expected_bindings=original['bindings'])


def test_updates_require_same_publisher_route(tmp_path, capture, cfg, policy):
    routes = records(capture, TC)
    assert len({row.source_id for row in routes}) == 2
    previous = snapshot(tmp_path, capture, [routes[0]], at=NOW-timedelta(minutes=1))
    current = snapshot(tmp_path, capture, [routes[1]])
    assert not member(run(cfg, current, policy, previous_snapshot=previous), 'updates')


def test_replay_rejects_erased_and_forged_prior_observations(tmp_path, capture, cfg, policy):
    rows = records(capture, CNN)
    before = deepcopy(rows)
    for row in before:
        row.description = 'Controlled prior-text mutation for a negative integrity test.'
    previous = snapshot(tmp_path, capture, before, at=NOW-timedelta(minutes=1))
    original = run(cfg, snapshot(tmp_path, capture, rows), policy, previous_snapshot=previous)
    assert member(original, 'updates')
    for erase in (True, False):
        altered = deepcopy(original)
        changes = member(altered, 'updates')[0]['evidence']['changes']
        if erase:
            changes.clear()
        else:
            changes[0]['before']['description'] = 'Controlled invented prior fact.'
        _resign(altered)
        with pytest.raises(DiscoveryError):
            replay_discovery(altered, expected_bindings=original['bindings'])


def test_replay_requires_original_external_topic_binding(capture, cfg, policy):
    original = run(cfg, capture, policy)
    altered = deepcopy(original)
    altered['bindings']['topic_matches_digest'] = '0' * 64
    _resign(altered)
    with pytest.raises(DiscoveryError, match='expected_binding'):
        replay_discovery(altered, expected_bindings=original['bindings'])


def test_coverage_orders_fractional_seconds_chronologically(tmp_path, capture, cfg, policy):
    rows = records(capture, CNN)
    # Only clock precision changes. Real source identities and texts are retained.
    for index, row in enumerate(rows):
        row.published_at = NOW.replace(microsecond=0) - timedelta(seconds=1)
        if index:
            row.published_at += timedelta(microseconds=1)
    receipt = run(cfg, snapshot(tmp_path, capture, rows), policy)
    assert member(receipt, 'hot')
    assert replay_discovery(receipt)


def test_replay_pins_evaluation_clock(capture, cfg, policy):
    original = run(cfg, capture, policy)
    later = build_discovery(cfg, capture, policy, now=NOW+timedelta(hours=1), history=[])
    with pytest.raises(DiscoveryError, match='expected_binding'):
        replay_discovery(later, expected_bindings=original['bindings'])
    altered = deepcopy(original)
    altered['generated_at'] = later['generated_at']
    _resign(altered)
    with pytest.raises(DiscoveryError, match='evaluation_clock'):
        replay_discovery(altered, expected_bindings=original['bindings'])


def test_surprise_novelty_covers_history_beyond_repetition_window(tmp_path, capture, cfg, policy):
    current = snapshot(tmp_path, capture, records(capture, STEEL))
    first = run(cfg, current, policy)
    row = member(first, 'surprise')[0]
    # Controlled history timestamp isolates novelty from the shorter repetition penalty.
    history = [{'story_id': row['story_id'], 'source_id': row['independent_source'],
                'shown_at': (NOW-timedelta(hours=73)).isoformat()}]
    second = build_discovery(cfg, current, policy, now=NOW, history=history, first_edition=False)
    assert not member(second, 'surprise')
    assert replay_discovery(second, expected_bindings=second['bindings'])
