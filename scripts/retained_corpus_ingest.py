#!/usr/bin/env python3
"""Build or service-ingest a public-only retained-corpus artifact."""
from __future__ import annotations
import argparse, json, os, sys, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.grouping import GroupingCandidate, GroupingPolicy
from curator.retained_corpus import (apply_translations, language_exclusive_story_ids,
                                     public_ingest_rows, regroup_with_corpus, retain)
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest
from curator.recommendation.supabase_http import _NoRedirect, validate_https_origin
from curator.sources import SafeHttpPolicy, SafeHttpTransport
from curator.translation import (
    ModelTranslationAdapter,
    ModelTranslationConfig,
    SupabaseTranslationConfig,
    SupabaseTranslationStore,
)
from curator.translation.ingest import IngestTranslationPolicy, translate_exclusive_stories


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
        cost_per_1k_characters_usd=float(translation.get('cost_per_1k_characters_usd', 0.002)),
        cache_ttl_days=int(translation.get('cache_ttl_days', 30)),
        on_failure=str(translation.get('on_failure') or 'show_original_marked'),
        max_items=int(translation.get('max_items_per_language', 25)),
        normalization_version=str(translation.get('normalization_version') or 'normalized-item-v1'),
        glossary_policy_version=str(translation.get('glossary_policy_version') or 'none-v1'),
        candidate_policy_version=str(translation.get('candidate_policy_version') or 'ranked-non-newsletter-v1'),
    )


def read_corpus_window(url, key, *, policy: GroupingPolicy, now, pages: int = 10):
    """Read the newest corpus rows inside the grouping window, newest first.

    Grouping must see stories ingested by EARLIER runs, not only this batch.
    The read RPC caps at 100 rows per call, so this pages until it leaves the
    window or hits the page cap, and the cap is reported rather than hidden.
    """
    headers = {'apikey': key, 'content-type': 'application/json'}
    if not key.startswith('sb_secret_'):
        headers['authorization'] = 'Bearer ' + key
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
                published_at=published, event_group_id=row.get('event_group_id')))
        before_published, before_story = rows[-1]['published_at'], rows[-1]['story_id']
        if len(rows) < 100:
            break
        truncated = page == pages - 1
    return tuple(collected), truncated


def translate_rows(cfg, rows, *, env, now, store=None, provider=None, corpus=()):
    """Translate the language-exclusive stories, or say plainly why not.

    Returns ``(rows, message)``. A missing credential, a switched-off feature,
    a provider failure or an unavailable cache is always a SKIP, never an
    exception: the hourly ingest must keep running and no story is ever
    dropped for a translation problem.
    """
    try:
        policy = _ingest_translation_policy(cfg)
    except ValueError as error:
        return rows, f'translation unavailable: invalid policy ({error})'
    if not policy.enabled:
        return rows, 'translation skipped: disabled in config'
    translation = cfg.translation or {}
    key_env = str(translation.get('api_key_env') or '')
    api_key = env.get(key_env, '') if key_env else ''
    if not api_key:
        return rows, 'translation skipped: key not configured'
    exclusive = set(language_exclusive_story_ids(rows, display_language=policy.display_language, corpus=corpus))
    stories = [(row.story_id, row.item) for row in rows if row.story_id in exclusive]
    if not stories:
        return rows, 'translation skipped: no language-exclusive stories'
    try:
        if store is None or provider is None:
            built = _build_translation_clients(translation, api_key, env)
            if built is None:
                return rows, 'translation skipped: store not configured'
            store = store or built[0]
            provider = provider or built[1]
        result = translate_exclusive_stories(
            stories, policy=policy, store=store, provider=provider,
            run_id=f"retained-corpus-{now.strftime('%Y%m%dT%H%M%SZ')}", now=now)
    except Exception:
        # Never let a translation problem take the ingest down with it.
        return rows, 'translation unavailable: original text retained'
    translated = result.counters.get('translated', 0) + result.counters.get('cache_hit', 0)
    return (apply_translations(rows, result.overlays),
            f'translated={translated} untranslated_shown={result.untranslated_shown}')


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
    provider = ModelTranslationAdapter(
        config=ModelTranslationConfig(
            provider_id=str(translation.get('provider') or 'openai'),
            model=str(translation.get('model') or ''),
            api_origin=str(translation.get('api_origin') or 'https://api.openai.com'),
            max_response_bytes=int(translation.get('max_response_bytes', 524288))),
        transport=transport, api_key=lambda: api_key)
    return store, provider

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
    retained = retain(source_items, categories=cfg.categories, observed_at=snap.generated_at,
                      grouping=grouping_policy)
    corpus = ()
    url = os.environ.get('NEWS_CURATOR_SUPABASE_URL', '')
    key = os.environ.get('NEWS_CURATOR_SUPABASE_SECRET_KEY', '')
    if a.command == 'ingest' and url and key:
        validate_https_origin(url)
        try:
            corpus, truncated = read_corpus_window(url, key, policy=grouping_policy, now=snap.generated_at)
            retained = regroup_with_corpus(retained, corpus, policy=grouping_policy)
            print(f'grouping corpus rows={len(corpus)} truncated={truncated}', file=sys.stderr)
        except Exception:
            # Grouping against the corpus is an improvement, never a gate. The
            # batch-local grouping from retain() still stands.
            print('grouping corpus unavailable: batch-local grouping only', file=sys.stderr)
    retained, translation_message = translate_rows(cfg, retained, env=os.environ, now=snap.generated_at,
                                                   corpus=corpus)
    print(translation_message, file=sys.stderr)
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
