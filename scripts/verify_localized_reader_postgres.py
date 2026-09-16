#!/usr/bin/env python3
"""Local protocol checks using captured public originals; overlays are test markers, not translation-quality evidence."""
from __future__ import annotations
import argparse, hashlib, json, subprocess
from pathlib import Path
from curator.translation import TranslationInput, TranslationCacheKey
from curator.models import Item
from datetime import datetime

def quote(value): return "'" + str(value).replace("'", "''") + "'"
def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--database', required=True); args=parser.parse_args()
    if not args.database.startswith('nc_'): raise SystemExit('isolated nc_ database required')
    root=Path(__file__).resolve().parents[1]
    rows=json.loads((root/'tests/fixtures/m2-retained-public.json').read_text())['rows']
    row=next(r for r in rows if r['language']=='zh' and len(r['title'])<=500 and len(r['summary'])<=2000)
    row={key:value for key,value in row.items() if key in ('story_id','origin_class','source_kind','canonical_url','title','summary','language','source_id','source_name','source_is_aggregator','published_at','source_observed_at','category_ids')}
    row.update(origin_class='public_outlet',source_kind='outlet',source_is_aggregator=False)
    row.setdefault('source_observed_at',row['published_at'])
    item=Item(title=row['title'],description=row['summary'],url=row['canonical_url'],canonical_url=row['canonical_url'],language='zh',source_id=row['source_id'],source_name=row['source_name'],published_at=datetime.fromisoformat(row['published_at']))
    content=TranslationInput.from_item(item)
    key=TranslationCacheKey.from_input(story_id=row['story_id'],content=content,target_locale='en',normalization_version='normalized-item-v1',provider='protocol-test',model_version='protocol-test',glossary_policy_version='none-v1',candidate_policy_version='protocol-test')
    values=key.as_dict(); values.pop("cache_key_digest",None); columns=list(values); vals=[quote(v) if not isinstance(v,list) else 'ARRAY['+','.join(quote(x) for x in v)+']' for v in values.values()]
    # Capture originated in the public ingest fixture. No private owner data.
    sid=quote(row['story_id']); cache=quote(key.digest)
    sql=['begin;', "set local request.jwt.claims = '{\"role\":\"service_role\"}';", f"select public.m2_ingest_retained_corpus({quote(json.dumps([row],ensure_ascii=False))}::jsonb);",
         f"select not exists(select 1 from public.m2_localized_candidates('en',null,null,null,null,100) r where r->>'story_id'={sid}) as missing_omitted;",
         f"insert into translation_private.translation_cache(cache_key_digest,{','.join(columns)},translated_title,translated_description,actual_characters) values({cache},{','.join(vals)},'Controlled translation marker','Controlled summary marker',0);",
         f"select count(*)=1 as translated_search from public.m2_localized_candidates('en',null,'Controlled translation marker',null,null,100) r where r->>'story_id'={sid} and r->>'title'={quote(row['title'])} and r->>'display_title'='Controlled translation marker' and r->>'language'='zh';",
         f"select count(*)=1 as source_search from public.m2_localized_candidates('en',null,{quote(row['title'][:4])},null,null,100) r where r->>'story_id'={sid};",
         f"select count(*)=1 as saved_overlay from public.m2_localized_story_text(ARRAY[{sid}],'en') r where r->>'title'='Controlled translation marker';",
         f"insert into translation_private.translation_cache_quarantine(cache_key_digest,reason_code) values({cache},'protocol_test');",
         f"select count(*)=1 as saved_placeholder from public.m2_localized_story_text(ARRAY[{sid}],'en') r where r->>'title'='Translation unavailable' and r->>'translation_available'='false';",
         f"delete from translation_private.translation_cache_quarantine where cache_key_digest={cache};",
         f"update public.retained_corpus_observations set summary=summary||' changed' where story_id={sid};",
         f"select not exists(select 1 from public.m2_localized_candidates('en',null,'Controlled translation marker',null,null,100) r where r->>'story_id'={sid}) as stale_digest_omitted;",
         "select not has_function_privilege('anon','public.m2_localized_candidates(text,text,text,timestamptz,text,integer)','execute') as candidates_private;",
         "select not has_function_privilege('authenticated','public.m2_translation_queue(integer,integer)','execute') as queue_private;",
         "select not has_schema_privilege('authenticated','translation_private','usage') as cache_private;",'rollback;']
    result=subprocess.run(['psql','-X','-At','-v','ON_ERROR_STOP=1','-h','/tmp',args.database],input='\n'.join(sql),text=True,capture_output=True)
    if result.returncode: print(result.stderr); raise SystemExit(1)
    checks=[x for x in result.stdout.splitlines() if x in ('t','f')]
    if len(checks)!=9 or 'f' in checks: print(result.stdout); raise SystemExit(1)
    print('PASS: 9 localized reader PostgreSQL checks; controlled overlays are not quality evidence')
if __name__=='__main__': main()
