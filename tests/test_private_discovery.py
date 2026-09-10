"""Private adapter contracts over captured public news and simulated auth transport.

Synthetic subject IDs and request responses below exercise authorization plumbing,
never claim a real account login or become real-news demo evidence.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

import curator.private_discovery as module
from curator.private_discovery import (DiscoveryClient, DiscoveryLimits, PrivateDiscoveryError,
    materialize_private_discovery, select_entries,
    validate_discovery_response, write_private_json)
from curator.config import Category, Config
from curator.discovery import load_discovery_policy
from curator.models import TierResult
from curator.personalization import AuthConfig, Session
from curator.personalization.materializer import SecretPreferenceConfig
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest, write_source_snapshot

REPO = Path(os.environ.get('NEWS_CURATOR_TEST_REPO', Path(__file__).resolve().parents[1]))

NOW = datetime(2026, 9, 10, 1, 17, 22, 44155, tzinfo=timezone.utc)
OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
GIT = '4741a64744c35f98d9f14c1180470f9ccb766f95'
SECRET = SecretPreferenceConfig('https://contract.invalid', 'sb_secret_contract_only', OWNER)


@pytest.fixture
def inputs(tmp_path):
    cfg = Config(categories=[Category('AI', ['AI', 'Anthropic'])], rss=[], settings={}, ranking={}, dedup={}, hackernews={}, reddit={})
    capture = load_source_snapshot(REPO / 'tests' / 'fixtures' / 'discovery-captured.json', current_time=NOW)
    rows = [i for r in capture.results for i in r.items if i.canonical_url == 'https://cnn.com/2026/09/09/tech/ai-anthropic-safety']
    path = tmp_path / 'source.json'
    write_source_snapshot([TierResult('sources', rows)], path, generated_at=NOW, configuration_digest=snapshot_config_digest(cfg))
    snapshot = load_source_snapshot(path, current_time=NOW)
    policy = load_discovery_policy(REPO / 'config/discovery-policy-r2.yaml')
    policy['revision'] = 4
    policy['policy_id'] = 'private-adapter-contract-r4'
    # Explicit scoped policy exceptions isolate adapter finalization behavior.
    # Production r2 retains all seven bands and is separately tested failing.
    for name, band in policy['bands'].items():
        band['active'] = False
        policy['band_exceptions'][name] = 'Transport contract test only; not a publication policy.'
    return cfg, snapshot, policy


class Transport:
    def __init__(self):
        self.calls = []
        self.envelope = None
        self.payload_digest = None
        self.context_owner = OWNER
        self.finalize_status = 200
        self.tamper_readback = False
        self.first = True
        self.history_available = True
        self.reader_response = None
        self.reader_status = 200
        self.stored = {}
        self.profile_revision = 3
        self.topic_adjustment = 0.0
        self.commit_then_timeout = False
        self.retry_identity_status = 200

    def context(self):
        latest = None if self.envelope is None else {
            'edition_id': self.envelope['edition_id'], 'payload_digest': self.payload_digest,
            'receipt_digest': self.envelope['receipt']['receipt_digest'], 'generated_at': self.envelope['receipt']['generated_at']}
        if latest and self.tamper_readback:
            latest['payload_digest'] = '0' * 64
        history = [] if self.envelope is None else [
            {'story_id': e['story_id'], 'source_id': e['independent_source'], 'shown_at': self.envelope['receipt']['generated_at']}
            for e in self.envelope['receipt']['entries']]
        return {'schema_version': 1, 'owner_user_id': self.context_owner, 'history_available': self.history_available,
                'first_edition': self.first if self.envelope is None else False,
                'latest': latest, 'history': history, 'storage_policy': module.asdict(DiscoveryLimits())}

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append((method, url, deepcopy(body), dict(headers)))
        if '/user_preferences?' in url:
            assert 'user_id=eq.' + OWNER in url
            return 200, [{'revision': self.profile_revision, 'interests': ['AI']}]
        if url.endswith('/materialize_user_interest_signals'):
            assert body == {'p_user_id': OWNER}
            return 200, {'revision': self.profile_revision, 'topic_adjustments': ([{'topic_id': 'ai', 'adjustment': self.topic_adjustment}] if self.topic_adjustment else []), 'more_like_topic_weight': 0.8, 'topic_signal_limit': 100}
        if url.endswith('/private_discovery_context'):
            assert body == {'p_owner_user_id': OWNER}
            return 200, self.context()
        if url.endswith('/private_discovery_retry_identity'):
            if self.retry_identity_status != 200:
                return self.retry_identity_status, None
            for envelope, payload_digest in self.stored.values():
                receipt = envelope['receipt']
                expected = {
                    'p_owner_user_id': envelope['owner_user_id'],
                    'p_snapshot_digest': receipt['bindings']['snapshot_digest'],
                    'p_profile_revision': receipt['bindings']['profile_revision'],
                    'p_profile_fingerprint': envelope['profile_fingerprint'],
                    'p_policy_digest': receipt['bindings']['policy_digest'],
                    'p_code_revision': envelope['code_revision'],
                    'p_language': receipt['language'],
                    'p_ranking_configuration_digest': receipt['bindings']['ranking_configuration_digest'],
                    'p_display_dedup_digest': receipt['bindings']['display_dedup_digest'],
                    'p_code_digest': receipt['bindings']['code_digest'],
                }
                if body == expected:
                    return 200, {'edition_id': envelope['edition_id'], 'payload_digest': payload_digest,
                                 'receipt_digest': receipt['receipt_digest'],
                                 'generated_at': receipt['generated_at']}
            return 200, None
        if url.endswith('/private_discovery_identity'):
            assert body['p_owner_user_id'] == OWNER
            found = self.stored.get(body['p_edition_id'])
            if found is None:
                return 200, None
            envelope, payload_digest = found
            return 200, {'edition_id': envelope['edition_id'],
                         'payload_digest': '0'*64 if self.tamper_readback else payload_digest,
                         'receipt_digest': envelope['receipt']['receipt_digest'],
                         'generated_at': envelope['receipt']['generated_at']}
        if url.endswith('/finalize_private_discovery'):
            if self.finalize_status != 200:
                return self.finalize_status, None
            text = body['p_payload_text']
            assert hashlib.sha256(text.encode()).hexdigest() == body['p_payload_digest']
            assert SECRET.secret_key not in text
            self.envelope, self.payload_digest = json.loads(text), body['p_payload_digest']
            assert self.envelope['edition_id'] not in self.stored
            self.stored[self.envelope['edition_id']] = (deepcopy(self.envelope), self.payload_digest)
            if self.commit_then_timeout:
                raise TimeoutError('Simulated transport timeout after commit')
            return 200, {'schema_version': 1, 'status': 'stored', 'edition_id': self.envelope['edition_id'], 'payload_digest': self.payload_digest}
        if url.endswith('/discovery_edition'):
            assert set(body) == {'p_edition_id'}
            return self.reader_status, self.reader_response
        raise AssertionError('Unexpected endpoint')


def build(inputs, transport):
    cfg, snapshot, policy = inputs
    return materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT, now=NOW, transport=transport)


def response_for(envelope):
    receipt = envelope['receipt']
    entries = []
    for entry in receipt['entries']:
        fact = envelope['cards'][entry['story_id']]
        card = dict(fact, story_id=entry['story_id'], publication_seq=0, position=0,
            score_components=entry['components'], ordering_mode='weighted_total',
            ordering_key={'final_score': entry['components']['final_score']}, topic_ranks={},
            ranking_explanation=entry['plain_reason'], page_order_mode='discovery', next_cursor=None,
            saved_at=None, read_at=None, state_revision=0, interests=[])
        entries.append({'position': entry['position'], 'primary_lane': entry['primary_lane'],
                        'reason': entry['plain_reason'],
                        'secondary_reasons': [{'lane': lane, 'reason': entry['lane_reasons'][lane]} for lane in entry['secondary_lanes']], 'card': card})
    return {'schema_version': 1, 'status': 'ready', 'reason_code': '', 'edition': {
        'edition_id': envelope['edition_id'], 'generated_at': receipt['generated_at'], 'code_revision': envelope['code_revision'],
        'policy_revision': receipt['policy']['revision'], 'policy_digest': receipt['bindings']['policy_digest'],
        'snapshot_digest': receipt['bindings']['snapshot_digest'], 'profile_revision': receipt['bindings']['profile_revision'],
        'receipt_digest': receipt['receipt_digest'], 'stale': False, 'disclosures': receipt['disclosures'],
        'shortfalls': receipt['shortfalls'], 'entries': entries}}


def test_build_fetches_subject_inputs_and_verifies_readback(inputs):
    transport = Transport()
    result = build(inputs, transport)
    assert result['status'] == 'stored'
    envelope = transport.envelope
    assert envelope['owner_user_id'] == OWNER
    assert envelope['receipt']['publishable'] is False
    assert envelope['receipt']['verdict'] == 'PASS'
    assert envelope['receipt']['profile_status'] == 'settled'
    assert envelope['receipt']['history_baseline'] == 'first_local_edition'
    assert set(envelope['cards']) == {e['story_id'] for e in envelope['receipt']['entries']}
    assert len([c for c in transport.calls if c[1].endswith('/private_discovery_context')]) == 1


@pytest.mark.parametrize('field,value', [('context_owner', OTHER), ('history_available', False), ('first', False)])
def test_wrong_or_unproven_history_refuses_before_write(inputs, field, value):
    transport = Transport()
    setattr(transport, field, value)
    with pytest.raises(PrivateDiscoveryError):
        build(inputs, transport)
    assert transport.envelope is None


def test_failed_bands_keep_previous_edition(inputs):
    cfg, snapshot, _ = inputs
    policy = load_discovery_policy(REPO / 'config/discovery-policy-r2.yaml')
    transport = Transport()
    result = build((cfg, snapshot, policy), transport)
    assert result['status'] == 'not_settled'
    assert set(result) == {'schema_version', 'status', 'reason_code', 'selected_count',
                           'shortfalls', 'failed_bands'}
    assert result['failed_bands']
    assert all(set(band) == {'band', 'verdict'} for band in result['failed_bands'])
    assert all(band['verdict'] == 'FAIL' for band in result['failed_bands'])
    assert transport.envelope is None


def test_failed_band_diagnostics_exclude_qualified_shortfall(inputs):
    cfg, snapshot, _ = inputs
    policy = load_discovery_policy(REPO / 'config/discovery-policy-r3.yaml')
    policy['bands']['topic_diversity']['cap'] = 0.1
    policy['bands']['deliberate_surprise'].update(floor=0.5, cap=1.0)
    result = build((cfg, snapshot, policy), Transport())
    assert result['status'] == 'not_settled'
    assert result['failed_bands']
    assert all(band['verdict'] == 'FAIL' for band in result['failed_bands'])
    assert 'topic_diversity' in {band['band'] for band in result['failed_bands']}
    assert 'deliberate_surprise' not in {band['band'] for band in result['failed_bands']}


def test_digest_mismatch_readback_refuses_completion(inputs):
    transport = Transport()
    transport.tamper_readback = True
    with pytest.raises(PrivateDiscoveryError):
        build(inputs, transport)


def test_finalizer_conflict_refuses_completion(inputs):
    transport = Transport()
    transport.finalize_status = 409
    with pytest.raises(PrivateDiscoveryError):
        build(inputs, transport)


def test_materializer_has_no_arbitrary_receipt_parameter(inputs):
    import inspect
    assert 'receipt' not in inspect.signature(materialize_private_discovery).parameters
    assert 'interest_artifact' not in inspect.signature(materialize_private_discovery).parameters
    assert 'owner_user_id' not in inspect.signature(materialize_private_discovery).parameters


def test_reader_same_order_and_reasons_no_owner_parameter(inputs):
    transport = Transport()
    build(inputs, transport)
    transport.reader_response = response_for(transport.envelope)
    config = AuthConfig('https://contract.invalid', 'sb_publishable_contract_only')
    client = DiscoveryClient(config, transport=transport)
    session = Session('controlled-access', 'controlled-refresh', 9999999999, OWNER)
    result = client.read(session)
    assert result == transport.reader_response
    assert select_entries(result, lane='hot') == result['edition']['entries']
    assert 'owner_user_id' not in transport.calls[-1][2]
    assert transport.calls[-1][3]['cache-control'] == 'no-store'


def test_reader_accepts_empty_topics_preserving_card_state(inputs):
    transport = Transport()
    build(inputs, transport)
    result = response_for(transport.envelope)
    result['edition']['entries'][0]['card']['topic_ids'] = []
    assert validate_discovery_response(result) == result


@pytest.mark.parametrize('mutation', ['duplicate', 'score', 'public_sequence', 'owner', 'size'])
def test_reader_rejects_invalid_projection(inputs, mutation):
    transport = Transport()
    build(inputs, transport)
    result = response_for(transport.envelope)
    entry = result['edition']['entries'][0]
    limits = DiscoveryLimits()
    if mutation == 'duplicate':
        result['edition']['entries'].append(deepcopy(entry))
    elif mutation == 'score':
        entry['card']['score_components']['final_score'] += 1
    elif mutation == 'public_sequence':
        entry['card']['publication_seq'] = 1
    elif mutation == 'owner':
        result['owner_user_id'] = OWNER
    else:
        limits = DiscoveryLimits(max_response_bytes=50)
    with pytest.raises(PrivateDiscoveryError):
        validate_discovery_response(result, limits=limits)


def test_unavailable_then_other_account_response_is_not_cached():
    transport = Transport()
    config = AuthConfig('https://contract.invalid', 'sb_publishable_contract_only')
    client = DiscoveryClient(config, transport=transport)
    transport.reader_response = {'schema_version': 1, 'status': 'unavailable', 'reason_code': 'no_private_edition', 'edition': None}
    assert client.read(Session('A', 'AR', 9999999999, OWNER))['edition'] is None
    transport.reader_status = 401
    with pytest.raises(PrivateDiscoveryError):
        client.read(Session('B', 'BR', 9999999999, OTHER))
    assert transport.calls[-1][3]['authorization'] == 'Bearer B'


def test_explicit_export_mode_is_private(tmp_path):
    path = tmp_path / 'export.json'
    write_private_json(path, {'status': 'unavailable'})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {'status': 'unavailable'}


def test_read_cli_prints_only_aggregates(inputs, monkeypatch, capsys):
    transport = Transport()
    build(inputs, transport)
    result = response_for(transport.envelope)
    spec = importlib.util.spec_from_file_location('private_read_cli', REPO / 'scripts/read_discovery.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_URL', 'https://contract.invalid')
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_contract_only')
    class Auth:
        def __init__(self, *args): pass
        def valid_session(self): return Session('controlled', 'controlled', 9999999999, OWNER)
    class Client:
        def __init__(self, *args): pass
        def read(self, *args, **kwargs): return result
    monkeypatch.setattr(cli, 'AgentAuth', Auth)
    monkeypatch.setattr(cli, 'MacOSKeychainStorage', lambda **kwargs: None)
    monkeypatch.setattr(cli, 'DiscoveryClient', Client)
    assert cli.main(['read', '--lane', 'hot']) == 0
    printed = capsys.readouterr().out
    assert OWNER not in printed and 'Anthropic' not in printed and 'story:' not in printed
    assert json.loads(printed)['selected_count'] == 1


def test_known_empty_retained_history_is_not_first_edition(inputs):
    transport = Transport()
    original_context = transport.context
    def context():
        value = original_context()
        if transport.envelope is None:
            value['first_edition'] = False
            value['latest'] = {'edition_id': 'prior-retained-edition', 'payload_digest': 'a'*64,
                               'receipt_digest': 'b'*64, 'generated_at': '2026-08-01T00:00:00Z'}
        return value
    transport.context = context
    assert build(inputs, transport)['status'] == 'stored'
    assert transport.envelope['receipt']['history_baseline'] == 'supplied_history'
    assert transport.envelope['receipt']['bindings']['history_first_edition'] is False


@pytest.mark.parametrize('field,value', [('history_window_hours', 1), ('max_payload_bytes', 100)])
def test_database_policy_bounds_fail_closed(inputs, field, value):
    transport = Transport()
    original_context = transport.context
    def context():
        result = original_context()
        result['storage_policy'][field] = value
        return result
    transport.context = context
    with pytest.raises(PrivateDiscoveryError):
        build(inputs, transport)
    assert transport.envelope is None


def test_pinned_edition_cannot_return_another_id(inputs):
    transport = Transport()
    build(inputs, transport)
    transport.reader_response = response_for(transport.envelope)
    client = DiscoveryClient(AuthConfig('https://contract.invalid', 'sb_publishable_contract_only'), transport=transport)
    with pytest.raises(PrivateDiscoveryError):
        client.read(Session('controlled', 'controlled', 9999999999, OWNER), edition_id='another-edition')



def test_same_observation_retry_does_not_finalize_twice(inputs):
    transport = Transport()
    first = build(inputs, transport)
    cfg, snapshot, policy = inputs
    second = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert second['status'] == 'already_stored'
    assert second['edition_id'] == first['edition_id']
    assert second['payload_digest'] == first['payload_digest']
    assert 'selected_count' not in second
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_uncertain_commit_resolves_same_identity_without_another_write(inputs):
    transport = Transport()
    transport.commit_then_timeout = True
    result = build(inputs, transport)
    assert result['status'] == 'already_stored'
    cfg, snapshot, policy = inputs
    retry = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert retry == result
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_retry_hidden_by_newer_latest_resolves_original_identity(inputs):
    transport = Transport()
    first = build(inputs, transport)
    # Simulate another retained edition becoming latest. The identity registry
    # remains authoritative for the earlier committed input observation.
    newer = deepcopy(transport.envelope)
    newer['edition_id'] = 'm2:' + 'f'*64
    transport.envelope = newer
    cfg, snapshot, policy = inputs
    retry = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert retry['status'] == 'already_stored'
    assert retry['edition_id'] == first['edition_id']
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_profile_revision_change_creates_distinct_input_identity(inputs):
    transport = Transport()
    first = build(inputs, transport)
    transport.profile_revision += 1
    cfg, snapshot, policy = inputs
    result = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    # Existing history may honestly leave the new input short. It must still
    # be evaluated, not mistaken for the prior profile's already-settled run.
    assert result['status'] != 'already_stored'
    identity_reads = [call[2]['p_edition_id'] for call in transport.calls if call[1].endswith('/private_discovery_identity')]
    assert identity_reads[-1] != first['edition_id']


def test_lower_signal_revision_change_does_not_collide_with_max_profile_revision(inputs):
    transport = Transport()
    first = build(inputs, transport)
    transport.topic_adjustment = 0.25
    cfg, snapshot, policy = inputs
    result = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert transport.profile_revision == 3
    assert result['status'] != 'already_stored'
    identity_reads = [call[2]['p_edition_id'] for call in transport.calls if call[1].endswith('/private_discovery_identity')]
    assert identity_reads[-1] != first['edition_id']


def test_updates_eligible_retry_is_one_settlement(inputs, tmp_path):
    cfg, snapshot, policy = inputs
    earlier_rows = deepcopy([item for result in snapshot.results for item in result.items])
    for item in earlier_rows:
        if not item.is_aggregator:
            # Controlled prior title truncation uses captured current text
            # to test changed-observation plumbing, not historical news claims.
            item.title = item.title[:-1]
    previous_path = tmp_path / 'controlled-prior-observation.json'
    write_source_snapshot([TierResult('sources', earlier_rows)], previous_path,
        generated_at=NOW - timedelta(minutes=1), configuration_digest=snapshot.configuration_digest)
    previous = load_source_snapshot(previous_path, current_time=NOW)
    transport = Transport()
    first = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        previous_snapshot=previous, now=NOW, transport=transport)
    assert transport.envelope['receipt']['entries'][0]['primary_lane'] == 'updates'
    retry = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        previous_snapshot=previous, now=NOW + timedelta(seconds=1), transport=transport)
    assert retry['status'] == 'already_stored' and retry['edition_id'] == first['edition_id']
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_automatic_baseline_first_edition_and_retry_skip_second_lookup(inputs):
    cfg, snapshot, policy = inputs
    transport = Transport()
    anchors = []

    def first_loader(anchor):
        anchors.append(anchor)
        return None

    first = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW, transport=transport, baseline_loader=first_loader)
    assert anchors == [None]

    def forbidden_loader(anchor):
        raise AssertionError('an exact retry must not depend on baseline artifacts')

    retry = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport, baseline_loader=forbidden_loader)
    assert retry['status'] == 'already_stored' and retry['edition_id'] == first['edition_id']
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_automatic_retry_finds_matching_edition_older_than_latest(inputs):
    cfg, snapshot, policy = inputs
    transport = Transport()
    first = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW, transport=transport, baseline_loader=lambda anchor: None)
    older_envelope = deepcopy(transport.envelope)
    newer_envelope = deepcopy(older_envelope)
    newer_envelope['edition_id'] = 'm2:' + 'f' * 64
    newer_envelope['code_revision'] = 'e' * 40
    newer_envelope['receipt']['generated_at'] = (NOW + timedelta(seconds=1)).isoformat()
    newer_digest = 'd' * 64
    transport.envelope = newer_envelope
    transport.payload_digest = newer_digest
    transport.stored[newer_envelope['edition_id']] = (newer_envelope, newer_digest)

    def forbidden_loader(anchor):
        raise AssertionError('an older exact retry must not depend on baseline artifacts')

    retry = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=2), transport=transport, baseline_loader=forbidden_loader)
    assert retry['status'] == 'already_stored' and retry['edition_id'] == first['edition_id']


def test_automatic_baseline_tuple_change_uses_latest_generation_anchor(inputs):
    cfg, snapshot, policy = inputs
    transport = Transport()
    materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW, transport=transport, baseline_loader=lambda anchor: None)
    transport.topic_adjustment = 0.25
    anchors = []
    result = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport,
        baseline_loader=lambda anchor: anchors.append(anchor) or None)
    assert anchors == [NOW]
    assert result['status'] != 'already_stored'


def test_automatic_baseline_failure_preserves_existing_state(inputs):
    cfg, snapshot, policy = inputs
    transport = Transport()
    first = build(inputs, transport)
    transport.topic_adjustment = 0.25

    def unavailable(anchor):
        raise OSError('controlled baseline transport failure')

    with pytest.raises(OSError, match='controlled baseline transport failure'):
        materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
            now=NOW + timedelta(seconds=1), transport=transport, baseline_loader=unavailable)
    assert list(transport.stored) == [first['edition_id']]
    assert len([call for call in transport.calls if call[1].endswith('/finalize_private_discovery')]) == 1


def test_automatic_retry_identity_ambiguity_fails_before_baseline_or_write(inputs):
    cfg, snapshot, policy = inputs
    transport = Transport()
    transport.retry_identity_status = 409
    called = False

    def loader(anchor):
        nonlocal called
        called = True

    with pytest.raises(PrivateDiscoveryError):
        materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
            now=NOW, transport=transport, baseline_loader=loader)
    assert called is False and transport.envelope is None



@pytest.mark.parametrize('setting', ['ranking', 'dedup'])
def test_runtime_ranking_configuration_changes_input_identity(inputs, setting):
    transport = Transport()
    first = build(inputs, transport)
    cfg, snapshot, policy = inputs
    cfg = deepcopy(cfg)
    if setting == 'ranking':
        cfg.ranking['half_life_hours'] = 13
    else:
        cfg.dedup['title_similarity_threshold'] = 0.95
    result = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert result['status'] != 'already_stored'
    identity_reads = [call[2]['p_edition_id'] for call in transport.calls if call[1].endswith('/private_discovery_identity')]
    assert identity_reads[-1] != first['edition_id']



def test_runtime_engine_digest_changes_input_identity(inputs, monkeypatch):
    transport = Transport()
    first = build(inputs, transport)
    original = Path.read_bytes
    engine_path = Path(module.discovery.__file__)
    def changed_engine_bytes(path):
        raw = original(path)
        return raw + b'\n# Controlled changed-code digest evidence\n' if path == engine_path else raw
    monkeypatch.setattr(Path, 'read_bytes', changed_engine_bytes)
    cfg, snapshot, policy = inputs
    result = materialize_private_discovery(cfg, snapshot, policy, SECRET, code_revision=GIT,
        now=NOW + timedelta(seconds=1), transport=transport)
    assert result['status'] != 'already_stored'
    identity_reads = [call[2]['p_edition_id'] for call in transport.calls if call[1].endswith('/private_discovery_identity')]
    assert identity_reads[-1] != first['edition_id']
