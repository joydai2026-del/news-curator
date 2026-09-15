#!/usr/bin/env python3
"""Build or service-ingest a public-only retained-corpus artifact."""
from __future__ import annotations
import argparse, json, os, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.config import load_config
from curator.pipeline import configured_source_specs
from curator.retained_corpus import public_ingest_rows, retain
from curator.source_snapshot import load_source_snapshot, snapshot_config_digest
from curator.recommendation.supabase_http import _NoRedirect, validate_https_origin

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
    rows=public_ingest_rows(retain(source_items, categories=cfg.categories, observed_at=snap.generated_at), allowed_source_ids=allowed)
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
