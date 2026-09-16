#!/usr/bin/env python3
"""Build or service-ingest a public-only retained-corpus artifact."""
from __future__ import annotations
import argparse, json, os, sys, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.grouping import GroupingCandidate, GroupingPolicy, exact_matches
from curator.retained_corpus import apply_translations, public_ingest_rows, retain
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest
from curator.recommendation.supabase_http import _NoRedirect, validate_https_origin
from curator.sources import SafeHttpPolicy, SafeHttpTransport
from curator.translation import (
    ModelPairingAdapter,
    ModelTranslationAdapter,
    ModelTranslationConfig,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
)
from curator.translation.base import TranslationPrivacyError, TranslationProviderError
from curator.translation.ingest import IngestTranslationPolicy, translate_exclusive_stories
from curator.translation.pairing import ExclusivityDecision, PairingPolicy, decide_exclusivity
from curator.translation.store import TranslationStoreError


def _ingest_translation_policy(cfg) -> IngestTranslationPolicy:
    """Build the validated policy from config. Boot fails on a bad value."""
    translation, language = cfg.translation or {}, cfg.language or {}
    return IngestTranslationPolicy(
        enabled=bool(translation.get('enabled', False)),
        provider=str(translation.get('provider') or 'google'),
        display_language=str(language.get('default_display') or 'en'),
        run_character_limit=int(translation.get('run_character_limit', 2000)),
        day_character_limit=int(translation.get('day_character_limit', 15000)),
        month_character_limit=int(translation.get('month_character_limit', 450000)),
        daily_cost_limit_usd=float(translation.get('daily_cost_limit_usd', 0.5)),
        input_cost_per_million_tokens_usd=float(translation.get('input_cost_per_million_tokens_usd', 0.25)),
        output_cost_per_million_tokens_usd=float(translation.get('output_cost_per_million_tokens_usd', 2.0)),
        characters_per_token=int(translation.get('characters_per_token', 4)),
        max_output_tokens_per_story=int(translation.get('max_output_tokens_per_story', 1000)),
        cache_ttl_days=int(translation.get('cache_ttl_days', 30)),
        on_failure=str(translation.get('on_failure') or 'show_original_marked'),
        max_items=int(translation.get('max_items_per_language', 25)),
        normalization_version=str(translation.get('normalization_version') or 'normalized-item-v1'),
        glossary_policy_version=str(translation.get('glossary_policy_version') or 'none-v1'),
        candidate_policy_version=str(translation.get('candidate_policy_version') or 'ranked-non-newsletter-v1'),
    )


def _service_headers(key):
    headers = {'apikey': key, 'content-type': 'application/json'}
    if not key.startswith('sb_secret_'):
        headers['authorization'] = 'Bearer ' + key
    return headers


def _rpc(url, key, name, body, *, timeout=30):
    request = urllib.request.Request(url + '/rest/v1/rpc/' + name,
                                     data=json.dumps(body).encode(),
                                     headers=_service_headers(key), method='POST')
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f'{name} failed')
        return json.loads(response.read() or b'null')


def read_exclusivity_decisions(url, key, story_ids):
    """Decisions already on record. A decided story is never re-asked."""
    if not story_ids:
        return {}
    rows = _rpc(url, key, 'm2_read_exclusivity_decisions', {'p_story_ids': list(story_ids)})
    decisions = {}
    for row in rows or ():
        decisions[str(row['story_id'])] = ExclusivityDecision(
            story_id=str(row['story_id']),
            decided_at=datetime.fromisoformat(str(row['decided_at']).replace('Z', '+00:00')),
            model=str(row.get('model') or ''), policy_id=str(row.get('policy_id') or ''),
            match_story_id=row.get('match_story_id'))
    return decisions


def record_exclusivity_decisions(url, key, decisions):
    """Write once. The RPC keeps the first decision on a replay."""
    for decision in decisions:
        _rpc(url, key, 'm2_record_exclusivity_decision', {
            'p_story_id': decision.story_id, 'p_model': decision.model,
            'p_policy_id': decision.policy_id, 'p_match_story_id': decision.match_story_id})


def read_corpus_window(url, key, *, policy, now, pages: int = 40):
    """Read every corpus row inside the PAIRING window, newest first.

    Pairing must see stories ingested by EARLIER runs, not only this batch. The
    read RPC caps at 100 rows per call, so this pages until it leaves the window
    or hits the page cap. Hitting the cap means the window was NOT fully read,
    which the caller treats as a reason to skip paid work, not to guess.
    """
    headers = _service_headers(key)
    horizon = now - timedelta(hours=policy.window_hours)
    collected, before_published, before_story, truncated = [], None, None, False
    for page in range(pages):
        body = json.dumps({'p_category_id': None, 'p_query': None,
                           'p_before_published_at': before_published,
                           'p_before_story_id': before_story, 'p_limit': 100}).encode()
        request = urllib.request.Request(url + '/rest/v1/rpc/m2_retained_candidates',
                                         data=body, headers=headers, method='POST')
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError('retained corpus read failed')
            rows = json.loads(response.read())
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            published = datetime.fromisoformat(str(row['published_at']).replace('Z', '+00:00'))
            if published < horizon:
                return tuple(collected), False
            collected.append(GroupingCandidate(
                story_id=str(row['story_id']), language=str(row['language']),
                title=str(row['title']), summary=str(row.get('summary') or ''),
                published_at=published, canonical_url=str(row.get('canonical_url') or ''),
                category_ids=tuple(row.get('category_ids') or ()),
                event_group_id=row.get('event_group_id')))
        before_published, before_story = rows[-1]['published_at'], rows[-1]['story_id']
        if len(rows) < 100:
            # The corpus itself ran out inside the window: fully read.
            return tuple(collected), False
        truncated = page == pages - 1
    return tuple(collected), truncated


def translate_rows(cfg, rows, *, env, now, store=None, provider=None, corpus=(),
                   pairing_provider=None, decisions=None, truncated=False, on_decision=None):
    """Decide exclusivity with the model, then translate what it ruled exclusive.

    Returns ``(rows, message)``. A missing credential, a switched-off feature, a
    provider failure or an unavailable cache is always a named SKIP with exit 0:
    the hourly ingest must keep running and no story is ever dropped for a
    translation problem. A programmer error is NOT swallowed.
    """
    try:
        policy = _ingest_translation_policy(cfg)
        pairing_policy = PairingPolicy.from_config(cfg.translation or {})
    except ValueError as error:
        return rows, f'translation unavailable: invalid policy ({error})'
    if not policy.enabled:
        return rows, 'translation skipped: sources.yaml translation.enabled is false'
    translation = cfg.translation or {}
    key_env = str(translation.get('api_key_env') or '')
    api_key = env.get(key_env, '') if key_env else ''
    if not api_key:
        # Exit 0, but make it impossible to miss in the Actions log.
        name = key_env or 'translation.api_key_env'
        print(f'::warning::translation skipped: {name} not configured', file=sys.stderr)
        return rows, f'translation skipped: {name} is not set'
    if truncated:
        # Exclusivity is unproven when the window was not fully read, and a
        # wrong exclusivity claim spends money on a story already covered.
        return rows, 'translation skipped: corpus read-back truncated'

    batch = tuple(_pairing_candidate(row) for row in rows)
    try:
        if store is None or provider is None or pairing_provider is None:
            built = _build_translation_clients(translation, api_key, env)
            if built is None:
                return rows, 'translation skipped: store not configured'
            store = store or built[0]
            provider = provider or built[1]
            pairing_provider = pairing_provider or built[2]
    except (TranslationProviderError, TranslationStoreError, ValueError) as error:
        return rows, f'translation unavailable: client not built ({type(error).__name__})'

    prefilter = exact_matches(batch + tuple(corpus), policy=GroupingPolicy.from_config(cfg.grouping or {}))
    try:
        decision = decide_exclusivity(
            batch, corpus, display_language=policy.display_language, policy=pairing_policy,
            provider=pairing_provider, now=now, already_decided=dict(decisions or {}),
            prefilter=prefilter)
    except (TranslationProviderError, TranslationStoreError) as error:
        return rows, f'pairing unavailable: {type(error).__name__}'
    if on_decision is not None:
        for record in decision.decisions.values():
            on_decision(record)
    rows = _apply_group_ids(rows, decision.group_ids)

    exclusive = set(decision.exclusive_story_ids)
    stories = [(row.story_id, row.item) for row in rows if row.story_id in exclusive]
    if not stories:
        return rows, (f'translation skipped: no language-exclusive stories '
                      f'(pairing calls={decision.calls} undecided={len(decision.undecided)})')
    try:
        result = translate_exclusive_stories(
            stories, policy=policy, store=store, provider=provider,
            run_id=f"retained-corpus-{now.strftime('%Y%m%dT%H%M%SZ')}", now=now)
    except (TranslationProviderError, TranslationStoreError, TranslationPrivacyError) as error:
        # Provider and store degradation only. A TypeError here is a code defect
        # and must surface, not hide behind "original text retained".
        return rows, f'translation unavailable: {type(error).__name__}, original text retained'
    translated = result.counters.get('translated', 0) + result.counters.get('cache_hit', 0)
    return (apply_translations(rows, result.overlays),
            f'translated={translated} untranslated_shown={result.untranslated_shown} '
            f'pairing_calls={decision.calls} undecided={len(decision.undecided)}')


def _pairing_candidate(row):
    return GroupingCandidate(
        story_id=row.story_id, language=row.item.language, title=row.item.title,
        summary=row.item.description or '', published_at=row.item.published_at,
        canonical_url=row.item.canonical_url, category_ids=tuple(sorted(row.category_ids)),
        event_group_id=row.event_group_id)


def _apply_group_ids(rows, group_ids):
    """An id a row already carries is authoritative and is never replaced."""
    from dataclasses import replace as _replace
    return tuple(_replace(row, event_group_id=row.event_group_id or group_ids[row.story_id])
                 if not row.event_group_id and row.story_id in group_ids else row
                 for row in rows)


def _build_translation_clients(translation, api_key, env):
    url = env.get(str(translation.get('supabase_url_env') or ''), '')
    service_key = env.get(str(translation.get('supabase_service_role_key_env') or ''), '')
    if not url or not service_key:
        return None
    validate_https_origin(url)
    transport = SafeHttpTransport(policy=SafeHttpPolicy(
        total_timeout_seconds=float(translation.get('request_timeout_seconds', 20)),
        max_wire_bytes=int(translation.get('max_response_bytes', 524288)),
        max_decoded_bytes=int(translation.get('max_response_bytes', 524288)),
        per_host_concurrency=int(translation.get('per_host_concurrency', 2))))
    store = SupabaseTranslationStore(SupabaseTranslationConfig(url, service_key), transport=transport)
    pairing = ModelPairingAdapter(
        config=ModelTranslationConfig(
            provider_id=str(translation.get('provider') or 'openai'),
            model=str(translation.get('model') or ''),
            api_origin=str(translation.get('api_origin') or 'https://api.openai.com'),
            max_response_bytes=int(translation.get('max_response_bytes', 524288))),
        transport=transport, api_key=lambda: api_key)
    provider = ModelTranslationAdapter(
        config=ModelTranslationConfig(
            provider_id=str(translation.get('provider') or 'openai'),
            model=str(translation.get('model') or ''),
            api_origin=str(translation.get('api_origin') or 'https://api.openai.com'),
            max_response_bytes=int(translation.get('max_response_bytes', 524288))),
        transport=transport, api_key=lambda: api_key)
    return store, provider, pairing

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('command', choices=('build','ingest')); p.add_argument('--root',type=Path,default=Path.cwd()); p.add_argument('--source-snapshot',type=Path,required=True); p.add_argument('--output',type=Path); a=p.parse_args()
    cfg=load_config(a.root); snap=load_source_snapshot(a.source_snapshot, expected_configuration_digest=snapshot_config_digest(cfg))
    # Use the same registry route enumeration as collection.  The global
    # Hacker News adapter is configured outside ``rss`` and category feeds.
    # Filtering before deduplication rejects an unexpected snapshot origin
    # before the expensive fuzzy pass, without changing eligible provenance.
    allowed = {spec.id for spec in configured_source_specs(cfg)}
    source_items = [
        item for result in snap.results for item in result.items
        if item.source_id in allowed
    ]
    unexpected = {
        item.source_id for result in snap.results for item in result.items
        if item.source_id not in allowed
    }
    if unexpected:
        raise ValueError("snapshot contains an unconfigured source")
    grouping_policy = GroupingPolicy.from_config(cfg.grouping or {})
    pairing_policy = PairingPolicy.from_config(cfg.translation or {})
    retained = retain(source_items, categories=cfg.categories, observed_at=snap.generated_at,
                      grouping=grouping_policy)
    corpus, truncated, decisions = (), False, {}
    url = os.environ.get('NEWS_CURATOR_SUPABASE_URL', '')
    key = os.environ.get('NEWS_CURATOR_SUPABASE_SECRET_KEY', '')
    if a.command == 'ingest' and url and key:
        validate_https_origin(url)
        try:
            corpus, truncated = read_corpus_window(url, key, policy=pairing_policy, now=snap.generated_at)
            decisions = read_exclusivity_decisions(url, key, [row.story_id for row in retained])
            print(f'pairing corpus rows={len(corpus)} truncated={truncated} '
                  f'decisions={len(decisions)}', file=sys.stderr)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
            # Without the window we cannot prove exclusivity, so this run does
            # not pay for translation. It is a skip, never a wrong claim.
            truncated = True
            print(f'pairing corpus unavailable: {type(error).__name__}', file=sys.stderr)
    recorded = []
    retained, translation_message = translate_rows(
        cfg, retained, env=os.environ, now=snap.generated_at, corpus=corpus,
        decisions=decisions, truncated=truncated, on_decision=recorded.append)
    print(translation_message, file=sys.stderr)
    if a.command == 'ingest' and url and key and recorded:
        try:
            record_exclusivity_decisions(url, key, recorded)
        except (urllib.error.URLError, ValueError) as error:
            print(f'exclusivity decisions not persisted: {type(error).__name__}', file=sys.stderr)
    rows=public_ingest_rows(retained, allowed_source_ids=allowed)
    if a.command == 'build':
        if not a.output: raise ValueError('output required')
        a.output.write_text(json.dumps({'schema_version':1,'generated_at':snap.generated_at.isoformat(),'rows':rows}, ensure_ascii=False), encoding='utf-8'); return 0
    url=os.environ.get('NEWS_CURATOR_SUPABASE_URL',''); key=os.environ.get('NEWS_CURATOR_SUPABASE_SECRET_KEY','')
    if not url or not key: raise ValueError('retained corpus ingest unavailable')
    validate_https_origin(url)
    headers={'apikey':key,'content-type':'application/json'}
    if not key.startswith('sb_secret_'): headers['authorization']='Bearer '+key
    body=json.dumps({'p_rows':rows}).encode(); request=urllib.request.Request(url+'/rest/v1/rpc/m2_ingest_retained_corpus', data=body, headers=headers, method='POST')
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
        if response.status != 200: raise ValueError('retained corpus ingest failed')
    return 0
if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception: print('retained corpus ingest unavailable', file=sys.stderr); raise SystemExit(2)
