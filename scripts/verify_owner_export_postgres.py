"""Exercise owner export in an existing isolated local test database only."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database',required=True)
parser.add_argument('--host',default='/tmp',choices=['/tmp','localhost','127.0.0.1'])
parser.add_argument('--receipt',required=True,type=Path)
args=parser.parse_args()
if not re.fullmatch(r'nc_m2_combined_[0-9a-f]{12}',args.database):
    raise SystemExit('Only a verifier-created isolated database is allowed')
psql=shutil.which('psql')
if not psql:raise SystemExit('psql is required')
ROOT=Path(__file__).resolve().parents[1]
checks=[]
def sql(statement,ok=True):
    run=subprocess.run([psql,'-X','-h',args.host,'-At','-v','ON_ERROR_STOP=1',args.database],input=statement,text=True,capture_output=True,timeout=30)
    if ok and run.returncode:raise RuntimeError(run.stderr[-1000:])
    return run.stdout.strip().splitlines()[-1] if ok else run
def literal(value):return "'"+str(value).replace("'","''")+"'"
def auth(owner,statement):return f"set role authenticated; set request.jwt.claim.sub='{owner}'; "+statement
def page(owner,cursor=None,fence=None,ok=True):
    statement='select public.m2_owner_export_page('+('null' if cursor is None else literal(cursor))+','+('null' if fence is None else literal(fence))+');'
    result=sql(auth(owner,statement),ok)
    return json.loads(result) if ok else result
def check(name,value):checks.append({'name':name,'passed':bool(value)})

owner,other=str(uuid.uuid4()),str(uuid.uuid4())
row=next(r for r in json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'] if r['category_ids'])
story,topic=row['story_id'],row['category_ids'][0]
sql(f"insert into auth.users values('{owner}'),('{other}');")
sql('insert into public.canonical_stories(story_id,canonical_url,title,summary,language,source_kind,source_name,published_at) values ('+
    ','.join(literal(v) for v in (story,row['canonical_url'],row['title'],row['summary'],row['language'],'outlet',row['source_name'],row['published_at']))+') on conflict do nothing;')
sql(f"insert into public.story_topics(story_id,topic_id,topic_name) values({literal(story)},{literal(topic)},{literal(topic)}) on conflict do nothing;")
sql(f"""insert into public.user_behavior_settings(user_id,learning_enabled) values('{owner}',true),('{other}',true);
insert into public.user_behavior_revisions(user_id,latest_revision) values('{owner}',240),('{other}',1);
insert into public.user_preferences(user_id) values('{owner}');
insert into public.user_behavior_profile_state(user_id,history_revision,profile) values('{owner}',240,'{{"local_test":true}}');
insert into public.user_story_state(user_id,story_id,read_at,saved_at) values('{owner}',{literal(story)},now(),now());
insert into public.user_story_interests(user_id,story_id,topic_id,signal) values('{owner}',{literal(story)},{literal(topic)},'more_like');
insert into public.user_behavior_events(user_id,event_id,event_revision,schema_version,actor_kind,event_type,payload,request_digest,occurred_at)
select '{owner}','event:'||encode(extensions.digest('{owner}'||n::text,'sha256'),'hex'),n,1,'human','search_query',
jsonb_build_object('query',{literal(row['title'])},'surface','local-export-test'),repeat('a',64),now() from generate_series(1,240)n;
insert into public.user_behavior_events(user_id,event_id,event_revision,schema_version,actor_kind,event_type,payload,request_digest,occurred_at)
values('{other}','event:'||repeat('b',64),1,1,'human','search_query','{{"query":"isolated other-owner marker","surface":"local-export-test"}}',repeat('b',64),now());""")
first=page(owner); repeated=page(owner)
check('identical first page is repeatable',first==repeated)
check('export endpoint does not expose model output or action receipts',all(r['section'] not in ('frozen_rankings','action_receipts') for r in first['rows']))
rows=[]; current=first; cursors=[]
while True:
    rows.extend(current['rows'])
    if current['next_cursor'] is None:break
    cursors.append(current['next_cursor'])
    current=page(owner,current['next_cursor'],first['fence'])
    assert len(cursors)<20
check('all 240 raw events exported beyond 200-history window',sum(r['section']=='behavior_events' for r in rows)==240)
check('all requested owner data sections exported',set(r['section'] for r in rows)=={'behavior_events','behavior_settings','behavior_revisions','behavior_profile','preferences','reading_state','story_interests'})
check('pagination is complete without duplicate rows',len(rows)==first['total_rows']==len({(r['section'],r['key']) for r in rows})==246)
check('all pages share a stable final fence',page(owner,None,first['fence'])['fence']==first['fence'])
check('repeated continuation is stable',page(owner,cursors[0])==page(owner,cursors[0]))
check('cross-owner continuation rejects',page(other,cursors[0],ok=False).returncode!=0)
check('other owner fresh export contains only its own event',sum(r['section']=='behavior_events' for r in page(other)['rows'])==1)
check('anonymous export denied',sql("set role anon; select public.m2_owner_export_page();",False).returncode!=0)
check('logged-out authenticated role denied',sql("set role authenticated; set request.jwt.claim.sub=''; select public.m2_owner_export_page();",False).returncode!=0)
check('internal unpaged projection is not callable by owner',sql(auth(owner,'select * from public.m2_owner_export_rows();'),False).returncode!=0)
check('malformed cursor rejected',page(owner,'e30=',ok=False).returncode!=0)
sql(f"update public.user_preferences set locale='zh' where user_id='{owner}';")
check('preferences change invalidates pending export',page(owner,cursors[0],ok=False).returncode!=0)
changed=page(owner)
sql(f"update public.user_story_state set saved_at=null,revision=revision+1 where user_id='{owner}';")
check('reading state change invalidates pending export',page(owner,changed['next_cursor'],ok=False).returncode!=0)
before_reset=page(owner)
sql(auth(owner,'select public.clear_behavior_history();'))
check('reset invalidates old pages and final fence',page(owner,before_reset['next_cursor'],ok=False).returncode!=0 and page(owner,None,before_reset['fence'],ok=False).returncode!=0)
check('new export after reset has no deleted raw history',not any(r['section']=='behavior_events' for r in page(owner)['rows']))
receipt={'environment':'isolated local PostgreSQL; simulated owners/actions; captured public article',
    'database':args.database,'migration_sha256':hashlib.sha256((ROOT/'supabase/migrations/202609140004_m2_owner_export.sql').read_bytes()).hexdigest(),
    'exported_rows':len(rows),'raw_events':240,'checks':checks}
args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'passed':sum(c['passed'] for c in checks),'failed':[c['name'] for c in checks if not c['passed']]}))
raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
