"""Pure M2 discovery and replay for an explicitly local, private preview.

The record is not an owned frozen Slate and is never publication authorization.
Source snapshots supply originals before topic caps. Exact URL evidence alone
supports coverage; duplicate publisher routes share one independence bucket.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import yaml

from .filter import topic_match
from .dedup import same_story
from .models import Item
from .source_snapshot import snapshot_config_digest
from .identity import story_id_for_item
from .normalize import canonical_url, clean_title
from .personalization.ranking import EMPTY_NEWSLETTER_INPUT_DIGEST, ranking_config_digest

LANES = ('updates', 'hot', 'interested', 'surprise')
COMPONENTS = ('relevance', 'freshness', 'trend', 'editor_consensus',
              'deliberate_surprise', 'diversity', 'repetition_penalty', 'source_fatigue_penalty')
BANDS = ('relevance', 'freshness', 'trend', 'deliberate_surprise',
         'source_diversity', 'topic_diversity', 'repetition')


class DiscoveryError(ValueError):
    """Invalid input or unverifiable discovery receipt."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _keys(value, names):
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise DiscoveryError('discovery_schema')


def _number(value, *, low=0, high=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiscoveryError('discovery_number')
    if integer and not isinstance(value, int):
        raise DiscoveryError('discovery_integer')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < low or (high is not None and value > high):
        raise DiscoveryError('discovery_number_range')
    return value


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise DiscoveryError('discovery_timestamp') from None


def _stamp(value):
    return _time(value).isoformat().replace('+00:00', 'Z')


def _validate_discovery_policy(value: Mapping) -> dict:
    """Reject unknown/missing keys and ambiguous numeric policy values."""
    _keys(value, ('revision', 'policy_id', 'size', 'lane_priority', 'lane_quotas', 'windows',
                  'gates', 'constraints', 'components', 'bands', 'band_exceptions', 'disclosures'))
    p = deepcopy(dict(value))
    _number(p['revision'], low=1, integer=True)
    if not isinstance(p['policy_id'], str) or not p['policy_id'].strip():
        raise DiscoveryError('discovery_policy_id')
    _number(p['size'], low=1, high=1000, integer=True)
    if not isinstance(p['lane_priority'], list) or len(p['lane_priority']) != 4 or set(p['lane_priority']) != set(LANES):
        raise DiscoveryError('discovery_priority')
    _keys(p['lane_quotas'], LANES)
    for value in p['lane_quotas'].values():
        _number(value, integer=True, high=1000)
    if sum(p['lane_quotas'].values()) != p['size']:
        raise DiscoveryError('discovery_quota_size')
    _keys(p['windows'], (*LANES, 'repetition', 'source_fatigue'))
    for value in p['windows'].values():
        _number(value, low=0.001, high=8760)
    _keys(p['gates'], ('hot_min_sources', 'surprise_min_sources', 'min_source_weight',
                       'interest_threshold', 'freshness_half_life_hours', 'cold_start',
                       'cold_start_excluded_topic_ids'))
    for name in ('hot_min_sources', 'surprise_min_sources'):
        _number(p['gates'][name], low=2, high=1000, integer=True)
    _number(p['gates']['min_source_weight'])
    _number(p['gates']['interest_threshold'], low=0.001, high=1)
    _number(p['gates']['freshness_half_life_hours'], low=0.001)
    if p['gates']['cold_start'] not in ('unavailable', 'topic_config_match'):
        raise DiscoveryError('discovery_cold_start')
    excluded_topics = p['gates']['cold_start_excluded_topic_ids']
    if (not isinstance(excluded_topics, list)
            or not all(isinstance(value, str) and value.strip() == value and value for value in excluded_topics)
            or len(set(excluded_topics)) != len(excluded_topics)):
        raise DiscoveryError('discovery_cold_start_excluded_topics')
    _keys(p['constraints'], ('max_per_source', 'max_per_aggregator', 'max_per_topic',
                            'max_per_source_window', 'max_appearances'))
    for value in p['constraints'].values():
        _number(value, low=1, high=10000, integer=True)
    _keys(p['components'], COMPONENTS)
    for part in p['components'].values():
        _keys(part, ('enabled', 'weight', 'cap'))
        if type(part['enabled']) is not bool:
            raise DiscoveryError('discovery_enabled')
        _number(part['weight'], high=1)
        _number(part['cap'], high=1)
    if not math.isclose(sum(p['components'][name]['weight'] for name in COMPONENTS[:6]), 1.0):
        raise DiscoveryError('discovery_weights')
    if p['components']['editor_consensus']['enabled']:
        raise DiscoveryError('discovery_newsletter_input_unavailable')
    _keys(p['bands'], BANDS)
    if not isinstance(p['band_exceptions'], dict) or not set(p['band_exceptions']) <= set(BANDS):
        raise DiscoveryError('discovery_band_exceptions')
    for name, band in p['bands'].items():
        _keys(band, ('active', 'floor', 'cap', 'min_distinct'))
        if type(band['active']) is not bool:
            raise DiscoveryError('discovery_band_active')
        _number(band['floor'], high=1)
        _number(band['cap'], low=band['floor'], high=1)
        _number(band['min_distinct'], integer=True, high=1000)
        reason = p['band_exceptions'].get(name)
        if (not band['active'] and (not isinstance(reason, str) or not reason.strip())) or (band['active'] and reason is not None):
            raise DiscoveryError('discovery_band_exception_required')
        if name not in ('source_diversity', 'topic_diversity') and band['min_distinct']:
            raise DiscoveryError('discovery_band_distinct')
    if not isinstance(p['disclosures'], list) or not all(isinstance(x, str) and x for x in p['disclosures']):
        raise DiscoveryError('discovery_disclosures')
    return p


def validate_discovery_policy(value: Mapping) -> dict:
    try:
        return _validate_discovery_policy(value)
    except DiscoveryError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise DiscoveryError('discovery_policy_schema') from exc


def load_discovery_policy(path: Path) -> dict:
    try:
        return validate_discovery_policy(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError) as exc:
        raise DiscoveryError('discovery_policy_unreadable') from exc


def _publisher(item):
    host = (urlsplit(canonical_url(item.canonical_url or item.url) or '').hostname or '').lower()
    return host.removeprefix('www.')


def _source(item):
    return ('platform:' + item.platform.strip().casefold()) if item.is_aggregator else ('publisher:' + _publisher(item))


def extract_observations(snapshot, language='en') -> list[dict]:
    """Public original source facts. No merged/fuzzy echoes are counted."""
    observed = _stamp(snapshot.generated_at)
    rows = {}
    for result in snapshot.results:
        for item in result.items:
            if item.language != language or item.is_newsletter:
                continue
            url = canonical_url(item.canonical_url or item.url)
            if not url or not _publisher(item):
                continue
            row = {'story_id': story_id_for_item(item), 'url': url,
                   'source_id': item.source_id, 'source_name': item.source_name,
                   'independent_source': _source(item), 'publisher': _publisher(item),
                   'is_aggregator': item.is_aggregator, 'echo_eligible': item.echo_eligible,
                   'time_is_estimated': item.time_is_estimated,
                   'published_at': _stamp(item.published_at), 'observed_at': observed,
                   'title': clean_title(item.title), 'description': clean_title(item.description),
                   'source_weight': _number(item.source_weight), 'language': item.language}
            # Blank aggregator platform cannot prove independence.
            row['eligible_source'] = item.echo_eligible and (not item.is_aggregator or bool(item.platform.strip()))
            row['text_digest'] = _digest([row['title'], row['description']])
            row['evidence_id'] = 'evidence:' + _digest(row)
            rows[row['evidence_id']] = row
    return sorted(rows.values(), key=lambda x: x['evidence_id'])


def _coverage(rows, now, hours):
    by_source = {}
    for row in rows:
        age = (now - _time(row['published_at'])).total_seconds() / 3600
        if row['eligible_source'] and not row['time_is_estimated'] and _time(row['published_at']) <= _time(row['observed_at']) and 0 <= age <= hours:
            key = row['independent_source']
            # First actual mention in each independent bucket avoids duplicated
            # routes falsely creating later movement within that same source.
            if key not in by_source or _time(row['published_at']) < _time(by_source[key]['published_at']):
                by_source[key] = row
    return sorted(by_source.values(), key=lambda x: (_time(x['published_at']), x['independent_source']))


def _updated(rows, before, observed_at, now, hours):
    if before is None or not 0 <= (now - observed_at).total_seconds()/3600 <= hours:
        return []
    old = defaultdict(list)
    new = defaultdict(list)
    for target, observations in ((old, before), (new, rows)):
        for row in observations:
            if not row['is_aggregator'] and not row['time_is_estimated'] and _time(row['published_at']) <= min(now, _time(row['observed_at'])):
                target[(row['url'], row['publisher'], row['source_id'])].append(row)
    changes = []
    for key in old.keys() & new.keys():
        older, current = old[key], new[key]
        # Conflicting feed texts at the same observed stage are ambiguous.
        if len({r['text_digest'] for r in older}) != 1 or len({r['text_digest'] for r in current}) != 1:
            continue
        a, b = older[0], current[0]
        prior_age = (now - _time(a['observed_at'])).total_seconds()/3600
        if (a['text_digest'] != b['text_digest'] and _time(a['observed_at']) < _time(b['observed_at'])
                and 0 <= prior_age <= hours):
            changes.append({'publisher': key[1], 'url': key[0], 'before': a, 'after': b})
    return sorted(changes, key=lambda x: (x['url'], x['publisher']))


def _score(raw, policy):
    result = {}
    for name in COMPONENTS:
        rule = policy['components'][name]
        value = raw[name]
        # None is retained separately in raw_components and disclosures. Its
        # score contribution is zero, not evidence of a measured zero penalty.
        if value is not None:
            _number(value, high=1)
        result[name] = 0.0 if value is None or not rule['enabled'] else min(value, rule['cap']) * rule['weight']
    result['final_score'] = sum(result[n] for n in COMPONENTS[:6]) - sum(result[n] for n in COMPONENTS[6:])
    return result


def _selection_reason(row, constraints, counts, topics, display_groups, *, source_diversity_cap=None,
                      topic_diversity_cap=None):
    source = row['independent_source']
    limit = constraints['max_per_aggregator'] if row['is_aggregator'] else constraints['max_per_source']
    if row['display_cluster_id'] in display_groups:
        return 'duplicate_display_cluster'
    if counts[source] >= limit:
        return 'source_cap'
    if source_diversity_cap is not None and counts[source] >= source_diversity_cap:
        return 'source_diversity_cap'
    if any(topics[t] >= constraints['max_per_topic'] for t in row['topic_ids']):
        return 'topic_cap'
    if topic_diversity_cap is not None and any(topics[t] >= topic_diversity_cap for t in row['topic_ids']):
        return 'topic_diversity_cap'
    if row['history_source_count'] is not None and row['history_source_count'] + counts[source] >= constraints['max_per_source_window']:
        return 'source_window_cap'
    if row['history_story_count'] is not None and row['history_story_count'] >= constraints['max_appearances'] and 'updates' not in row['lane_scores']:
        return 'repetition_cap'
    return ''


def _select(candidates, policy, *, source_diversity_cap=None, topic_diversity_cap=None):
    counts, topics, display_groups = Counter(), Counter(), set()
    selected, shortfalls, rejected = [], {}, []
    for lane in policy['lane_priority']:
        pool = sorted((r for r in candidates if r['primary_lane'] == lane),
                      key=lambda r: (-r['components']['final_score'], r['story_id']))
        filled, skipped = 0, False
        for row in pool:
            if filled >= policy['lane_quotas'][lane]:
                break
            constraints = policy['constraints']
            reason = _selection_reason(
                row, constraints, counts, topics, display_groups,
                source_diversity_cap=source_diversity_cap, topic_diversity_cap=topic_diversity_cap)
            if reason:
                rejected.append({'story_id': row['story_id'], 'reason': reason})
                skipped = True
                continue
            selected.append({**row, 'position': len(selected) + 1, 'backfilled': skipped})
            display_groups.add(row['display_cluster_id'])
            counts[row['independent_source']] += 1
            topics.update(row['topic_ids'])
            filled += 1
        shortfalls[lane] = policy['lane_quotas'][lane] - filled
    return selected, shortfalls, rejected


def _backfill_distinct(entries, candidates, policy, rejected, bands, *, source_diversity_cap, topic_diversity_cap):
    """Replace one lowest-ranked duplicate with an eligible new identity."""
    constraints = policy['constraints']
    selected_ids = {row['story_id'] for row in entries}
    counts = Counter(row['independent_source'] for row in entries)
    topics = Counter(topic for row in entries for topic in row['topic_ids'])
    display_groups = {row['display_cluster_id'] for row in entries}
    for band_name, reason in (('source_diversity', 'source_diversity_distinct'),
                              ('topic_diversity', 'topic_diversity_distinct')):
        band = next(item for item in bands if item['band'] == band_name)
        if band['verdict'] != 'FAIL' or band['distinct'] >= band['min_distinct']:
            continue
        targets = sorted(entries, key=lambda row: (row['components']['final_score'], row['story_id']))
        for target in targets:
            duplicate = counts[target['independent_source']] > 1 if band_name == 'source_diversity' else any(
                topics[topic] > 1 for topic in target['topic_ids'])
            if not duplicate:
                continue
            reduced_counts, reduced_topics = counts.copy(), topics.copy()
            reduced_groups = display_groups - {target['display_cluster_id']}
            reduced_counts[target['independent_source']] -= 1
            reduced_topics.subtract(target['topic_ids'])
            pool = sorted((row for row in candidates if row['primary_lane'] == target['primary_lane']
                           and row['story_id'] not in selected_ids),
                          key=lambda row: (-row['components']['final_score'], row['story_id']))
            for replacement in pool:
                if band_name == 'source_diversity':
                    prospective_distinct = len({source for source, count in reduced_counts.items() if count > 0}
                                               | {replacement['independent_source']})
                else:
                    prospective_distinct = len({topic for topic, count in reduced_topics.items() if count > 0}
                                               | set(replacement['topic_ids']))
                improves = prospective_distinct > band['distinct']
                if not improves or _selection_reason(
                        replacement, constraints, reduced_counts, reduced_topics, reduced_groups,
                        source_diversity_cap=source_diversity_cap,
                        topic_diversity_cap=topic_diversity_cap):
                    continue
                replacement = {**replacement, 'position': target['position'], 'backfilled': True}
                replaced = [replacement if row['story_id'] == target['story_id'] else row for row in entries]
                kept_rejections = [item for item in rejected if item['story_id'] != replacement['story_id']]
                return replaced, [*kept_rejections, {'story_id': target['story_id'], 'reason': reason}], True
    return entries, rejected, False


def _final_diversity_caps(entries, bands):
    """Return stricter per-source/topic caps required by failed final bands."""
    caps = {}
    for band_name, cap_name in (('source_diversity', 'source'), ('topic_diversity', 'topic')):
        band = next(item for item in bands if item['band'] == band_name)
        if (band['verdict'] == 'FAIL' and band['achieved'] is not None
                and band['achieved'] > band['cap']):
            cap = math.floor(len(entries) * band['cap'])
            if cap > 0 or (cap_name == 'topic' and band['min_distinct'] == 0):
                caps[cap_name] = cap
    return caps


def _select_with_final_band_backfill(candidates, policy, history_available):
    """Backfill final share-band rejections from each story's primary lane."""
    entries, shortfalls, rejected = _select(candidates, policy)
    bands, verdict = _bands(entries, policy, history_available)
    applied = {'source': None, 'topic': None}
    for _ in range(policy['size']):
        proposed = _final_diversity_caps(entries, bands)
        next_caps = {name: min(value, applied[name]) if applied[name] is not None else value
                     for name, value in proposed.items()}
        if any(applied[name] != value for name, value in next_caps.items()):
            applied.update(next_caps)
            entries, shortfalls, rejected = _select(
                candidates, policy, source_diversity_cap=applied['source'],
                topic_diversity_cap=applied['topic'])
            bands, verdict = _bands(entries, policy, history_available)
        entries, rejected, swapped = _backfill_distinct(
            entries, candidates, policy, rejected, bands,
            source_diversity_cap=applied['source'], topic_diversity_cap=applied['topic'])
        if not swapped and not any(applied[name] != value for name, value in next_caps.items()):
            return entries, shortfalls, rejected, bands, verdict
        bands, verdict = _bands(entries, policy, history_available)
    return entries, shortfalls, rejected, bands, verdict


def _bands(entries, policy, history_available):
    size = len(entries)
    sources = Counter(r['independent_source'] for r in entries)
    topics = Counter(t for r in entries for t in r['topic_ids'])
    achieved = {
        'relevance': sum(r['raw_components']['relevance'] >= policy['gates']['interest_threshold'] for r in entries)/size if size else None,
        'freshness': sum(r['age_hours'] <= policy['windows']['hot'] for r in entries)/size if size else None,
        'trend': sum('hot' in r['lane_scores'] for r in entries)/size if size else None,
        'deliberate_surprise': sum('surprise' in r['lane_scores'] for r in entries)/size if size else None,
        'source_diversity': max(sources.values(), default=0)/size if size else None,
        'topic_diversity': max(topics.values(), default=0)/size if size else None,
        'repetition': sum(bool(r['history_story_count']) for r in entries)/size if size and history_available else None,
    }
    results = []
    for name in BANDS:
        rule, value = policy['bands'][name], achieved[name]
        distinct = len(sources) if name == 'source_diversity' else len(topics) if name == 'topic_diversity' else 0
        verdict = 'DISABLED' if not rule['active'] else 'UNKNOWN' if value is None else 'PASS' if rule['floor'] <= value <= rule['cap'] and distinct >= rule['min_distinct'] else 'FAIL'
        results.append({'band': name, **rule, 'achieved': value, 'distinct': distinct,
                        'verdict': verdict, 'exception_reason': policy['band_exceptions'].get(name, '')})
    return results, 'PASS' if size and all(b['verdict'] in ('PASS', 'DISABLED') for b in results) else 'FAIL'


def _derive(observations, earlier, topic_matches, p, profile_input, history_rows, now, observed_at, display_dedup):
    """The same deterministic derivation is used at build and replay."""
    grouped = defaultdict(list)
    for row in observations:
        grouped[row['story_id']].append(row)
    prepared = []
    profile_available = profile_input is not None and profile_input['interest_count'] > 0
    for story_id, rows in sorted(grouped.items()):
        valid = [r for r in rows if not r['time_is_estimated'] and _time(r['published_at']) <= min(now, _time(r['observed_at']))]
        if not valid:
            continue
        representative = min(valid, key=lambda r: (r['is_aggregator'], -r['source_weight'], r['source_id'], r['evidence_id']))
        age = (now - _time(representative['published_at'])).total_seconds()/3600
        topics = topic_matches[story_id]
        subject_topics = topics if profile_available else [
            topic for topic in topics if topic not in p['gates']['cold_start_excluded_topic_ids']
        ]
        affinity = profile_input['scores'].get(story_id, 0.0) if profile_available else float(bool(subject_topics)) if p['gates']['cold_start'] == 'topic_config_match' else 0.0
        count_story = None if history_rows is None else sum(r['story_id'] == story_id and (now-_time(r['shown_at'])).total_seconds()/3600 <= p['windows']['repetition'] for r in history_rows)
        count_novelty = None if history_rows is None else sum(r['story_id'] == story_id for r in history_rows)
        count_source = None if history_rows is None else sum(r['source_id'] == representative['independent_source'] and (now-_time(r['shown_at'])).total_seconds()/3600 <= p['windows']['source_fatigue'] for r in history_rows)
        hot = _coverage(rows, now, p['windows']['hot'])
        broad = _coverage(rows, now, p['windows']['surprise'])
        changed = _updated(rows, earlier, observed_at, now, p['windows']['updates'])
        lanes, reasons = {}, {}
        if changed:
            lanes['updates'] = 1.0
            reasons['updates'] = 'Publisher text changed since the previous bound observation.'
        if len(hot) >= p['gates']['hot_min_sources'] and _time(hot[-1]['published_at']) > _time(hot[0]['published_at']):
            lanes['hot'] = min(1.0, len(hot)/p['gates']['hot_min_sources'])
            reasons['hot'] = f"Exact-URL coverage on {len(hot)} independent sources within {p['windows']['hot']} hours, with distinct publication times. This measures coverage reach, not audience popularity."
        if affinity >= p['gates']['interest_threshold'] and age <= p['windows']['interested']:
            lanes['interested'] = affinity
            reasons['interested'] = 'Matches the supplied saved-interest profile.' if profile_available else 'Matches shared configured subject topics; no personal profile is available.'
        outside = affinity < p['gates']['interest_threshold'] if profile_available else p['gates']['cold_start'] == 'topic_config_match' and not subject_topics
        quality = any(not r['is_aggregator'] and r['eligible_source'] and r['source_weight'] >= p['gates']['min_source_weight'] and not r['time_is_estimated'] and _time(r['published_at']) <= _time(r['observed_at']) and 0 <= (now-_time(r['published_at'])).total_seconds()/3600 <= p['windows']['surprise'] for r in rows)
        if outside and quality and age <= p['windows']['surprise'] and count_novelty == 0 and len(broad) >= p['gates']['surprise_min_sources']:
            lanes['surprise'] = 1.0 - affinity
            reasons['surprise'] = ('Outside strong saved-interest matches' if profile_available else 'Outside shared configured subject topics') + f"; qualified publisher evidence and {len(broad)} independent sources within {p['windows']['surprise']} hours provide an importance proxy. Not present in the supplied edition history."
        if not lanes:
            continue
        primary = next(lane for lane in p['lane_priority'] if lane in lanes)
        raw = {'relevance': affinity, 'freshness': 0.5 ** (max(0, age)/p['gates']['freshness_half_life_hours']),
               'trend': lanes.get('hot', 0.0), 'editor_consensus': None,
               'deliberate_surprise': lanes.get('surprise', 0.0), 'diversity': 0.0,
               'repetition_penalty': None if count_story is None else min(1.0, count_story/p['constraints']['max_appearances']),
               'source_fatigue_penalty': None if count_source is None else min(1.0, count_source/p['constraints']['max_per_source_window'])}
        prepared.append({k: representative[k] for k in ('story_id', 'title', 'description', 'url', 'source_id', 'source_name', 'independent_source', 'is_aggregator', 'language', 'published_at')} | {
            'topic_ids': topics, 'age_hours': age, 'primary_lane': primary, 'secondary_lanes': [x for x in p['lane_priority'] if x in lanes and x != primary],
            'lane_scores': lanes, 'lane_reasons': reasons, 'raw_components': raw,
            'history_story_count': count_story, 'history_source_count': count_source, 'history_novelty_count': count_novelty,
            'evidence': {'observations': rows, 'changes': changed, 'hot': hot, 'importance': broad},
            'plain_reason': reasons[primary]})
    # Fuzzy title similarity is a display-only collapse. Its alias never
    # contributes another publisher to exact-URL coverage or Updates evidence.
    display_representatives = []
    threshold = display_dedup['threshold']
    bucket_hours = display_dedup['time_bucket_hours']
    for row in prepared:
        item = Item(title=row['title'], url=row['url'], canonical_url=row['url'],
                    source_id=row['source_id'], source_name=row['source_name'],
                    published_at=_time(row['published_at']), language=row['language'])
        match = next((key for key, old in display_representatives
                      if abs((item.published_at-old.published_at).total_seconds())/3600 <= bucket_hours
                      and same_story(old, item, threshold)), None)
        row['display_cluster_id'] = match or row['story_id']
        if match is None:
            display_representatives.append((row['story_id'], item))
    source_frequency = Counter(r['independent_source'] for r in prepared)
    topic_frequency = Counter(t for r in prepared for t in r['topic_ids'])
    for row in prepared:
        source_rarity = 1/source_frequency[row['independent_source']]
        topic_rarity = sum(1/topic_frequency[t] for t in row['topic_ids'])/len(row['topic_ids']) if row['topic_ids'] else 0
        row['raw_components']['diversity'] = (source_rarity + topic_rarity)/2
        row['components'] = _score(row['raw_components'], p)
    entries, shortfalls, rejected, bands, verdict = _select_with_final_band_backfill(
        prepared, p, history_rows is not None)
    return prepared, entries, shortfalls, rejected, bands, verdict


def build_discovery(cfg, current_snapshot, policy: Mapping, *, previous_snapshot=None,
                    interest_artifact=None, history: Sequence[Mapping] | None = None,
                    now: datetime | None = None, language='en', first_edition: bool | None = None,
                    ranking_interpretation_mode='literal-v1') -> dict:
    """Build independent lanes then merge once, score, constrain and verify.

    ``history=None`` means unavailable. ``[]`` declares this privacy scope has
    no prior local editions. History rows have story_id, source_id (the receipt's
    independent_source key), shown_at (aware ISO timestamp). Never pass another
    user's history or profile; authentication/ownership is the caller's job.
    """
    p = validate_discovery_policy(policy)
    if current_snapshot.configuration_digest != snapshot_config_digest(cfg):
        raise DiscoveryError('discovery_configuration_binding')
    now = _time(now or current_snapshot.generated_at)
    if language not in ('en', 'zh') or _time(current_snapshot.generated_at) > now:
        raise DiscoveryError('discovery_snapshot_time_or_language')
    if previous_snapshot is not None:
        if previous_snapshot.configuration_digest != current_snapshot.configuration_digest:
            raise DiscoveryError('discovery_previous_configuration')
        if _time(previous_snapshot.generated_at) >= _time(current_snapshot.generated_at):
            raise DiscoveryError('discovery_previous_order')
    observations = extract_observations(current_snapshot, language)
    earlier = extract_observations(previous_snapshot, language) if previous_snapshot is not None else None
    originals = [i for result in current_snapshot.results for i in result.items if i.language == language and not i.is_newsletter]
    allowed = {story_id_for_item(i) for result in current_snapshot.results for i in result.items if not i.is_newsletter}
    if interest_artifact is not None:
        a = interest_artifact
        if a.source_snapshot_digest != current_snapshot.content_digest or a.configuration_digest != ranking_config_digest(cfg, interpretation_mode=ranking_interpretation_mode) or a.newsletter_input_digest != EMPTY_NEWSLETTER_INPUT_DIGEST:
            raise DiscoveryError('discovery_interest_binding')
        _number(a.preference_revision, integer=True)
        _number(a.interest_count, integer=True)
        if _time(a.generated_at) > now or set(a.scores) - allowed or a.matched_story_count != len(a.scores) or (not a.interest_count and a.scores):
            raise DiscoveryError('discovery_interest_schema')
        for score in a.scores.values():
            _number(score, low=0.000000001, high=1)
    if first_edition is not None and (type(first_edition) is not bool or history is None or (first_edition and history)):
        raise DiscoveryError('discovery_history_baseline')
    history_first = (not history if first_edition is None else first_edition) if history is not None else None
    history_rows = None if history is None else []
    if history is not None:
        for row in history:
            _keys(row, ('story_id', 'source_id', 'shown_at'))
            if not all(isinstance(row[k], str) and row[k] for k in row):
                raise DiscoveryError('discovery_history_schema')
            if _time(row['shown_at']) > now:
                raise DiscoveryError('discovery_history_future')
            history_rows.append(dict(row))
    item_groups = defaultdict(list)
    for item in originals:
        item_groups[story_id_for_item(item)].append(item)
    topic_matches = {story_id: sorted({category.id for category in cfg.categories
                     for item in items if topic_match(item, category) is not None})
                     for story_id, items in sorted(item_groups.items())}
    threshold = cfg.dedup.get('title_similarity_threshold', 0.90)
    bucket_hours = cfg.dedup.get('time_bucket_hours', 36.0)
    display_dedup = {'threshold': threshold, 'time_bucket_hours': bucket_hours}
    profile_input = asdict(interest_artifact) if interest_artifact else None
    profile_available = profile_input is not None and profile_input['interest_count'] > 0
    prepared, entries, shortfalls, rejected, bands, verdict = _derive(
        observations, earlier, topic_matches, p, profile_input, history_rows,
        now, _time(current_snapshot.generated_at), display_dedup)
    bindings = {'snapshot_digest': current_snapshot.content_digest,
                'evaluation_clock': _stamp(now),
                'observations_digest': _digest(observations),
                'observation_clock': _stamp(current_snapshot.generated_at),
                'previous_observations_digest': _digest(earlier) if earlier is not None else None,
                'previous_observation_clock': _stamp(previous_snapshot.generated_at) if previous_snapshot else None,
                'topic_matches_digest': _digest(topic_matches),
                'configuration_digest': current_snapshot.configuration_digest,
                'ranking_configuration_digest': ranking_config_digest(cfg, interpretation_mode=ranking_interpretation_mode),
                'display_dedup_digest': _digest({'threshold': threshold, 'time_bucket_hours': bucket_hours}),
                'previous_snapshot_digest': previous_snapshot.content_digest if previous_snapshot else None,
                'profile_digest': _digest(asdict(interest_artifact)) if interest_artifact else None,
                'profile_revision': interest_artifact.preference_revision if interest_artifact else None,
                'history_digest': _digest(history_rows) if history_rows is not None else None,
                'history_first_edition': history_first,
                'policy_digest': _digest(p), 'code_digest': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    result = {'schema_version': 2, 'artifact_scope': 'local_private_preview', 'generated_at': _stamp(now),
              'language': language, 'profile_available': profile_available,
              'profile_status': 'missing' if interest_artifact is None else 'settled' if profile_available else 'settled_empty', 'history_available': history_rows is not None,
              'history_baseline': 'unavailable' if history_rows is None else 'first_local_edition' if history_first else 'supplied_history',
              'updates_baseline': 'available' if previous_snapshot else 'insufficient_previous_observation',
              'policy': p, 'bindings': bindings, 'profile_input': asdict(interest_artifact) if interest_artifact else None,
              'history_input': history_rows, 'previous_observations': earlier, 'topic_matches': topic_matches, 'display_dedup': {'threshold': threshold, 'time_bucket_hours': bucket_hours}, 'observations': observations, 'candidates': prepared, 'entries': entries,
              'rejected': rejected, 'shortfalls': shortfalls, 'bands': bands, 'verdict': verdict,
              'publishable': False, 'disclosures': list(p['disclosures'])}
    result['receipt_digest'] = _digest(result)
    return result


def replay_discovery(receipt: Mapping, *, expected_bindings: Mapping | None = None) -> bool:
    """Rerun all lane generators and selection from the bound receipt inputs.

    A checksum is integrity, not authenticity. Callers should supply trusted
    expected bindings when replaying a stored artifact across trust boundaries.
    """
    try:
        data = deepcopy(dict(receipt))
        checksum = data.pop('receipt_digest')
        if checksum != _digest(data):
            raise DiscoveryError('discovery_receipt_digest')
        p = validate_discovery_policy(data['policy'])
        bindings = data['bindings']
        if bindings['policy_digest'] != _digest(p) or bindings['code_digest'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise DiscoveryError('discovery_receipt_binding')
        if expected_bindings is not None and any(bindings.get(k) != v for k, v in expected_bindings.items()):
            raise DiscoveryError('discovery_expected_binding')
        if bindings['observations_digest'] != _digest(data['observations']):
            raise DiscoveryError('discovery_observations_binding')
        for key, input_name in [('profile_digest', 'profile_input'), ('history_digest', 'history_input')]:
            expected = _digest(data[input_name]) if data[input_name] is not None else None
            if bindings[key] != expected:
                raise DiscoveryError('discovery_private_input_binding')
        if bindings['display_dedup_digest'] != _digest(data['display_dedup']):
            raise DiscoveryError('discovery_display_dedup_binding')
        if data['generated_at'] != bindings['evaluation_clock']:
            raise DiscoveryError('discovery_evaluation_clock')
        now = _time(data['generated_at'])
        observed_at = _time(bindings['observation_clock'])
        if observed_at > now or data['schema_version'] != 2 or data['language'] not in ('en', 'zh'):
            raise DiscoveryError('discovery_clock_or_schema')
        earlier = data['previous_observations']
        if bindings['previous_observations_digest'] != (_digest(earlier) if earlier is not None else None):
            raise DiscoveryError('discovery_previous_observations_binding')
        if (earlier is None) != (bindings['previous_snapshot_digest'] is None):
            raise DiscoveryError('discovery_previous_snapshot_binding')
        previous_clock = bindings['previous_observation_clock']
        if (earlier is None) != (previous_clock is None) or (previous_clock is not None and _time(previous_clock) >= observed_at):
            raise DiscoveryError('discovery_previous_clock')
        for rows, clock in ((data['observations'], bindings['observation_clock']), (earlier, previous_clock)):
            if rows is None:
                continue
            if rows != sorted(rows, key=lambda row: row['evidence_id']) or len({row['evidence_id'] for row in rows}) != len(rows):
                raise DiscoveryError('discovery_observation_order')
            for row in rows:
                fact = {key: value for key, value in row.items() if key != 'evidence_id'}
                if row['evidence_id'] != 'evidence:' + _digest(fact) or row['observed_at'] != clock or row['language'] != data['language']:
                    raise DiscoveryError('discovery_observation_binding')
        profile = data['profile_input']
        profile_available = profile is not None and profile['interest_count'] > 0
        profile_status = 'missing' if profile is None else 'settled' if profile_available else 'settled_empty'
        if (data['profile_available'] is not profile_available or data['profile_status'] != profile_status
                or bindings['profile_revision'] != (profile['preference_revision'] if profile else None)):
            raise DiscoveryError('discovery_profile_status')
        if profile is not None and (profile['source_snapshot_digest'] != bindings['snapshot_digest']
                or profile['configuration_digest'] != bindings['ranking_configuration_digest']
                or profile['newsletter_input_digest'] != EMPTY_NEWSLETTER_INPUT_DIGEST
                or _time(profile['generated_at']) > now):
            raise DiscoveryError('discovery_profile_binding')
        history = data['history_input']
        history_first = bindings['history_first_edition']
        if (history is None and history_first is not None) or (history is not None and type(history_first) is not bool) or (history_first and history):
            raise DiscoveryError('discovery_history_baseline')
        baseline = 'unavailable' if history is None else 'first_local_edition' if history_first else 'supplied_history'
        if data['history_available'] is not (history is not None) or data['history_baseline'] != baseline:
            raise DiscoveryError('discovery_history_status')
        if history is not None:
            for row in history:
                _keys(row, ('story_id', 'source_id', 'shown_at'))
                if _time(row['shown_at']) > now:
                    raise DiscoveryError('discovery_history_future')
        if data['updates_baseline'] != ('available' if earlier is not None else 'insufficient_previous_observation'):
            raise DiscoveryError('discovery_updates_status')
        if bindings['topic_matches_digest'] != _digest(data['topic_matches']):
            raise DiscoveryError('discovery_topic_matches_binding')
        derived = _derive(data['observations'], earlier, data['topic_matches'], p, profile,
                          history, now, observed_at, data['display_dedup'])
        for key, expected in zip(('candidates', 'entries', 'shortfalls', 'rejected', 'bands', 'verdict'), derived):
            if data[key] != expected:
                raise DiscoveryError('discovery_replay_' + key)
        if data['disclosures'] != p['disclosures']:
            raise DiscoveryError('discovery_disclosures_binding')
        if data['publishable'] is not False or data['artifact_scope'] != 'local_private_preview':
            raise DiscoveryError('discovery_publication_scope')
        return True
    except DiscoveryError:
        raise
    except (KeyError, TypeError, ValueError, StopIteration, OverflowError) as exc:
        raise DiscoveryError('discovery_receipt_schema') from exc
