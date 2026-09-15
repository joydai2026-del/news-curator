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

def frozen_insert(user, bindings=None, cards=None, expires_at="now()+interval '1 hour'"):
    if bindings is None:
        current=snapshot(user)
        bindings={key:current[key] for key in ('history_generation','consent_revision','server_commit_revision')}
    return f"insert into public.m2_frozen_rankings(request_id,user_id,bindings,cards,page_size,expires_at) values('{uuid.uuid4()}','{user}',{literal(json.dumps(bindings))},{literal(json.dumps(cards or []))},20,{expires_at});"

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
unseen_row=next(r for r in json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'] if r['story_id'] != story)
unseen_story=unseen_row['story_id']
sql('insert into public.canonical_stories(story_id,canonical_url,title,summary,language,source_kind,source_name,published_at) values ('+
    ','.join(literal(v) for v in (story,row['canonical_url'],row['title'],row['summary'],row['language'],'outlet',row['source_name'],row['published_at']))+');')
sql(f"insert into public.retained_corpus_observations(story_id,source_id,source_name,source_is_aggregator,language,title,summary,canonical_url,published_at,first_observed_at,source_observed_at) select story_id,'captured','Captured',false,language,title,summary,canonical_url,published_at,now(),now() from public.canonical_stories where story_id={literal(story)}; insert into public.retained_corpus_categories(story_id,category_id) values({literal(story)},{literal(topic)});")
sql('insert into public.canonical_stories(story_id,canonical_url,title,summary,language,source_kind,source_name,published_at) values ('+
    ','.join(literal(v) for v in (unseen_story,unseen_row['canonical_url'],unseen_row['title'],unseen_row['summary'],unseen_row['language'],'outlet',unseen_row['source_name'],unseen_row['published_at']))+');')
sql(f"insert into public.retained_corpus_observations(story_id,source_id,source_name,source_is_aggregator,language,title,summary,canonical_url,published_at,first_observed_at,source_observed_at) select story_id,'captured','Captured',false,language,title,summary,canonical_url,published_at,now(),now() from public.canonical_stories where story_id={literal(unseen_story)};")
action_owner=str(uuid.uuid4())
sql(f"insert into auth.users values('{action_owner}');")
def call(statement, ok=True): return sql(authenticated(action_owner,statement),ok=ok)
def result(statement): return json.loads(call(statement).splitlines()[-1])
def event_id():return 'event:'+uuid.uuid4().hex+uuid.uuid4().hex
def mutation(kind,key,event,revision,generation,*,read=True,saved=True,event_type='save',signal='more_like'):
    when="'2026-09-01T00:00:00Z'"
    if kind=='state':
        return f"select public.set_story_state_with_event({literal(story)},{str(read).lower()},{str(saved).lower()},{revision},{literal(key)},{literal(event)},{literal(event_type)},'local-test',{when},{generation});"
    return f"select public.set_story_interest_with_event({literal(story)},{literal(topic)},{literal(signal)},{revision},{literal(key)},{literal(event)},'local-test',{when},{generation});"
def event_count():return int(sql(f"select count(*) from public.user_behavior_events where user_id='{action_owner}';"))
def event_record(event_key):
    return json.loads(sql(f"select jsonb_build_object('event_type',event_type,'payload',payload) from public.user_behavior_events where user_id='{action_owner}' and event_id={literal(event_key)};"))
def current_revision(kind):
    table='user_story_state' if kind=='state' else 'user_story_interests'
    return int(sql(f"select revision from public.{table} where user_id='{action_owner}' and story_id={literal(story)};"))

def action_side_effects(user, story_id):
    return json.loads(sql(f"""select jsonb_build_object(
      'events',(select count(*) from public.user_behavior_events where user_id='{user}'),
      'states',(select count(*) from public.user_story_state where user_id='{user}' and story_id={literal(story_id)}),
      'interests',(select count(*) from public.user_story_interests where user_id='{user}' and story_id={literal(story_id)}),
      'receipts',(select count(*) from public.user_action_receipts where user_id='{user}')
    );"""))

def rejects_without_side_effects(label, user, story_id, statements):
    before=action_side_effects(user,story_id)
    rejected=all(sql(authenticated(user,statement),ok=False).returncode!=0 for statement in statements)
    check(label,rejected and action_side_effects(user,story_id)==before)

def combined_branch(label,kind,revision,generation,**mutation_values):
    key=uuid.uuid4().hex; event_key=event_id(); statement=mutation(kind,key,event_key,revision,generation,**mutation_values)
    before=event_count()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first,replay=list(pool.map(result,[statement,statement]))
    check(label+' same key same event replays complete original receipt',first==replay and event_count()==before+1)
    check(label+' same key different event rejects without append',call(mutation(kind,key,event_id(),revision,generation,**mutation_values),ok=False).returncode!=0 and event_count()==before+1)
    repeated=result(mutation(kind,uuid.uuid4().hex,event_id(),first['revision'],generation,**mutation_values))
    check(label+' distinct action records distinct event',repeated['revision']==first['revision']+1 and event_count()==before+2)
    return event_key

call("select public.set_behavior_consent(true,true,'local-policy');")
sql(frozen_insert(action_owner,cards=[{'story_id':story}]))
# Captured retained stories must be actionable only while the owner has a live
# frozen order. Expired orders, cross-owner attempts, and unseen retained rows
# cannot create state, interest, behavior, or idempotency side effects.
expired_owner=str(uuid.uuid4())
sql(f"insert into auth.users values('{expired_owner}');")
sql(frozen_insert(expired_owner,cards=[{'story_id':story}],expires_at="now()-interval '1 second'"))
rejects_without_side_effects('expired own frozen retained story rejects actions without side effects',expired_owner,story,[
    f"select public.set_story_state({literal(story)},true,true,0,'expired-state');",
    f"select public.set_story_interest({literal(story)},{literal(topic)},'more_like',0,'expired-interest');",
])
rejects_without_side_effects('other owner read save plus minus combined actions deny without side effects',other,story,[
    f"select public.set_story_state_with_event({literal(story)},true,false,0,'other-read',{literal(event_id())},'read_more','local-test','2026-09-01T00:00:00Z',1);",
    f"select public.set_story_state_with_event({literal(story)},true,true,0,'other-save',{literal(event_id())},'save','local-test','2026-09-01T00:00:00Z',1);",
    f"select public.set_story_interest_with_event({literal(story)},{literal(topic)},'more_like',0,'other-plus',{literal(event_id())},'local-test','2026-09-01T00:00:00Z',1);",
    f"select public.set_story_interest_with_event({literal(story)},{literal(topic)},'less_like',0,'other-minus',{literal(event_id())},'local-test','2026-09-01T00:00:00Z',1);",
])
generation=snapshot(action_owner)['history_generation']
rejects_without_side_effects('unseen retained story combined actions deny without side effects',action_owner,unseen_story,[
    f"select public.set_story_state_with_event({literal(unseen_story)},true,true,0,'unseen-state-event',{literal(event_id())},'save','local-test','2026-09-01T00:00:00Z',{generation});",
    f"select public.set_story_interest_with_event({literal(unseen_story)},{literal(topic)},'more_like',0,'unseen-interest-event',{literal(event_id())},'local-test','2026-09-01T00:00:00Z',{generation});",
])
check('unseen retained story is unavailable',call(f"select public.set_story_state({literal(unseen_story)},true,true,0,'unseen');",ok=False).returncode!=0)
check('other owner cannot use ranked story',sql(authenticated(other,f"select public.set_story_state({literal(story)},true,true,0,'cross-owner');"),ok=False).returncode!=0)
check('unassigned retained category is unavailable',call(f"select public.set_story_interest({literal(story)},'not-assigned','more_like',0,'bad-topic');",ok=False).returncode!=0)
for kind,revision in [('state',0),('interest',0)]:
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

# Controlled local identities exercise native RPC branches that differ by event
# payload and mapped event type. These are not owner-history or production data.
generation=snapshot(action_owner)['history_generation']
unsave_event=combined_branch('unsave', 'state', current_revision('state'), generation, read=True, saved=False, event_type='save')
unsave_payload=event_record(unsave_event)
check('unsave stores explicit saved false payload',unsave_payload['event_type']=='save' and unsave_payload['payload'].get('saved') is False)
# Restore the saved state through M1 so read_more proves its own transition and
# payload shape rather than relying on an unchanged state.
resave_revision=current_revision('state')
result(f"select public.set_story_state({literal(story)},true,true,{resave_revision},{literal(uuid.uuid4().hex)});")
generation=snapshot(action_owner)['history_generation']
read_more_event=combined_branch('read_more', 'state', current_revision('state'), generation, read=True, saved=False, event_type='read_more')
read_more_payload=event_record(read_more_event)
check('read_more omits saved payload key',read_more_payload['event_type']=='read_more' and 'saved' not in read_more_payload['payload'])
# Restore the pre-existing saved state without appending a behavior event so the
# original withdrawal invariant remains exercised without alteration.
restore_revision=current_revision('state')
result(f"select public.set_story_state({literal(story)},true,true,{restore_revision},{literal(uuid.uuid4().hex)});")
generation=snapshot(action_owner)['history_generation']
less_like_event=combined_branch('less_like', 'interest', current_revision('interest'), generation, signal='less_like')
check('less_like maps to negative behavior event',event_record(less_like_event)['event_type']=='less_like_this')

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
# Reset deletes the owner's populated frozen order. A retained row that was never
# acted on must immediately become unavailable again.
sql(frozen_insert(action_owner,cards=[{'story_id':unseen_story}]))
check('reset fixture has populated own frozen retained story',sql(f"select count(*)=1 from public.m2_frozen_rankings where user_id='{action_owner}';")=='t')
call('select public.clear_behavior_history();')
check('reset removes populated own frozen order',sql(f"select count(*)=0 from public.m2_frozen_rankings where user_id='{action_owner}';")=='t')
rejects_without_side_effects('reset makes unacted retained story unavailable',action_owner,unseen_story,[
    f"select public.set_story_state({literal(unseen_story)},true,true,0,'reset-unseen');",
])

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

# A saved interest is its own owner-access grant. Expiring its original frozen
# card cannot erase that interest or create a read-state. Category retraction
# removes current feed eligibility while preserving historical topic integrity.
interest_owner=str(uuid.uuid4())
sql(f"insert into auth.users values('{interest_owner}');")
sql(frozen_insert(interest_owner,cards=[{'story_id':story}]))
first_interest=json.loads(sql(authenticated(interest_owner,f"select public.set_story_interest({literal(story)},{literal(topic)},'more_like',0,'interest-only-first');")).splitlines()[-1])
sql(f"update public.m2_frozen_rankings set expires_at=now()-interval '1 second' where user_id='{interest_owner}';")
second_interest=json.loads(sql(authenticated(interest_owner,f"select public.set_story_interest({literal(story)},{literal(topic)},'less_like',{first_interest['revision']},'interest-only-after-expiry');")).splitlines()[-1])
check('expired frozen interest-only owner preserves and updates own interest without read state',second_interest['status']=='updated' and sql(f"select count(*)=1 and (select signal='less_like' from public.user_story_interests where user_id='{interest_owner}' and story_id={literal(story)}) and not exists(select 1 from public.user_story_state where user_id='{interest_owner}' and story_id={literal(story)});")=='t')
sql(f"delete from public.retained_corpus_categories where story_id={literal(story)} and category_id={literal(topic)};")
check('category retraction preserves historical topic and interest but excludes current M2 category candidates',sql(f"select exists(select 1 from public.user_story_interests where user_id='{interest_owner}' and story_id={literal(story)}) and exists(select 1 from public.story_topics where story_id={literal(story)} and topic_id={literal(topic)}) and not exists(select 1 from public.m2_retained_candidates({literal(topic)},null,null,null,50) row where row->>'story_id'={literal(story)});")=='t')
sql(authenticated(interest_owner,'select public.clear_behavior_history();'))
rejects_without_side_effects('reset removes interest-only access after frozen expiry',interest_owner,story,[
    f"select public.set_story_interest({literal(story)},{literal(topic)},'more_like',0,'interest-only-after-reset');",
])
receipt={'database':DB,'environment':'local PostgreSQL only, no production claims','migrations':migrations,'checks':checks}
args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'database':DB,'passed':sum(c['passed'] for c in checks),'failed':[c['name'] for c in checks if not c['passed']]}))
raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
