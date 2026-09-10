"""Service materialization and authenticated reads of private M2 editions.

A producer fetches the configured subject's profile and DB-proven history itself.
No API accepts an arbitrary preview receipt to attach an owner afterward.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request

from . import discovery
from .filter import topic_match
from .identity import story_id_for_item
from .dashboard import _CARD_FIELDS, _bounded, _integer, _safe_url, _timestamp
from .personalization import AuthConfig, AuthError, Session
from .personalization.preferences import JsonRestTransport
from .personalization.materializer import SecretPreferenceConfig, fetch_interest_profile
from .personalization.ranking import InterestArtifact, build_interest_artifact, ranking_config_digest

_SHA = re.compile(r'^[0-9a-f]{64}$')
_GIT = re.compile(r'^[0-9a-f]{40}$')
_STORY = re.compile(r'^story:[0-9a-f]{64}$')
_TOPIC = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
_EDITION = re.compile(r'^[A-Za-z0-9:_-]{1,100}$')


class PrivateDiscoveryError(RuntimeError):
    """A safe error that never includes credentials, profile or source content."""


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _require(condition):
    if not condition:
        raise PrivateDiscoveryError('Private discovery could not be verified.')


def _exact(value, keys):
    _require(isinstance(value, dict) and set(value) == set(keys))


def _sha(value):
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        _require(parsed.tzinfo is not None)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PrivateDiscoveryError('Private discovery timestamp was invalid.') from None


def _stamp(value):
    return _time(value).isoformat().replace('+00:00', 'Z')


@dataclass(frozen=True)
class DiscoveryLimits:
    max_entries: int = 100
    max_payload_bytes: int = 16 * 1024 * 1024
    max_response_bytes: int = 1024 * 1024
    staleness_hours: float = 27
    history_window_hours: float = 168
    max_history_rows: int = 10000

    def __post_init__(self):
        for name in ('max_entries', 'max_payload_bytes', 'max_response_bytes', 'max_history_rows'):
            _require(_integer(getattr(self, name), minimum=1))
        for name in ('staleness_hours', 'history_window_hours'):
            value = getattr(self, name)
            _require(type(value) in (int, float) and math.isfinite(value) and value > 0)

    @classmethod
    def from_mapping(cls, value):
        _exact(value, cls.__dataclass_fields__)
        return cls(**value)


class DiscoveryTransport(JsonRestTransport):
    """Existing no-proxy/no-redirect transport with separate discovery bounds."""

    def __init__(self, limits: DiscoveryLimits | None = None):
        super().__init__()
        self.limits = limits or DiscoveryLimits()

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        data = None if body is None else canonical_json(body).encode('utf-8')
        # The RPC wraps a canonical JSON string, whose quotes are escaped once.
        _require(data is None or len(data) <= 2 * self.limits.max_payload_bytes + 1024)
        request = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
        try:
            with self._opener.open(request, timeout=timeout) as response:
                _require(response.geturl() == url)
                raw = response.read(self.limits.max_response_bytes + 1)
                _require(len(raw) <= self.limits.max_response_bytes)
                if not raw:
                    return response.status, None
                try:
                    return response.status, json.loads(raw)
                except (ValueError, UnicodeError):
                    raise PrivateDiscoveryError('Private discovery response was invalid.') from None
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PrivateDiscoveryError('Private discovery service was unavailable.') from None


def _headers(config):
    headers = {'apikey': config.secret_key, 'accept': 'application/json', 'content-type': 'application/json'}
    if not config.secret_key.startswith('sb_secret_'):
        headers['authorization'] = 'Bearer ' + config.secret_key
    return headers


def _rpc(client, config, name, body):
    try:
        status, payload = client.request('POST', config.supabase_url + '/rest/v1/rpc/' + name,
                                         headers=_headers(config), body=body)
    except (AuthError, OSError, TimeoutError):
        raise PrivateDiscoveryError('Private discovery service was unavailable.') from None
    _require(status == 200)
    return payload


def validate_context(value, owner, *, now, policy):
    _exact(value, ('schema_version', 'owner_user_id', 'history_available', 'first_edition', 'latest', 'history', 'storage_policy'))
    _require(type(value['schema_version']) is int and value['schema_version'] == 1)
    _require(value['owner_user_id'] == owner and value['history_available'] is True and type(value['first_edition']) is bool)
    limits = DiscoveryLimits.from_mapping(value['storage_policy'])
    _require(limits.history_window_hours >= max(policy['windows']['repetition'], policy['windows']['source_fatigue']))
    rows = value['history']
    _require(isinstance(rows, list) and len(rows) <= limits.max_history_rows)
    for row in rows:
        _exact(row, ('story_id', 'source_id', 'shown_at'))
        _require(isinstance(row['story_id'], str) and bool(_STORY.fullmatch(row['story_id'])))
        _require(isinstance(row['source_id'], str) and 0 < len(row['source_id']) <= 256)
        age = (_time(now) - _time(row['shown_at'])).total_seconds()/3600
        _require(0 <= age <= limits.history_window_hours)
    latest = value['latest']
    if latest is not None:
        _exact(latest, ('edition_id', 'payload_digest', 'receipt_digest', 'generated_at'))
        _require(isinstance(latest['edition_id'], str) and bool(_EDITION.fullmatch(latest['edition_id'])))
        _require(_sha(latest['payload_digest']) and _sha(latest['receipt_digest']))
        _require(_time(latest['generated_at']) <= _time(now))
    _require(not value['first_edition'] or (not rows and latest is None))
    _require(value['first_edition'] or latest is not None)
    return limits


def _source_cards(receipt):
    cards = {}
    for entry in receipt['entries']:
        coverage = []
        seen = set()
        for observation in entry['evidence']['observations']:
            key = (observation['source_id'], observation['published_at'], observation['url'], observation['title'])
            if key in seen:
                continue
            seen.add(key)
            coverage.append({'headline': observation['title'], 'mentioned_at': observation['published_at'],
                             'source_id': observation['source_id'], 'source_kind': 'outlet',
                             'source_name': observation['source_name'], 'url': observation['url']})
        # Refuse over-bound source evidence instead of silently dropping it.
        _require(len(coverage) <= 20)
        cards[entry['story_id']] = {
            'canonical_url': entry['url'], 'title': entry['title'], 'summary': entry['description'],
            'language': entry['language'], 'published_at': entry['published_at'],
            'topic_ids': entry['topic_ids'], 'source_kind': 'outlet', 'source_name': entry['source_name'],
            'coverage_mentions': coverage,
        }
    return cards


def _validate_identity(value, edition_id, now):
    if value is None:
        return None
    _exact(value, ('edition_id', 'payload_digest', 'receipt_digest', 'generated_at'))
    _require(value['edition_id'] == edition_id and _sha(value['payload_digest']) and _sha(value['receipt_digest']))
    _require(_time(value['generated_at']) <= now)
    return value


def _existing_identity(client, config, edition_id, now):
    value = _rpc(client, config, 'private_discovery_identity',
                 {'p_owner_user_id': config.owner_user_id, 'p_edition_id': edition_id})
    return _validate_identity(value, edition_id, now)


def _retry_identity(client, config, *, snapshot_digest, profile_revision, profile_fingerprint,
                    policy_digest, code_revision, language, ranking_configuration_digest,
                    display_dedup_digest, code_digest, now):
    value = _rpc(client, config, 'private_discovery_retry_identity', {
        'p_owner_user_id': config.owner_user_id, 'p_snapshot_digest': snapshot_digest,
        'p_profile_revision': profile_revision, 'p_profile_fingerprint': profile_fingerprint,
        'p_policy_digest': policy_digest, 'p_code_revision': code_revision, 'p_language': language,
        'p_ranking_configuration_digest': ranking_configuration_digest,
        'p_display_dedup_digest': display_dedup_digest, 'p_code_digest': code_digest,
    })
    return None if value is None else _validate_identity(value, value.get('edition_id'), now)


def _already_stored(identity):
    # Metadata proves the existing commit. Its entry count is not in this
    # bounded response, so a retry must not invent the current build's count.
    return {'schema_version': 1, 'status': 'already_stored',
            'edition_id': identity['edition_id'], 'payload_digest': identity['payload_digest']}


def materialize_private_discovery(cfg, snapshot, policy, secret_config: SecretPreferenceConfig, *,
                                  previous_snapshot=None, code_revision: str, now=None,
                                  language='en', transport=None, baseline_loader=None):
    """Fetch a real subject's own inputs, then settle only a replay-verified PASS."""
    _require(isinstance(code_revision, str) and bool(_GIT.fullmatch(code_revision)))
    now = _time(now or datetime.now(timezone.utc))
    p = discovery.validate_discovery_policy(policy)
    client = transport or DiscoveryTransport()
    # The existing getter makes an exact configured-owner query. It never takes
    # a caller-supplied score artifact or an arbitrary user ID from a receipt.
    profile = fetch_interest_profile(secret_config, transport=client)
    context = _rpc(client, secret_config, 'private_discovery_context', {'p_owner_user_id': secret_config.owner_user_id})
    limits = validate_context(context, secret_config.owner_user_id, now=now, policy=p)
    if isinstance(client, DiscoveryTransport):
        client.limits = limits
    _require(language in ('en', 'zh'))
    _require(previous_snapshot is None or baseline_loader is None)
    # Identity follows immutable observed inputs, not evaluation time or the
    # history that the first successful commit itself creates. Full fetched
    # profile content is included because max(preference, signal revision) is
    # not unique when the smaller of those independent counters changes.
    profile_fingerprint = digest(asdict(profile))
    policy_digest = digest(p)
    ranking_digest = ranking_config_digest(cfg)
    dedup_digest = digest({'threshold': cfg.dedup.get('title_similarity_threshold', 0.90),
                           'time_bucket_hours': cfg.dedup.get('time_bucket_hours', 36.0)})
    code_digest = hashlib.sha256(Path(discovery.__file__).read_bytes()).hexdigest()
    if baseline_loader is not None:
        retry = _retry_identity(client, secret_config, snapshot_digest=snapshot.content_digest,
            profile_revision=profile.revision, profile_fingerprint=profile_fingerprint,
            policy_digest=policy_digest, code_revision=code_revision, language=language,
            ranking_configuration_digest=ranking_digest, display_dedup_digest=dedup_digest,
            code_digest=code_digest, now=now)
        if retry is not None:
            return _already_stored(retry)
        anchor = None if context['latest'] is None else _time(context['latest']['generated_at'])
        previous_snapshot = baseline_loader(anchor)
    edition_id = 'm2:' + digest([secret_config.owner_user_id, snapshot.content_digest,
        previous_snapshot.content_digest if previous_snapshot else None,
        profile.revision, profile_fingerprint, policy_digest, code_revision, language,
        ranking_digest, dedup_digest, code_digest])
    if context['latest'] is not None and context['latest']['edition_id'] == edition_id:
        return _already_stored(_validate_identity(context['latest'], edition_id, now))
    existing = _existing_identity(client, secret_config, edition_id, now)
    if existing is not None:
        return _already_stored(existing)
    _require(0 <= (now-_time(snapshot.generated_at)).total_seconds() <= cfg.source_snapshot_max_age_seconds)
    profile_payload = build_interest_artifact(profile,
        [i for result in snapshot.results for i in result.items],
        source_snapshot_digest=snapshot.content_digest,
        configuration_digest=ranking_config_digest(cfg), generated_at=now, categories=cfg.categories)
    artifact = InterestArtifact(**{k: v for k, v in profile_payload.items() if k != 'schema_version'})
    receipt = discovery.build_discovery(cfg, snapshot, p, previous_snapshot=previous_snapshot,
                                       interest_artifact=artifact, history=context['history'], first_edition=context['first_edition'], now=now, language=language)
    item_groups = defaultdict(list)
    for result in snapshot.results:
        for item in result.items:
            if item.language == language and not item.is_newsletter:
                item_groups[story_id_for_item(item)].append(item)
    topic_matches = {story_id: sorted({category.id for category in cfg.categories
                     for item in items if topic_match(item, category) is not None})
                     for story_id, items in sorted(item_groups.items())}
    expected = {
        'evaluation_clock': _stamp(now),
        'history_first_edition': context['first_edition'],
        'observation_clock': _stamp(snapshot.generated_at),
        'previous_observation_clock': _stamp(previous_snapshot.generated_at) if previous_snapshot else None,
        'previous_observations_digest': digest(discovery.extract_observations(previous_snapshot, language)) if previous_snapshot else None,
        'topic_matches_digest': digest(topic_matches),
        'snapshot_digest': snapshot.content_digest,
        'configuration_digest': snapshot.configuration_digest,
        'ranking_configuration_digest': ranking_config_digest(cfg),
        'previous_snapshot_digest': previous_snapshot.content_digest if previous_snapshot else None,
        'profile_digest': digest(asdict(artifact)), 'profile_revision': profile.revision,
        'history_digest': digest(context['history']), 'policy_digest': policy_digest,
        'observations_digest': digest(discovery.extract_observations(snapshot, language)),
        'code_digest': code_digest, 'display_dedup_digest': dedup_digest,
    }
    _require(set(receipt['bindings']) == set(expected))
    discovery.replay_discovery(receipt, expected_bindings=expected)
    if receipt['verdict'] != 'PASS':
        failed_bands = [
            {'band': band['band'], 'verdict': band['verdict']}
            for band in receipt['bands']
            if band['verdict'] not in ('PASS', 'DISABLED')
        ]
        return {'schema_version': 1, 'status': 'not_settled', 'reason_code': 'edition_bands_failed',
                'selected_count': len(receipt['entries']), 'shortfalls': receipt['shortfalls'],
                'failed_bands': failed_bands}
    _require(receipt['profile_status'] in ('settled', 'settled_empty'))
    _require(0 < len(receipt['entries']) <= limits.max_entries)
    envelope = {'schema_version': 1, 'kind': 'owned_private_discovery',
                'owner_user_id': secret_config.owner_user_id, 'edition_id': edition_id,
                'profile_fingerprint': profile_fingerprint,
                'code_revision': code_revision, 'materialized_at': _stamp(now),
                'receipt': receipt, 'cards': _source_cards(receipt)}
    payload_text = canonical_json(envelope)
    _require(len(payload_text.encode('utf-8')) <= limits.max_payload_bytes)
    payload_digest = hashlib.sha256(payload_text.encode('utf-8')).hexdigest()
    try:
        stored = _rpc(client, secret_config, 'finalize_private_discovery',
                      {'p_payload_text': payload_text, 'p_payload_digest': payload_digest})
        _exact(stored, ('schema_version', 'status', 'edition_id', 'payload_digest'))
        _require(type(stored['schema_version']) is int and stored['schema_version'] == 1 and stored['status'] in ('stored', 'already_stored'))
        _require(stored['edition_id'] == edition_id and stored['payload_digest'] == payload_digest)
    except (PrivateDiscoveryError, OSError, TimeoutError):
        # A timeout, conflicting concurrent same-identity attempt, or malformed
        # response is uncertain. Resolve the same identity; never write again.
        existing = _existing_identity(client, secret_config, edition_id, now)
        if existing is not None:
            return _already_stored(existing)
        raise PrivateDiscoveryError('Private discovery settlement could not be verified.') from None
    readback = _existing_identity(client, secret_config, edition_id, now)
    _require(readback is not None and readback['payload_digest'] == payload_digest and
             readback['receipt_digest'] == receipt['receipt_digest'])
    return {'schema_version': 1, 'status': stored['status'], 'edition_id': edition_id,
            'payload_digest': payload_digest, 'selected_count': len(receipt['entries'])}


def validate_discovery_card(card):
    _exact(card, _CARD_FIELDS)
    _require(isinstance(card['story_id'], str) and bool(_STORY.fullmatch(card['story_id'])))
    _require(_bounded(card['title'], 2000, 8000) and bool(card['title']) and _bounded(card['summary'], 8000, 32000))
    _require(card['source_kind'] == 'outlet' and card['language'] in ('en', 'zh') and _safe_url(card['canonical_url'], allow_empty=False))
    _require(_timestamp(card['published_at']) and _timestamp(card['saved_at'], nullable=True) and _timestamp(card['read_at'], nullable=True))
    _require(type(card['publication_seq']) is int and card['publication_seq'] == 0 and type(card['position']) is int and card['position'] == 0)
    _require(_integer(card['state_revision']) and card['topic_ranks'] == {} and card['page_order_mode'] == 'discovery' and card['next_cursor'] is None)
    topics = card['topic_ids']
    _require(isinstance(topics, list) and len(topics) <= 20 and all(isinstance(t, str) and _TOPIC.fullmatch(t) for t in topics) and len(topics) == len(set(topics)))
    _require(card['ordering_mode'] == 'weighted_total' and isinstance(card['ordering_key'], dict))
    _exact(card['score_components'], (*discovery.COMPONENTS, 'final_score'))
    scores = card['score_components']
    _require(all(type(v) in (int, float) and math.isfinite(v) for v in scores.values()))
    _require(all(0 <= scores[name] <= 1 for name in discovery.COMPONENTS))
    _require(math.isclose(scores['final_score'], sum(scores[n] for n in discovery.COMPONENTS[:6]) - sum(scores[n] for n in discovery.COMPONENTS[6:]), abs_tol=1e-12))
    _require(_bounded(card['source_name'], 200, 1000) and bool(card['source_name']) and _bounded(card['ranking_explanation'], 2000, 8000) and bool(card['ranking_explanation']))
    mentions = card['coverage_mentions']
    _require(isinstance(mentions, list) and len(mentions) <= 20 and len(canonical_json(mentions).encode()) <= 32768)
    for row in mentions:
        _exact(row, ('headline', 'mentioned_at', 'source_id', 'source_kind', 'source_name', 'url'))
        _require(row['source_kind'] == 'outlet' and _timestamp(row['mentioned_at']) and _safe_url(row['url'], allow_empty=False))
        for name, chars, size in [('headline', 2000, 8000), ('source_id', 160, 512), ('source_name', 200, 1000)]:
            _require(_bounded(row[name], chars, size) and bool(row[name]))
    interests = card['interests']
    _require(isinstance(interests, list) and len(interests) <= 20)
    for row in interests:
        _exact(row, ('topic_id', 'signal', 'revision'))
        _require(isinstance(row['topic_id'], str) and bool(_TOPIC.fullmatch(row['topic_id'])) and row['signal'] in ('more_like', 'less_like') and _integer(row['revision']))
    _require(len(canonical_json(card).encode()) <= 65536)
    return card


def validate_discovery_response(value, *, limits: DiscoveryLimits | None = None):
    limits = limits or DiscoveryLimits()
    _require(len(canonical_json(value).encode('utf-8')) <= limits.max_response_bytes)
    _exact(value, ('schema_version', 'status', 'reason_code', 'edition'))
    _require(type(value['schema_version']) is int and value['schema_version'] == 1)
    if value['status'] == 'unavailable':
        _require(value['reason_code'] in ('no_private_edition', 'edition_unavailable') and value['edition'] is None)
        return value
    _require(value['status'] == 'ready' and value['reason_code'] == '')
    edition = value['edition']
    _exact(edition, ('edition_id', 'generated_at', 'code_revision', 'policy_revision', 'policy_digest', 'snapshot_digest',
                     'profile_revision', 'receipt_digest', 'stale', 'disclosures', 'shortfalls', 'entries'))
    _require(isinstance(edition['edition_id'], str) and bool(_EDITION.fullmatch(edition['edition_id'])))
    _require(_timestamp(edition['generated_at']) and isinstance(edition['code_revision'], str) and bool(_GIT.fullmatch(edition['code_revision'])))
    _require(_integer(edition['policy_revision'], minimum=1) and _integer(edition['profile_revision']))
    _require(all(_sha(edition[k]) for k in ('policy_digest', 'snapshot_digest', 'receipt_digest')) and type(edition['stale']) is bool)
    _require(isinstance(edition['disclosures'], list) and len(edition['disclosures']) <= 100 and all(_bounded(s, 2000, 8000) and s for s in edition['disclosures']))
    _exact(edition['shortfalls'], discovery.LANES)
    _require(all(_integer(n) for n in edition['shortfalls'].values()))
    entries = edition['entries']
    _require(isinstance(entries, list) and 0 < len(entries) <= limits.max_entries)
    ids = set()
    for position, entry in enumerate(entries, 1):
        _exact(entry, ('position', 'primary_lane', 'reason', 'secondary_reasons', 'card'))
        _require(type(entry['position']) is int and entry['position'] == position and entry['primary_lane'] in discovery.LANES)
        _require(_bounded(entry['reason'], 2000, 8000) and bool(entry['reason']))
        secondary = entry['secondary_reasons']
        _require(isinstance(secondary, list) and len(secondary) <= 3)
        seen = {entry['primary_lane']}
        for row in secondary:
            _exact(row, ('lane', 'reason'))
            _require(row['lane'] in discovery.LANES and row['lane'] not in seen and _bounded(row['reason'], 2000, 8000) and bool(row['reason']))
            seen.add(row['lane'])
        card = validate_discovery_card(entry['card'])
        _require(card['story_id'] not in ids and card['ranking_explanation'] == entry['reason'])
        ids.add(card['story_id'])
    return value


class DiscoveryClient:
    def __init__(self, config: AuthConfig, *, transport=None, limits: DiscoveryLimits | None = None):
        self.config = config
        self.limits = limits or DiscoveryLimits()
        self.transport = transport or DiscoveryTransport(self.limits)

    def read(self, session: Session, *, edition_id=None):
        _require(edition_id is None or isinstance(edition_id, str) and bool(_EDITION.fullmatch(edition_id)))
        try:
            status, payload = self.transport.request('POST', self.config.supabase_url + '/rest/v1/rpc/discovery_edition',
                headers={'apikey': self.config.publishable_key, 'authorization': 'Bearer ' + session.access_token,
                         'accept': 'application/json', 'content-type': 'application/json', 'cache-control': 'no-store'},
                body={'p_edition_id': edition_id})
        except (AuthError, OSError, TimeoutError):
            raise PrivateDiscoveryError('Private discovery could not be read.') from None
        _require(status == 200)
        result = validate_discovery_response(payload, limits=self.limits)
        if edition_id is not None and result['status'] == 'ready':
            _require(result['edition']['edition_id'] == edition_id)
        return result


def select_entries(response, *, lane='updates', topic=None):
    _require(lane in discovery.LANES)
    _require(topic is None or isinstance(topic, str) and bool(_TOPIC.fullmatch(topic)))
    if response['status'] != 'ready':
        return []
    return [row for row in response['edition']['entries']
            if row['primary_lane'] == lane and (topic is None or topic in row['card']['topic_ids'])]


def write_private_json(path: Path, value):
    """Atomic explicit export, mode600 from creation through final replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
