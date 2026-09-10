"""Private discovery access and persistence on installed PostgreSQL 17.11.

News text/URLs come from a captured public-source fixture. User UUIDs are isolated
access-control test identities. Shifted capture timestamps and band exceptions
are explicit contract transforms, never live ranking or editorial evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

from curator.config import Category, Config
from curator.discovery import build_discovery, load_discovery_policy, replay_discovery
from curator.identity import story_id_for_item
from curator.personalization.ranking import InterestArtifact, EMPTY_NEWSLETTER_INPUT_DIGEST, ranking_config_digest
from curator.source_snapshot import load_source_snapshot, write_source_snapshot, snapshot_config_digest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/202609090001_discovery_lanes.sql'
RETRY_MIGRATION = ROOT / 'supabase/migrations/202609100001_discovery_retry_identity.sql'
IMAGE = 'postgres:17.11'
OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


def _run(*args, input_text=None, check=True):
    return subprocess.run(args, input=input_text, capture_output=True, text=True,
                          timeout=60, check=check)


def _sql(container, sql, check=True):
    return _run('docker', 'exec', '-i', container, 'psql', '-X', '-At', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', input_text=sql, check=check)


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _json_call(container, expression, *, role='service_role', owner=None):
    subject = f"select set_config('request.jwt.claim.sub','{owner}',false);" if owner else ''
    result = _sql(container, f"set role {role}; {subject} select coalesce(({expression}), 'null'::jsonb);")
    return json.loads(result.stdout.splitlines()[-1])


@pytest.fixture(scope='module')
def db():
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable; PostgreSQL 17.11 runtime not verified')
    if _run('docker', 'image', 'inspect', IMAGE, check=False).returncode:
        pytest.skip('Installed PostgreSQL 17.11 image or Docker daemon unavailable; no pull attempted')
    container = 'news-curator-discovery-' + uuid.uuid4().hex[:10]
    _run('docker', 'run', '--rm', '-d', '--name', container, '-e', 'POSTGRES_PASSWORD=review-only', IMAGE)
    try:
        deadline = time.monotonic() + 30
        ready = 0
        while time.monotonic() < deadline:
            result = _sql(container, 'select 1;', check=False)
            ready = ready + 1 if result.returncode == 0 and result.stdout.strip() == '1' else 0
            if ready >= 2:
                break
            time.sleep(.25)
        else:
            pytest.fail('PostgreSQL 17.11 readiness timeout')
        _sql(container, """
          create role anon nologin;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create schema extensions;
          create extension pgcrypto with schema extensions;
          create schema auth;
          create table auth.users(id uuid primary key);
          create function auth.uid() returns uuid language sql stable as $$
            select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
          grant usage on schema public,auth to anon,authenticated,service_role;
          grant execute on function auth.uid() to anon,authenticated,service_role;
        """)
        _sql(container, (ROOT / 'supabase/migrations/202608290001_user_preferences.sql').read_text())
        _sql(container, (ROOT / 'supabase/migrations/202609070001_reading_history.sql').read_text())
        _sql(container, (ROOT / 'supabase/migrations/202609080001_dashboard_summary.sql').read_text())
        _sql(container, MIGRATION.read_text())
        _sql(container, RETRY_MIGRATION.read_text())
        _sql(container, f"""
          insert into auth.users values('{OWNER}'),('{OTHER}');
          insert into public.user_preferences(user_id,revision,interests) values('{OWNER}',1,array['OpenAI']),('{OTHER}',1,array['OpenAI']);
        """)
        yield container
    finally:
        _run('docker', 'stop', container, check=False)


def _payload(tmp_path, owner=OWNER, edition_id='db-contract-1'):
    cfg = Config(categories=[Category('AI', ['OpenAI'])], rss=[], settings={}, ranking={}, dedup={}, hackernews={}, reddit={})
    path = ROOT / 'tests/fixtures/discovery-captured.json'
    raw = json.loads(path.read_text())
    captured_at = datetime.fromisoformat(raw['generated_at'].replace('Z', '+00:00'))
    capture = load_source_snapshot(path, current_time=captured_at)
    # Retain relative event ages while making the database freshness check deterministic.
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    shift = now - capture.generated_at
    for result in capture.results:
        for item in result.items:
            item.published_at += shift
    rebound = tmp_path / 'db-capture.json'
    write_source_snapshot(capture.results, rebound, generated_at=now,
                          configuration_digest=snapshot_config_digest(cfg))
    snapshot = load_source_snapshot(rebound, current_time=now)
    policy = load_discovery_policy(ROOT / 'config/discovery-policy-r2.yaml')
    policy['policy_id'] = 'captured-db-access-control-transform'
    policy['windows']['hot'] = 6
    # These tests measure storage/access, not editorial calibration. Exceptions are explicit.
    for name in policy['bands']:
        policy['bands'][name]['active'] = False
        policy['band_exceptions'][name] = 'controlled database access-control fixture'
    items = [i for r in snapshot.results for i in r.items]
    selected = next(i for i in items if i.source_id == 'techcrunch')
    profile = InterestArtifact(now.isoformat(), snapshot.content_digest,
        EMPTY_NEWSLETTER_INPUT_DIGEST, ranking_config_digest(cfg), 1, 1, 1,
        {story_id_for_item(selected): .9})
    receipt = build_discovery(cfg, snapshot, policy, interest_artifact=profile,
                              history=[], now=now)
    assert receipt['verdict'] == 'PASS'
    assert replay_discovery(receipt)
    cards = {}
    for row in receipt['entries']:
        cards[row['story_id']] = {
            'canonical_url': row['url'], 'title': row['title'], 'summary': row['description'],
            'language': row['language'], 'published_at': row['published_at'],
            'topic_ids': row['topic_ids'], 'source_kind': 'outlet', 'source_name': row['source_name'],
            'coverage_mentions': [],
        }
    assert any(not c['topic_ids'] for c in cards.values()), 'fixture must exercise outside-topic Saved'
    fingerprint = hashlib.sha256(json.dumps({'revision':1,'interests':['OpenAI'],'topic_signals':[],
        'topic_adjustments':[],'more_like_topic_weight':.8},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    identity = [owner,snapshot.content_digest,None,1,fingerprint,receipt['bindings']['policy_digest'],
                '4741a64744c35f98d9f14c1180470f9ccb766f95','en',
                receipt['bindings']['ranking_configuration_digest'],receipt['bindings']['display_dedup_digest'],
                receipt['bindings']['code_digest']]
    stable_id = 'm2:' + hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()
    return {'schema_version': 1, 'kind': 'owned_private_discovery', 'owner_user_id': owner,
            'edition_id': stable_id, 'profile_fingerprint':fingerprint,
            'code_revision': '4741a64744c35f98d9f14c1180470f9ccb766f95',
            'materialized_at': datetime.now(timezone.utc).isoformat(), 'receipt': receipt, 'cards': cards}


def _wire(payload):
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return text, hashlib.sha256(text.encode()).hexdigest()


def _finalize_expression(payload, digest=None):
    text, computed = _wire(payload)
    return f'public.finalize_private_discovery({_quote(text)},{_quote(digest or computed)})'


def _retry_expression(payload):
    receipt = payload['receipt']
    bindings = receipt['bindings']
    values = [payload['owner_user_id'], bindings['snapshot_digest'], bindings['profile_revision'],
              payload['profile_fingerprint'], bindings['policy_digest'], payload['code_revision'],
              receipt['language'], bindings['ranking_configuration_digest'],
              bindings['display_dedup_digest'], bindings['code_digest']]
    return 'public.private_discovery_retry_identity(' + ','.join(
        str(value) if isinstance(value, int) else _quote(value) for value in values) + ')'


def test_private_discovery_full_contract(db, tmp_path):
    for column, value in [('max_entries', 101), ('max_response_bytes', 1048577)]:
        rejected = _sql(db, f'set role service_role; update public.discovery_storage_policy set {column}={value};', check=False)
        assert rejected.returncode and 'check constraint' in rejected.stderr
    context = _json_call(db, f"public.private_discovery_context('{OWNER}')")
    assert context['first_edition'] and context['history_available'] and context['history'] == []
    assert set(context) == {'schema_version','owner_user_id','history_available','first_edition','latest','history','storage_policy'}
    payload = _payload(tmp_path)
    expression = _finalize_expression(payload)
    denied = _sql(db, f'set role anon; select {expression};', check=False)
    assert denied.returncode and 'permission denied' in denied.stderr
    denied = _sql(db, f'set role authenticated; select {expression};', check=False)
    assert denied.returncode and 'permission denied' in denied.stderr
    mismatch = _sql(db, 'set role service_role; select ' + _finalize_expression(payload, '0'*64) + ';', check=False)
    assert mismatch.returncode and 'digest mismatch' in mismatch.stderr
    stored = _json_call(db, expression)
    assert stored == {'schema_version':1,'status':'stored','edition_id':payload['edition_id'],'payload_digest':_wire(payload)[1]}
    assert _json_call(db, expression)['status'] == 'already_stored'
    identity = _json_call(db, f"public.private_discovery_identity('{OWNER}','{payload['edition_id']}')")
    assert identity['payload_digest'] == stored['payload_digest']
    assert _json_call(db, f"public.private_discovery_identity('{OTHER}','{payload['edition_id']}')") is None
    changed = deepcopy(payload)
    changed['materialized_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    conflict = _sql(db, 'set role service_role; select ' + _finalize_expression(changed) + ';', check=False)
    assert conflict.returncode and 'edition conflict' in conflict.stderr
    for role in ('anon','authenticated'):
        denied = _sql(db, f'set role {role}; select * from public.private_discovery_editions;', check=False)
        assert denied.returncode and 'permission denied' in denied.stderr
    denied = _sql(db, 'set role anon; select public.discovery_edition();', check=False)
    assert denied.returncode and 'permission denied' in denied.stderr
    other = _json_call(db, f"public.discovery_edition('{payload['edition_id']}')", role='authenticated', owner=OTHER)
    assert other == {'schema_version':1,'status':'unavailable','reason_code':'edition_unavailable','edition':None}
    owner = _json_call(db, 'public.discovery_edition()', role='authenticated', owner=OWNER)
    assert owner['status'] == 'ready'
    assert [e['card']['story_id'] for e in owner['edition']['entries']] == [e['story_id'] for e in payload['receipt']['entries']]
    assert all(e['card']['page_order_mode'] == 'discovery' and e['card']['next_cursor'] is None for e in owner['edition']['entries'])
    assert OWNER not in json.dumps(owner)
    assert _sql(db, "select count(*) from public.publication_entries;").stdout.strip() == '0'
    assert _sql(db, 'set role anon; select count(*) from public.feed_page();').stdout.splitlines()[-1] == '0'
    story = next(e['card']['story_id'] for e in owner['edition']['entries'] if not e['card']['topic_ids'])
    guessed = _sql(db, f"set role authenticated; select set_config('request.jwt.claim.sub','{OTHER}',false); select public.set_story_state('{story}',true,true,0,'probe');", check=False)
    assert guessed.returncode and 'story is unavailable' in guessed.stderr
    saved = _json_call(db, f"public.set_story_state('{story}',true,true,0,'save-outside')", role='authenticated', owner=OWNER)
    assert saved['status'] == 'updated' and saved['saved_at'] and saved['read_at']
    saved_rows = _sql(db, f"set role authenticated; select set_config('request.jwt.claim.sub','{OWNER}',false); select * from public.saved_page();").stdout.splitlines()
    saved_card = json.loads(saved_rows[-1])
    assert saved_card['topic_ids'] == [] and saved_card['story_id'] == story
    assert _json_call(db, 'public.dashboard_summary()', role='authenticated', owner=OWNER)['saved_count'] == 1
    current = _json_call(db, f"public.private_discovery_context('{OWNER}')")
    assert not current['first_edition'] and current['latest']['payload_digest'] == stored['payload_digest']
    assert len(current['history']) == len(payload['receipt']['entries'])
    # A real captured story explicitly archived as public stays actionable to other users.
    public_story = next(e['card']['story_id'] for e in owner['edition']['entries'] if e['card']['topic_ids'])
    _sql(db, f"""
      insert into public.publication_runs(build_nonce,commit_sha,deployed_url,built_at,candidate_digest,site_sha256)
      values('controlled-public-regression','4741a64744c35f98d9f14c1180470f9ccb766f95','https://news.joydong.org/',now(),repeat('a',64),repeat('b',64));
      insert into public.publication_topics(publication_seq,topic_id,topic_name,position)
      select publication_seq,'ai','AI',1 from public.publication_runs where build_nonce='controlled-public-regression';
      insert into public.publication_entries(publication_seq,story_id,topic_id,position,canonical_url,title,summary,language,published_at,
        score_components,ordering_mode,ordering_key,topic_ranks,source_kind,source_name,ranking_explanation)
      select r.publication_seq,s.story_id,'ai',1,s.canonical_url,s.title,s.summary,s.language,s.published_at,
        '{{}}','weighted_total','{{}}','{{}}',s.source_kind,s.source_name,'Captured source ordering'
      from public.canonical_stories s cross join public.publication_runs r
      where s.story_id='{public_story}' and r.build_nonce='controlled-public-regression';
    """)
    public_save = _json_call(db, f"public.set_story_state('{public_story}',false,true,0,'public-save')", role='authenticated', owner=OTHER)
    assert public_save['status'] == 'updated'
    public_interest = _json_call(db, f"public.set_story_interest('{public_story}','ai','more_like',0,'public-interest')", role='authenticated', owner=OTHER)
    assert public_interest['status'] == 'updated'
    anonymous_card = _sql(db, "set role anon; select * from public.feed_page('ai');").stdout.splitlines()[-1]
    assert 'primary_lane' not in anonymous_card and 'secondary_reasons' not in anonymous_card
    assert payload['receipt']['entries'][0]['plain_reason'] not in anonymous_card
    # Controlled expiry removes only test private editions. The retained Save still works.
    _sql(db, f"delete from public.private_discovery_editions where owner_user_id='{OWNER}';")
    retained = _json_call(db, f"public.set_story_state('{story}',false,true,1,'retained-save')", role='authenticated', owner=OWNER)
    assert retained['status'] == 'updated'


def test_retry_identity_is_service_only_bounded_and_ambiguous_fail_closed(db, tmp_path):
    payload = _payload(tmp_path, edition_id='retry-contract')
    expression = _finalize_expression(payload)
    _json_call(db, expression)
    retry = _json_call(db, _retry_expression(payload))
    assert set(retry) == {'edition_id', 'payload_digest', 'receipt_digest', 'generated_at'}
    assert retry['edition_id'] == payload['edition_id']
    for role in ('anon', 'authenticated'):
        denied = _sql(db, f'set role {role}; select {_retry_expression(payload)};', check=False)
        assert denied.returncode and 'permission denied' in denied.stderr
    null_expression = (
        f"public.private_discovery_retry_identity('{OWNER}',null,1,'{'a'*64}',"
        f"'{'b'*64}','{'c'*40}','en','{'d'*64}','{'e'*64}','{'f'*64}')"
    )
    null_rejected = _sql(db, f'set role service_role; select {null_expression};', check=False)
    assert null_rejected.returncode and 'retry identity unavailable' in null_rejected.stderr
    index_count = _sql(
        db, "select count(*) from pg_indexes where schemaname='public' "
        "and indexname='private_discovery_retry_lookup';"
    )
    assert index_count.stdout.strip() == '1'

    duplicate_id = 'm2:' + 'f' * 64
    _sql(db, f"""insert into public.private_discovery_editions
      select owner_user_id,'{duplicate_id}',payload_digest,payload_text,payload,generated_at,stored_at
      from public.private_discovery_editions where owner_user_id='{OWNER}' and edition_id='{payload['edition_id']}';""")
    ambiguous = _sql(db, f'set role service_role; select {_retry_expression(payload)};', check=False)
    assert ambiguous.returncode and 'retry identity ambiguous' in ambiguous.stderr
    _sql(db, f"delete from public.private_discovery_editions where edition_id in ('{duplicate_id}','{payload['edition_id']}');")


@pytest.mark.parametrize('mutation', ['failed','unknown_band','missing_profile','invalid_source_fact','duplicate','stale_history'])
def test_private_discovery_rejects_invalid_receipts_without_partial_rows(db, tmp_path, mutation):
    payload = _payload(tmp_path, edition_id='invalid-'+mutation)
    if mutation == 'failed':
        payload['receipt']['verdict'] = 'FAIL'
    elif mutation == 'unknown_band':
        payload['receipt']['bands'][0]['verdict'] = 'UNKNOWN'
    elif mutation == 'missing_profile':
        payload['receipt']['profile_available'] = False
    elif mutation == 'invalid_source_fact':
        next(iter(payload['cards'].values()))['canonical_url'] = 'https://different.invalid/'
    elif mutation == 'duplicate':
        payload['receipt']['entries'].append(deepcopy(payload['receipt']['entries'][0]))
    else:
        payload['receipt']['history_input'] = [{'story_id':'not-real','source_id':'unknown','shown_at':payload['receipt']['generated_at']}]
    result = _sql(db, 'set role service_role; select ' + _finalize_expression(payload) + ';', check=False)
    assert result.returncode
    assert _sql(db, f"select count(*) from public.private_discovery_editions where edition_id='{payload['edition_id']}';").stdout.strip() == '0'


def test_concurrent_private_finalization_is_idempotent(db, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    payload = _payload(tmp_path, edition_id='concurrent-contract')
    expression = _finalize_expression(payload)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _json_call(db, expression), range(2)))
    assert sorted(r['status'] for r in results) == ['already_stored', 'stored']
    count = _sql(db, f"select count(*) from public.private_discovery_editions where edition_id='{payload['edition_id']}';").stdout.strip()
    assert count == '1'
