"""Local-only migration test. Random UUIDs are isolated auth test identities."""
import argparse
import shutil
import concurrent.futures
import hashlib
import json
import subprocess
import uuid
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', default='/tmp', choices=['/tmp', 'localhost', '127.0.0.1'])
parser.add_argument('--port', type=int, default=5432)
parser.add_argument('--receipt', required=True, type=Path)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
DB = 'nc_m2_combined_' + uuid.uuid4().hex[:12]
PSQL = shutil.which('psql')
if not PSQL:
    raise SystemExit('psql is required')
checks = []

def sql(statement, database=DB, ok=True):
    result = subprocess.run([PSQL, '-X', '-h', args.host, '-p', str(args.port), '-At', '-v', 'ON_ERROR_STOP=1', database], input=statement, text=True, capture_output=True, timeout=30)
    if ok and result.returncode:
        raise RuntimeError(result.stderr[-1800:])
    return result.stdout.strip() if ok else result

def check(name, condition):
    checks.append({'name': name, 'passed': bool(condition)})

def literal(value):
    return "'"+str(value).replace("'", "''")+"'"

def authenticated(user, statement):
    return f"set role authenticated; set request.jwt.claim.sub='{user}'; "+statement

def snapshot(user):
    return json.loads(sql(authenticated(user,'select public.m2_history_snapshot();')).splitlines()[-1])

def frozen_insert(user, bindings=None):
    if bindings is None:
        current=snapshot(user)
        bindings={key:current[key] for key in ('history_generation','consent_revision','server_commit_revision')}
    return f"insert into public.m2_frozen_rankings(request_id,user_id,bindings,cards,page_size,expires_at) values('{uuid.uuid4()}','{user}',{literal(json.dumps(bindings))},'[]',20,now()+interval '1 hour');"

sql('create database '+DB, database='postgres')
sql("""create schema auth; create schema extensions;
create extension pgcrypto with schema extensions;
create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$
 select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
create function auth.jwt() returns jsonb language sql stable as $$
 select coalesce(nullif(current_setting('request.jwt.claims',true),'')::jsonb,'{}'::jsonb) $$;
grant usage on schema auth to anon,authenticated,service_role;
grant execute on all functions in schema auth to anon,authenticated,service_role;""")
migrations=[]
for path in sorted((ROOT/'supabase/migrations').glob('*.sql')):
    data=path.read_text()
    sql(data)
    migrations.append({'file':path.name,'sha256':hashlib.sha256(data.encode()).hexdigest()})
check('all migrations apply',True)
owner, other = str(uuid.uuid4()), str(uuid.uuid4())
sql(f"insert into auth.users values ('{owner}'),('{other}');")
def service(statement):
    return 'set role service_role; '+statement
def reserve(_):
    rid=str(uuid.uuid4())
    result=sql(service(f"select public.m2_reserve_ranker_budget('{owner}','{rid}',0.01,0.05);"))
    return rid,result.splitlines()[-1]
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    reservations=list(pool.map(reserve,range(20)))
accepted=[rid for rid,result in reservations if result=='t']
check('20 concurrent reservations admit exactly budget capacity5',len(accepted)==5)
check('daily reserved does not exceed cap',sql(f"select reserved_usd=0.05 and spent_usd=0 from public.m2_ranker_daily_budget where user_id='{owner}';")=='t')
rid=accepted[0]
settle=f"select public.m2_settle_ranker_budget('{owner}','{rid}',0.006,'settled');"
sql(service(settle)); sql(service(settle))
check('identical ambiguous settlement retry counted once',sql(f"select reserved_usd=0.04 and spent_usd=0.006 from public.m2_ranker_daily_budget where user_id='{owner}';")=='t')
check('conflicting settlement rejects',sql(service(f"select public.m2_settle_ranker_budget('{owner}','{rid}',0.005,'settled');"),ok=False).returncode!=0)
check('cross-owner settlement rejects',sql(service(f"select public.m2_settle_ranker_budget('{other}','{accepted[1]}',0,'released');"),ok=False).returncode!=0)
check('authenticated cannot reserve',sql(f"set role authenticated; select public.m2_reserve_ranker_budget('{owner}','{uuid.uuid4()}',0.01,2);",ok=False).returncode!=0)
check('authenticated cannot read frozen orders',sql('set role authenticated; select * from public.m2_frozen_rankings;',ok=False).returncode!=0)
for user,with_revision in [(owner,True),(other,False)]:
    if with_revision:
        sql(f"insert into public.user_behavior_revisions(user_id) values('{user}');")
    sql(frozen_insert(user))
    sql(f"set role authenticated; set request.jwt.claim.sub='{user}'; select public.clear_behavior_history();")
    check('clear deletes frozen order '+('existing revision' if with_revision else 'no previous events'),sql(f"select count(*)=0 from public.m2_frozen_rankings where user_id='{user}';")=='t')
check('clear preserves budget audit',sql(f"select count(*)=5 from public.m2_ranker_reservations where user_id='{owner}';")=='t')

# Actual captured public article; all actions and identities below are isolated test inputs.
row=next(r for r in json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'] if r['category_ids'])
story,topic=row['story_id'],row['category_ids'][0]
sql('insert into public.canonical_stories(story_id,canonical_url,title,summary,language,source_kind,source_name,published_at) values ('+
    ','.join(literal(v) for v in (story,row['canonical_url'],row['title'],row['summary'],row['language'],'outlet',row['source_name'],row['published_at']))+');')
sql(f"insert into public.story_topics(story_id,topic_id,topic_name) values({literal(story)},{literal(topic)},{literal(topic)});")
action_owner=str(uuid.uuid4())
sql(f"insert into auth.users values('{action_owner}'); insert into public.user_story_state(user_id,story_id) values('{action_owner}',{literal(story)});")
def call(statement, ok=True): return sql(authenticated(action_owner,statement),ok=ok)
def result(statement): return json.loads(call(statement).splitlines()[-1])
def event_id():return 'event:'+uuid.uuid4().hex+uuid.uuid4().hex
def mutation(kind,key,event,revision,generation,signal='more_like'):
    when="'2026-09-01T00:00:00Z'"
    if kind=='state':
        return f"select public.set_story_state_with_event({literal(story)},true,true,{revision},{literal(key)},{literal(event)},'save','local-test',{when},{generation});"
    return f"select public.set_story_interest_with_event({literal(story)},{literal(topic)},{literal(signal)},{revision},{literal(key)},{literal(event)},'local-test',{when},{generation});"
def event_count():return int(sql(f"select count(*) from public.user_behavior_events where user_id='{action_owner}';"))

call("select public.set_behavior_consent(true,true,'local-policy');")
for kind,revision in [('state',1),('interest',0)]:
    generation=snapshot(action_owner)['history_generation']; key=uuid.uuid4().hex; event=event_id()
    statement=mutation(kind,key,event,revision,generation)
    before=event_count()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first,replay=list(pool.map(result,[statement,statement]))
    check(kind+' same key same event replays complete original receipt',first==replay and event_count()==before+1)
    check(kind+' same key different event rejects without append',call(mutation(kind,key,event_id(),revision,generation),ok=False).returncode!=0 and event_count()==before+1)
    repeated=result(mutation(kind,uuid.uuid4().hex,event_id(),first['revision'],generation))
    check(kind+' distinct action records distinct event',repeated['revision']==first['revision']+1 and event_count()==before+2)
    # A plain M1 retry may replay state, but cannot learn another event.
    plain=(f"select public.set_story_state({literal(story)},true,true,{revision},{literal(key)});" if kind=='state' else
        f"select public.set_story_interest({literal(story)},{literal(topic)},'more_like',{revision},{literal(key)});")
    if kind=='interest': first_interest_plain=plain
    result(plain)
    check(kind+' combined then M1 retry adds no event',event_count()==before+2)
    plain_key=uuid.uuid4().hex; revision=repeated['revision']
    plain=(f"select public.set_story_state({literal(story)},true,true,{revision},{literal(plain_key)});" if kind=='state' else
        f"select public.set_story_interest({literal(story)},{literal(topic)},'more_like',{revision},{literal(plain_key)});")
    result(plain)
    check(kind+' M1 receipt cannot be promoted to combined event',call(mutation(kind,plain_key,event_id(),revision,generation),ok=False).returncode!=0 and event_count()==before+2)

# Provider-only withdrawal retains raw events and M1 state, deletes derivatives.
before=event_count(); old=snapshot(action_owner)
sql(f"insert into public.user_behavior_profile_state(user_id,profile) values('{action_owner}','{{}}');")
sql(frozen_insert(action_owner))
call('select public.set_behavior_consent(true,false,null);')
new=snapshot(action_owner)
check('provider withdrawal retains raw learning history',event_count()==before and new['learning_enabled'] and len(new['events'])==before)
check('provider withdrawal advances generation and consent',new['history_generation']>old['history_generation'] and new['consent_revision']>old['consent_revision'])
check('withdrawal clears all derived owner data',sql(f"select not exists(select 1 from public.user_behavior_profile_state where user_id='{action_owner}') and not exists(select 1 from public.user_story_interests where user_id='{action_owner}') and not exists(select 1 from public.m2_frozen_rankings where user_id='{action_owner}');")=='t')
check('withdrawal preserves M1 read and saved state',sql(f"select read_at is not null and saved_at is not null from public.user_story_state where user_id='{action_owner}' and story_id={literal(story)};")=='t')
check('revocation tombstone blocks old M1 interest replay',result(first_interest_plain)=={'status':'conflict','revision':0} and sql(f"select count(*)=0 from public.user_story_interests where user_id='{action_owner}';")=='t')
old_bindings={k:old[k] for k in ('history_generation','consent_revision','server_commit_revision')}
check('stale provider result cannot insert frozen cards',sql(frozen_insert(action_owner,old_bindings),ok=False).returncode!=0)

# Repeat with learning disabled: an accepted state-only action stays state-only
# on replay after enabling learning, while changed event identity is rejected.
call('select public.set_behavior_consent(false,false,null);')
for kind in ('state','interest'):
    generation=snapshot(action_owner)['history_generation']
    revision=int(sql(f"select coalesce((select revision from public.{'user_story_state' if kind=='state' else 'user_story_interests'} where user_id='{action_owner}' and story_id={literal(story)}),0);"))
    key=uuid.uuid4().hex; event=event_id(); statement=mutation(kind,key,event,revision,generation)
    check(kind+' disabled learning still validates event identity',call(mutation(kind,uuid.uuid4().hex,'invalid-event',revision,generation),ok=False).returncode!=0)
    before=event_count(); first=result(statement)
    check(kind+' learning disabled stores no event',first['behavior_event']['status']=='learning_disabled' and event_count()==before)
    call('select public.set_behavior_consent(true,false,null);')
    check(kind+' re-enable replay cannot retroactively learn',result(statement)==first and event_count()==before)
    check(kind+' re-enable changed event replay rejects',call(mutation(kind,key,event_id(),revision,generation),ok=False).returncode!=0)
    call('select public.clear_behavior_history();')
    check(kind+' reset rejects old combined generation',call(statement,ok=False).returncode!=0)
    check(kind+' reset rejects stale generation for new action key',call(mutation(kind,uuid.uuid4().hex,event_id(),revision,generation),ok=False).returncode!=0)
    call('select public.set_behavior_consent(false,false,null);')
check('explicit clear removes raw history',event_count()==0)
check('clear tombstone blocks old M1 interest replay',result(first_interest_plain)=={'status':'conflict','revision':0} and sql(f"select count(*)=0 from public.user_story_interests where user_id='{action_owner}';")=='t')

# Deterministic concurrent interleaving: revocation holds the owner lock while
# an old completed provider response tries to persist. The insert must wait,
# then reject after revocation commits, never resurrecting private cards.
call("select public.set_behavior_consent(true,true,'local-policy');")
old=snapshot(action_owner); old_bindings={k:old[k] for k in ('history_generation','consent_revision','server_commit_revision')}
sql(frozen_insert(action_owner,old_bindings))
marker='nc_m2_revoke_'+uuid.uuid4().hex[:10]
revoke_sql=authenticated(action_owner,f"begin; set application_name='{marker}'; select public.set_behavior_consent(true,false,null); select pg_sleep(1.5); commit;")
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    revoke=pool.submit(sql,revoke_sql)
    for _ in range(100):
        sleeping=sql(f"select exists(select 1 from pg_stat_activity where application_name='{marker}' and wait_event='PgSleep');")=='t'
        if sleeping:break
        time.sleep(.01)
    check('revocation interleaving reached locked transaction',sleeping)
    late=pool.submit(sql,frozen_insert(action_owner,old_bindings),DB,False)
    # frozen_insert with explicit bindings must not query blocked owner snapshot.
    revoke.result(timeout=5); inserted=late.result(timeout=5)
check('pending frozen write rejects after concurrent revocation',inserted.returncode!=0 and 'stale frozen ranking bindings' in inserted.stderr)
check('concurrent revocation leaves no frozen cards',sql(f"select count(*)=0 from public.m2_frozen_rankings where user_id='{action_owner}';")=='t')
receipt={'database':DB,'environment':'local PostgreSQL only, no production claims','migrations':migrations,'checks':checks}
args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'database':DB,'passed':sum(c['passed'] for c in checks),'failed':[c['name'] for c in checks if not c['passed']]}))
raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
