"""Actual rendered run() -> ASGI -> RankingService, using captured public news.

Auth, owner state storage and behavior RPCs are explicit local test doubles.
The provider must never be called. This is integration evidence, not live proof.
"""
from __future__ import annotations
import asyncio
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from curator.models import Item
from curator.render import render_site, configure_m2_reader
from curator.recommendation.service import RankingService, ServicePolicy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.asgi import RankingASGI
from scripts.build_auth_callback import activate_personalization_link

# F3: this used to be `pytest.importorskip`, and playwright was pinned in no
# requirements file and installed by no workflow, so every assertion below
# reported SKIPPED and proved nothing. A hard import is the point: a missing
# browser stack must fail the run, never quietly pass it.
import pytest
from playwright import sync_api as playwright
ROOT=Path(__file__).resolve().parents[1]
READER='https://reader.example'
RANKER='https://ranker.example'
DATABASE='https://project-ref.supabase.co'
OWNER='00000000-0000-0000-0000-000000000001'

class LocalStore:
    def __init__(self, rows):
        self.rows=rows; self.frozen={}; self.events=[]; self.states={}; self.rank_reads=[]
        self.generation=1; self.consent=1; self.learning=True; self.provider=False
    def history_snapshot(self, token):
        revision=len(self.events)
        return {'history_revision':revision, 'server_commit_revision':revision,
            'included_history_revision':revision if self.learning else 0,
            'history_generation':self.generation,'consent_revision':self.consent,
            'learning_enabled':self.learning,'provider_processing_enabled':self.provider,
            'provider_policy_id':'test-policy' if self.provider else None,
            'newest_event_id':self.events[-1]['event_id'] if self.events and self.learning else None,
            'events':copy.deepcopy(self.events) if self.learning else []}
    def retained_candidates(self, *, category_id, query, limit, **kwargs):
        self.rank_reads.append((category_id,query,len(self.events)))
        rows=[r for r in self.rows if (not category_id or category_id in r['category_ids']) and
              (not query or query.casefold() in (r['title']+' '+r['summary']).casefold())]
        before=kwargs.get('before_published_at')
        sid=kwargs.get('before_story_id')
        if before: rows=[r for r in rows if (r['published_at'],r['story_id']) < (before,sid)]
        return rows[:limit]
    def owner_states(self, token, story_ids):
        return {sid:copy.deepcopy(self.states.get(sid, {'read_at':None,'saved_at':None,'state_revision':0,'interests':[]})) for sid in story_ids}
    def reserve_budget(self, **kwargs): raise AssertionError('Unpriced local fallback must not reserve provider spend')
    def settle_budget(self, **kwargs): raise AssertionError('Provider must not run')
    def save_frozen_order(self, **kwargs):
        key=str(len(self.frozen)+1); self.frozen[key]=copy.deepcopy(kwargs); return key
    def load_frozen_order(self, *, user_id, frozen_order_id):
        row=self.frozen.get(frozen_order_id)
        assert row is None or row['user_id']==user_id
        return copy.deepcopy(row)
    def event(self, body):
        if not self.learning:return {'status':'learning_disabled'}
        assert body['p_expected_history_generation']==self.generation
        self.events.append({'event_id':body['p_event_id'],'event_type':body['p_event_type'],
            'payload':body['p_payload'],'occurred_at':body['p_occurred_at'],
            'event_revision':len(self.events)+1, 'story_title':None,'story_summary':None,'source_id':None})
        return {'status':'recorded','event_id':body['p_event_id'],'event_revision':len(self.events)}

class LocalAuth:
    def get_user(self, token):
        assert token=='local-auth-token'
        return {'id':OWNER}

class NoProvider:
    def rerank(self, *args, **kwargs):raise AssertionError('Local deterministic test must not invoke a provider')


def asgi_request(app, request):
    body=(request.post_data or '').encode()
    parsed=urlsplit(request.url)
    method=request.method
    headers=[(k.encode(),v.encode()) for k,v in request.headers.items()]
    async def dispatch():
        output=[]
        async def receive():return {'type':'http.request','body':body,'more_body':False}
        async def send(value):output.append(value)
        await app({'type':'http','method':method,'path':parsed.path,
            'query_string':parsed.query.encode(), 'headers':headers},receive,send)
        return output[0]['status'],output[1]['body']
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(dispatch())).result(timeout=10)


def _drive_the_reader(tmp_path, *, include_the_tail, inject_server_selected_surprise=False,
                      inject_slow_valid_fallback=False, inject_empty_continuation=False,
                      inject_empty_owner_switch=False, inject_empty_expired_deadline=False,
                      inject_empty_pointerdown=False, inject_saved_race=False,
                      inject_saved_page_race=False, inject_saved_auth_race=False,
                      inject_fast_page_history_failure=False, order_case=None,
                      expected_order_copy=None):
    """The whole reader drive. `include_the_tail` selects everything from the
    saved-navigation step onward, which is the part issue #48 breaks."""
    artifact_dir = Path(os.environ.get('NEWS_CURATOR_QA_OUTPUT_DIR', str(tmp_path)))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    capture=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())
    rows=sorted(capture['rows'],key=lambda r:(r['published_at'],r['story_id']),reverse=True)
    assert len(rows)>250 and len({row['story_id'] for row in rows})==len(rows)
    categories=sorted({category for row in rows for category in row['category_ids']})
    ranked={}
    for category in categories:
        row=next(r for r in rows if category in r['category_ids'])
        ranked[category]=[Item(title=row['title'],url=row['canonical_url'],canonical_url=row['canonical_url'],
            published_at=datetime.fromisoformat(row['published_at']),source_id=row['source_id'],
            source_name=row['source_name'],language=row['language'],description=row['summary'])]
    site=tmp_path/'site'
    render_site(ranked,[],datetime.fromisoformat(capture['generated_at']),site,
        topic_ids_by_name={c:c for c in categories},require_summaries=False,discovery_enabled=True)
    activate_personalization_link(site/'index.html',supabase_url=DATABASE,publishable_key='sb_publishable_localtest',
        m2_config={'enabled':True,'url':RANKER,'policy_version':'test-policy',
        'model_version':'test-model','provider_policy_id':'test-policy','provider_retention_url':'https://policy.example',
        'page_size':25,'request_timeout_ms':8000,'transport_timeout_ms':310000})
    store=LocalStore(rows)
    service=RankingService(auth=LocalAuth(),store=store,adapter=RankLLMAdapter(
        policy=RankerPolicy('test-provider','test-model','https://provider.example','test-prompt'),engine=NoProvider()),
        policy=ServicePolicy('test-policy','test-model','test-policy','test-tenant',enabled=True,preview_owner_ids=(OWNER,)),cursor_key=b'k'*32)
    app=RankingASGI(service=service,reader_origin=READER)
    requests=[]; page_errors=[]; export_mode={'oversized':False}; export_requests=[]
    history_mode={'fail':False, 'fail_after_page':False}
    cursor_mode={'reject_once':False,'rejections':0}
    empty_mode={'remaining':int(inject_empty_continuation or inject_empty_owner_switch
                                or inject_empty_expired_deadline or inject_empty_pointerdown),
                'rank_response':None}
    surprise_mode={'remaining':1 if inject_server_selected_surprise else 0}
    def route_handler(route):
        request=route.request; parsed=urlsplit(request.url); body=request.post_data_json if request.post_data else {}
        requests.append(parsed.path)
        if request.url.startswith(READER):
            if parsed.path=='/auth/client.js':
                script='''window.__localSession={access_token:"local-auth-token",user_id:"'''+OWNER+'''"}; window.NewsCuratorAuth={
                    config:()=>({url:"'''+DATABASE+'''",key:"sb_publishable_localtest"}),
                    sessionForRequest:async()=>window.__localSession, hasSessionCandidate:()=>!!window.__localSession,
                    isConfirmed:()=>true,confirmSession:()=>{},clearSession:()=>{window.__localSession=null;},
                    channelName:"m2-local-test"};'''
                return route.fulfill(status=200,content_type='text/javascript',body=script)
            file=site/(parsed.path.lstrip('/') or 'index.html')
            return route.fulfill(status=200,content_type='text/javascript' if file.suffix=='.js' else 'text/html',body=file.read_bytes())
        if request.url.startswith(RANKER):
            if parsed.path=='/page' and empty_mode['remaining']:
                empty_mode['remaining']-=1
                assert empty_mode['rank_response'] is not None
                pending={**empty_mode['rank_response'],'cards':[]}
                return route.fulfill(status=200,content_type='application/json',body=json.dumps(pending))
            if parsed.path=='/page' and cursor_mode['reject_once']:
                cursor_mode['reject_once']=False;cursor_mode['rejections']+=1
                return route.fulfill(status=409,content_type='application/json',
                    body=json.dumps({'error':'cursor_version'}))
            status,payload=asgi_request(app,request)
            if parsed.path=='/rank' and status==200 and order_case is not None:
                response=json.loads(payload)
                if order_case.startswith('legacy_'):
                    response.pop('order_origin',None)
                else:
                    response['order_origin']=order_case
                if order_case in ('prepared_model','direct_model','legacy_model'):
                    response['result_mode']='model'
                    response['fallback_reason']=''
                payload=json.dumps(response).encode()
            if parsed.path=='/page' and history_mode['fail_after_page']:
                history_mode['fail']=True
            if parsed.path=='/rank' and status==200:
                empty_mode['rank_response']=json.loads(payload)
            # M2 is the source of truth for a ranked response.  A category
            # request may deliberately include a server-selected surprise
            # story that does not carry the locally selected category.
            selected=(body.get('eligibility') or {}).get('category')
            if parsed.path=='/rank' and status==200 and selected and surprise_mode['remaining']:
                response=json.loads(payload)
                if response['cards']:
                    response['cards'][0]['category_ids']=[next(c for c in categories if c!=selected)]
                    payload=json.dumps(response).encode()
                    surprise_mode['remaining']-=1
            return route.fulfill(status=status,content_type='application/json',body=payload)
        if request.url.startswith(DATABASE):
            name=parsed.path.rsplit('/',1)[-1]
            if name=='latest_publication':
                payload={'publication_seq':1,'finalized_at':capture['generated_at'],'initial_history_cursor':None,
                    'page_size':1 if inject_saved_page_race else 20,'poll_seconds':60,
                    'topics':[{'topic_id':c,'name':c} for c in categories]}
            elif name=='m2_history_snapshot':
                if history_mode['fail']:
                    return route.fulfill(status=500,content_type='application/json',body='{}')
                payload=store.history_snapshot('local-auth-token')
            elif name=='append_behavior_event':payload=store.event(body)
            elif name=='set_story_state_with_event':
                sid=body['p_story_id']; current=store.owner_states('',[sid])[sid]
                assert current['state_revision']==body['p_expected_revision']
                current.update(read_at=body['p_occurred_at'] if body['p_read'] else None,
                    saved_at=body['p_occurred_at'] if body['p_saved'] else None,state_revision=current['state_revision']+1)
                store.states[sid]=current
                event=store.event({**body,'p_payload':{'story_id':sid,**({'saved':body['p_saved']} if body['p_event_type']=='save' else {}),'surface':'reader'}})
                payload={'status':'updated','revision':current['state_revision'],'read_at':current['read_at'],
                         'saved_at':current['saved_at'],'behavior_event':event}
            elif name=='set_story_state':
                sid=body['p_story_id']; current=store.owner_states('',[sid])[sid]
                if current['state_revision'] != body['p_expected_revision']:
                    payload={'status':'conflict','revision':current['state_revision']}
                else:
                    current.update(read_at=capture['generated_at'] if body['p_read'] else None,
                        saved_at=capture['generated_at'] if body['p_saved'] else None,state_revision=current['state_revision']+1)
                    store.states[sid]=current
                    payload={'status':'updated','revision':current['state_revision'],
                             'read_at':current['read_at'],'saved_at':current['saved_at']}
            elif name=='set_story_interest_with_event':
                sid=body['p_story_id']; current=store.owner_states('',[sid])[sid]
                signal=body['p_signal']; revision=body['p_expected_revision']+1
                current['interests']=[{'topic_id':body['p_topic_id'],'signal':signal,'revision':revision}];store.states[sid]=current
                event=store.event({**body,'p_event_type':'less_like_this' if signal=='less_like' else 'more_like_this',
                    'p_payload':{'story_id':sid,'topic_id':body['p_topic_id'],'surface':'reader'}})
                payload={'status':'updated','signal':signal,'revision':revision,'behavior_event':event}
            elif name=='set_behavior_consent':
                store.learning=body['p_learning_enabled'];store.provider=body['p_provider_processing_enabled'];store.consent+=1
                payload={'learning_enabled':store.learning,'provider_processing_enabled':store.provider,
                    'provider_policy_id':body['p_provider_policy_id'],'consent_revision':store.consent}
            elif name=='clear_behavior_history':
                store.events=[];store.generation+=1
                payload={'events_deleted':0,'profiles_deleted':0,'history_revision':0,'history_generation':store.generation}
            elif name=='m2_owner_export_page':
                export_requests.append(copy.deepcopy(body))
                export_rows=([{'section':'behavior_events','key':str(index+1).zfill(20),
                    'value':{'controlled_payload':'x'*600000}} for index in range(3)] if export_mode['oversized'] else
                    [{'section':'behavior_events','key':str(index+1).zfill(20),'value':copy.deepcopy(event)}
                        for index,event in enumerate(store.events)])
                offset=int(body['p_cursor'].split('-')[-1]) if body.get('p_cursor') else 0
                fence='a'*64
                assert body.get('p_expected_fence') in (None,fence)
                payload={'schema_version':1,'owner_id':OWNER,'fence':fence,'offset':offset,
                    'total_rows':len(export_rows),'max_download_bytes':1048576,
                    'rows':export_rows[offset:offset+1],
                    'next_cursor':f'cursor-{offset+1}' if offset+1<len(export_rows) else None}
            elif name=='saved_page':
                saved_rows=[]
                for row in rows:
                    state=store.states.get(row['story_id'])
                    if not state or not state['saved_at']:continue
                    saved_rows.append({'story_id':row['story_id'],'canonical_url':row['canonical_url'],
                        'title':row['title'],'summary':row['summary'],'language':row['language'],
                        'published_at':row['published_at'],'publication_seq':0,'position':0,
                        'ordering_mode':'preference_then_freshness','ordering_key':{},'page_order_mode':'saved_at',
                        'next_cursor':{'before_saved_at':state['saved_at'],'before_story_id':row['story_id']},
                        'score_components':{},'topic_ids':row['category_ids'],'topic_ranks':{},
                        'source_kind':row['source_kind'],'source_name':row['source_name'],
                        'ranking_explanation':'Saved story','coverage_mentions':[],**copy.deepcopy(state)})
                before_saved_at=body.get('p_before_saved_at')
                before_story_id=body.get('p_before_story_id')
                if before_saved_at:
                    saved_rows=[row for row in saved_rows
                                if (row['saved_at'],row['story_id']) < (before_saved_at,before_story_id)]
                saved_rows.sort(key=lambda row: (row['saved_at'], row['story_id']), reverse=True)
                payload=saved_rows[:int(body['p_limit'])]
            elif name=='feed_page':payload=[]
            elif name=='discovery_edition':payload={'schema_version':1,'status':'unavailable','reason_code':'no_private_edition','edition':None}
            else:raise AssertionError(name)
            return route.fulfill(status=200,content_type='application/json',body=json.dumps(payload))
        route.abort()
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True,channel='chrome',args=['--mute-audio'])
        context=browser.new_context(viewport={'width':390,'height':844})
        context.add_init_script('''(() => {
            if (window.speechSynthesis) window.speechSynthesis.speak = () => {};
            if (window.HTMLMediaElement) window.HTMLMediaElement.prototype.play = () => Promise.resolve();
            window.__savedAppSettled = 0;
            window.addEventListener("news-curator:saved-request-finished", () => { window.__savedAppSettled += 1; });
        })();''')
        context.add_init_script('''(() => {
            const originalFetch=window.fetch.bind(window);
            window.fetch=(url,options)=>{
              if(window.__stallExport && String(url).endsWith("/m2_owner_export_page"))
                return new Promise((resolve,reject)=>{window.__releaseExport=()=>originalFetch(url,options).then(resolve,reject);});
              if(window.__stallHistory && String(url).endsWith("/m2_history_snapshot"))
                return new Promise((resolve,reject)=>setTimeout(()=>originalFetch(url,options).then(resolve,reject),400));
              if(window.__stallSaved && String(url).endsWith("/saved_page"))
                return new Promise((resolve,reject)=>{window.__releaseSaved=()=>originalFetch(url,options).then(resolve,reject);});
              if(window.__stallState && (String(url).endsWith("/set_story_state_with_event") ||
                  String(url).endsWith("/set_story_state")))
                return new Promise((resolve,reject)=>{
                  window.__stateReleases=window.__stateReleases||[];
                  window.__stateReleases.push(()=>originalFetch(url,options).then(resolve,reject));
                });
              if(window.__holdM2Response && String(url).startsWith("https://ranker.example"))
                return new Promise((resolve,reject)=>{
                  originalFetch(url,options).then(response=>{
                    window.__heldM2Releases=window.__heldM2Releases||[];
                    window.__heldM2Releases.push(()=>resolve(response));
                  },reject);
                });
              return window.__stallM2 && String(url).startsWith("https://ranker.example")
                ?new Promise((resolve,reject)=>{
                    const timer=setTimeout(()=>originalFetch(url,options).then(async(response)=>{
                      const payload=await response.json();
                      if(window.__stallM2ForceModel!==false){payload.result_mode="model";payload.fallback_reason="";payload.order_origin="direct_model";}
                      resolve(new Proxy(response,{get(target,key){
                        return key==="text" ? async()=>JSON.stringify(payload) : Reflect.get(target,key,target);
                      }}));
                    },reject),window.__stallM2Delay||0);
                    options.signal.addEventListener('abort',()=>{clearTimeout(timer);reject(options.signal.reason);},{once:true});
                  })
                :originalFetch(url,options);
            };
        })();''')
        context.route('**/*',route_handler)
        page=context.new_page();page.on('pageerror',lambda error:page_errors.append(str(error)))
        try:
            page.goto(READER + '?silent=1',wait_until='networkidle')
            page.wait_for_function("() => document.querySelectorAll('[data-m2-card=true]').length===25")
            assert '/rank' in requests
            if expected_order_copy is not None:
                assert page.locator('#m2-mode').inner_text() == expected_order_copy
                signal = page.locator('[data-m2-card=true] .signal span').first
                assert signal.inner_text() == expected_order_copy
                rank_calls = requests.count('/rank')
                page.locator('#m2-language-toggle').click()
                assert signal.inner_text() == expected_order_copy
                assert requests.count('/rank') == rank_calls
                assert not page_errors, page_errors
                return
            assert page.locator('#m2-mode').inner_text() == (
                'Freshness order. Model ranking was not used.')
            if inject_saved_race:
                saved_only = rows[-1]['story_id']
                replacement_saved = rows[-2]['story_id']
                for story_id in (saved_only, replacement_saved):
                    store.states.setdefault(story_id, {
                        'read_at': None, 'saved_at': None, 'state_revision': 0, 'interests': []})
                store.states[saved_only]['saved_at'] = capture['generated_at']
                page.evaluate('window.__stallSaved=true')
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.wait_for_function('() => typeof window.__releaseSaved === "function"')
                page.locator('.chip[data-filter="__all__"]:visible').click()
                page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').count()==0
                settled_before = page.evaluate('window.__savedAppSettled')
                page.evaluate('window.__stallSaved=false;window.__releaseSaved()')
                page.wait_for_function('(before) => window.__savedAppSettled > before', arg=settled_before)
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').count()==0
                store.states[saved_only]['saved_at'] = None
                store.states[replacement_saved]['saved_at'] = capture['generated_at']
                saved_requests = requests.count('/rest/v1/rpc/saved_page')
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.wait_for_function('() => document.querySelector(".chip[data-filter=\'__saved__\']").getAttribute("aria-pressed")==="true"')
                page.locator(f'.card:not([hidden])[data-story-id="{replacement_saved}"]').wait_for(state='visible')
                assert page.locator(f'.card:not([hidden])[data-story-id="{replacement_saved}"]').count()==1
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').count()==0
                assert requests.count('/rest/v1/rpc/saved_page') > saved_requests
                assert not page_errors, page_errors
                return
            if inject_saved_page_race:
                saved_only, saved_second, saved_third = (row['story_id'] for row in rows[-3:])
                saved_times = {
                    saved_only: (datetime.fromisoformat(capture['generated_at'])).isoformat(),
                    saved_second: (datetime.fromisoformat(capture['generated_at']) - timedelta(seconds=1)).isoformat(),
                    saved_third: (datetime.fromisoformat(capture['generated_at']) - timedelta(seconds=2)).isoformat(),
                }
                for story_id in (saved_only, saved_second, saved_third):
                    store.states.setdefault(story_id, {
                        'read_at': None, 'saved_at': saved_times[story_id],
                        'state_revision': 0, 'interests': []})
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.wait_for_function('() => document.querySelectorAll(".card:not([hidden])").length===1')
                page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').wait_for(state='visible')
                page.locator('#load-more').click()
                page.locator(f'.card:not([hidden])[data-story-id="{saved_second}"]').wait_for(state='visible')
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').count()==1
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_second}"]').count()==1
                assert requests.count('/rest/v1/rpc/saved_page')==2
                page.locator('.chip[data-filter="__all__"]:visible').click()
                page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.wait_for_function('() => document.querySelectorAll(".card:not([hidden])").length===1')
                page.evaluate('window.__stallSaved=true')
                page.locator('#load-more').click()
                page.wait_for_function('() => typeof window.__releaseSaved === "function"')
                page.locator('.chip[data-filter="__all__"]:visible').click()
                page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
                settled_before = page.evaluate('window.__savedAppSettled')
                page.evaluate('window.__stallSaved=false;window.__releaseSaved()')
                page.wait_for_function('(before) => window.__savedAppSettled > before', arg=settled_before)
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').count()==0
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_second}"]').count()==0
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_third}"]').count()==0
                assert page.locator('[data-m2-card=true]').count()==25
                saved_requests = requests.count('/rest/v1/rpc/saved_page')
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"]').wait_for(state='visible')
                assert requests.count('/rest/v1/rpc/saved_page') > saved_requests
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_second}"]').count()==0
                assert not page_errors, page_errors
                return
            if inject_saved_auth_race:
                saved_only = rows[-1]['story_id']
                store.states.setdefault(saved_only, {
                    'read_at': None, 'saved_at': capture['generated_at'],
                    'state_revision': 0, 'interests': []})
                page.evaluate('window.__stallSaved=true')
                page.locator('.chip[data-filter="__saved__"]:visible').click()
                page.wait_for_function('() => typeof window.__releaseSaved === "function"')
                page.evaluate('window.__localSession=null;window.dispatchEvent(new Event("news-curator:auth-changed"))')
                page.wait_for_function('() => document.querySelector("#m2-controls").hidden')
                settled_before = page.evaluate('window.__savedAppSettled')
                page.evaluate('window.__stallSaved=false;window.__releaseSaved()')
                page.wait_for_function('(before) => window.__savedAppSettled > before', arg=settled_before)
                assert page.locator(f'.card:not([hidden])[data-story-id="{saved_only}"].is-saved').count()==0
                assert not page_errors, page_errors
                return
            if inject_fast_page_history_failure:
                preceding_ids=page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')
                history_mode['fail_after_page']=True
                page.locator('#load-more').click()
                page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading")')
                page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===50')
                assert page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')[:25] == preceding_ids
                assert 'Could not load more' not in page.locator('#m2-mode').inner_text()
                assert not page_errors, page_errors
                return
            if inject_empty_owner_switch or inject_empty_expired_deadline or inject_empty_pointerdown:
                page.evaluate('''(delay) => {
                    window.__stallM2=true;window.__stallM2Delay=delay;
                    window.__stallM2ForceModel=false;
                }''', 8500 if inject_empty_pointerdown else 400)
                with page.expect_request(lambda request:urlsplit(request.url).path=='/page'):
                    page.locator('#load-more').click()
                if inject_empty_pointerdown:
                    page.evaluate('''() => document.body.dispatchEvent(
                        new PointerEvent("pointerdown", {bubbles:true}))''')
                    page.wait_for_function(
                        '() => document.querySelectorAll("[data-m2-card=true]").length===50', timeout=20000)
                    assert requests.count('/page')==2 and not page_errors,page_errors
                    return
                if inject_empty_owner_switch:
                    page.evaluate('window.dispatchEvent(new Event("news-curator:auth-changed"))')
                else:
                    page.evaluate('''() => {
                        const actualNow=Date.now.bind(Date);
                        Date.now=()=>actualNow()+310001;
                    }''')
                page.wait_for_timeout(1000)
                assert empty_mode['remaining']==0
                assert requests.count('/page')==1
                if inject_empty_expired_deadline:
                    assert 'Could not load more' in page.locator('#m2-mode').inner_text()
                    assert page.locator('[data-m2-card=true]').count()==25
                assert not page_errors,page_errors
                return
            if inject_empty_continuation:
                first_ids=page.locator('[data-m2-card=true]').evaluate_all(
                    '(cards)=>cards.map(card=>card.dataset.storyId)')
                page.locator('#load-more').click()
                if inject_empty_continuation > 1:
                    page.wait_for_function('''() => document.querySelector('#m2-mode').textContent
                        .includes('Tap Load more to continue')''', timeout=10000)
                    assert requests.count('/page')==3 and requests.count('/rank')==1
                    assert page.locator('[data-m2-card=true]').count()==25
                    return
                page.wait_for_function(
                    '() => document.querySelectorAll("[data-m2-card=true]").length===50', timeout=10000)
                ids=page.locator('[data-m2-card=true]').evaluate_all(
                    '(cards)=>cards.map(card=>card.dataset.storyId)')
                assert ids[:25]==first_ids and len(set(ids))==50
                assert requests.count('/page')==2 and requests.count('/rank')==1
                assert 'Tap Load more to continue' not in page.locator('#m2-mode').inner_text()
                return
            if inject_slow_valid_fallback:
                page.evaluate('''() => {
                    window.__stallM2=true;
                    window.__stallM2Delay=8500;
                    window.__stallM2ForceModel=false;
                }''')
                with page.expect_request(lambda request: urlsplit(request.url).path == '/rank'):
                    page.evaluate('document.querySelector("#m2-refresh").click()')
                page.wait_for_function('''() =>
                    document.querySelectorAll('[data-m2-card=true]').length===0 &&
                    document.querySelector('#m2-mode').textContent.includes('still loading')''', timeout=12000)
                page.wait_for_function('''() =>
                    document.querySelectorAll('[data-m2-card=true]').length===25 &&
                    document.querySelector('#m2-mode').textContent.includes('Model ranking was not used')''', timeout=15000)
                return
            race_card = page.locator('[data-m2-card=true]').nth(1)
            race_story_id = race_card.get_attribute('data-story-id')
            second_race_card = page.locator('[data-m2-card=true]').nth(2)
            second_race_story_id = second_race_card.get_attribute('data-story-id')
            page.evaluate('''() => {
                window.__stallM2=true;window.__stallM2Delay=1000;window.__stallState=true;window.__stateReleases=[];
                const cards=document.querySelectorAll('[data-m2-card=true]');window.__raceCards=[cards[1],cards[2]];
            }''')
            with page.expect_request(lambda request: urlsplit(request.url).path == '/rank'):
                page.evaluate('document.querySelector("#m2-refresh").click()')
            race_card.locator('.accordion-toggle').click()
            second_race_card.locator('.accordion-toggle').click()
            page.wait_for_function('() => window.__stateReleases?.length===1')
            page.wait_for_timeout(1100)
            page.evaluate('window.__stateReleases[0]()')
            page.wait_for_function('() => window.__stateReleases?.length===2')
            page.wait_for_timeout(100)
            page.evaluate('window.__stallState=false;window.__stateReleases[1]()')
            page.wait_for_function('([first,second]) => [first,second].every(storyId => { const card=document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"]`); return card?.dataset.stateRevision==="1" && card.classList.contains("is-read"); })', arg=[race_story_id, second_race_story_id])
            page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
            # A synchronous language rerender must carry the in-flight guard to
            # the replacement card. Otherwise a second stale-revision save can
            # start while the original RPC is still pending.
            toggle_story_id = race_story_id
            toggle_card = page.locator(f'[data-m2-card=true][data-story-id="{toggle_story_id}"]')
            toggle_card.locator('.accordion-toggle').click()
            page.evaluate('window.__stallState=true;window.__stateReleases=[]')
            toggle_card.locator('.save-action').click()
            page.wait_for_function('() => window.__stateReleases?.length===1')
            page.locator('#m2-language-toggle').click()
            replacement = page.locator(f'[data-m2-card=true][data-story-id="{toggle_story_id}"]')
            assert replacement.locator('.save-action').is_disabled()
            replacement.locator('.save-action').evaluate('(button) => button.click()')
            page.wait_for_timeout(100)
            assert page.evaluate('window.__stateReleases.length') == 1
            page.evaluate('window.__stallState=false;window.__stateReleases[0]()')
            page.wait_for_function('(storyId) => { const card=document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"]`); return Number(card?.dataset.stateRevision)>=2 && card.classList.contains("is-saved") && !card.querySelector(".save-action").disabled; }', arg=toggle_story_id)
            replacement.locator('.accordion-toggle').click()
            replacement.locator('.save-action').click()
            page.wait_for_function('(storyId) => { const card=document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"]`); return Number(card?.dataset.stateRevision)>=3 && !card.classList.contains("is-saved"); }', arg=toggle_story_id)
            # A replacement can arrive with a newer cross-tab revision while a
            # direct mark-unread CAS is stalled. If that CAS conflicts, rollback
            # must keep the newer server baseline instead of restoring revision N.
            page.evaluate('''(storyId) => {
                window.__stallM2=true;window.__stallM2Delay=500;window.__stallState=true;window.__stateReleases=[];
                document.querySelector("#m2-refresh").click();
                document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"] .read-action`).click();
            }''', toggle_story_id)
            page.wait_for_function('() => window.__stateReleases?.length===1')
            authoritative = copy.deepcopy(store.states[toggle_story_id])
            authoritative.update(read_at=capture['generated_at'], state_revision=authoritative['state_revision']+1)
            store.states[toggle_story_id] = authoritative
            authoritative_revision = authoritative['state_revision']
            page.wait_for_function('(args) => Number(document.querySelector(`[data-m2-card=true][data-story-id="${args.storyId}"]`)?.dataset.stateRevision)===args.revision',
                arg={'storyId': toggle_story_id, 'revision': authoritative_revision})
            page.evaluate('window.__stallState=false;window.__stateReleases[0]()')
            page.wait_for_function('(args) => { const card=document.querySelector(`[data-m2-card=true][data-story-id="${args.storyId}"]`); return Number(card?.dataset.stateRevision)===args.revision && card.classList.contains("is-read") && document.querySelector("#reader-status").textContent.includes("could not be saved"); }',
                arg={'storyId': toggle_story_id, 'revision': authoritative_revision})
            page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
            assert page.locator('#discovery-controls').is_hidden()
            assert page.locator('.edition-meta').is_hidden()
            assert page.locator('.eyebrow').text_content()=='Reading feed'
            # Check the same rendered reader at every required mobile/tablet width.
            for width in (320, 390, 430, 768):
                page.set_viewport_size({'width': width, 'height': 844})
                page.screenshot(path=str(artifact_dir / f'reader-m2-{width}-viewport.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.locator('#m2-controls summary').focus()
                page.keyboard.press('Enter')
                assert page.locator('#m2-controls details').get_attribute('open') is not None
                targets = page.locator('#m2-controls button, #m2-controls label, #m2-controls a:visible')
                for box in targets.evaluate_all('(nodes) => nodes.map(node => { const r=node.getBoundingClientRect(); return {width:r.width,height:r.height}; })'):
                    assert box['width'] >= 44 and box['height'] >= 44, (width, box)
                page.locator('#m2-controls summary').click()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#m2-controls summary').click()
            # A tab holding a cursor from the pre-atomic release gets one 409,
            # discards that cursor, and re-ranks onto the current contract.
            rank_before_cursor_upgrade=requests.count('/rank')
            cursor_mode['reject_once']=True
            with page.expect_response(lambda response:urlsplit(response.url).path=='/rank'):
                page.locator('#load-more').click()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            assert cursor_mode['rejections']==1
            assert requests.count('/rank')==rank_before_cursor_upgrade+1
            # The real continuation route must exceed an initial 200-row window.
            for _ in range(9):
                before=page.locator('[data-m2-card=true]').count()
                page.locator('#load-more').click()
                page.wait_for_function('(before)=>document.querySelectorAll("[data-m2-card=true]").length>before',arg=before)
            ids=page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')
            assert len(ids)>200 and len(set(ids))==len(ids)
            card=page.locator('[data-m2-card=true]').first
            saved_story_id=card.get_attribute('data-story-id')
            card.locator('.accordion-toggle').click()
            page.wait_for_function('() => document.querySelector("[data-m2-card=true]").dataset.stateRevision==="1"')
            card.locator('.save-action').click()
            page.wait_for_function('() => document.querySelector("[data-m2-card=true]").dataset.stateRevision==="2"')
            discovery_reads=requests.count('/rest/v1/rpc/discovery_edition')
            rank_reads=requests.count('/rank')
            page.locator('.chip[data-filter="__saved__"]:visible').click()
            page.wait_for_function('() => document.querySelectorAll(".card:not([hidden])").length===1')
            page.reload(wait_until='networkidle')
            assert page.locator('.chip[data-filter="__saved__"]:visible').get_attribute('aria-pressed')=='true'
            assert page.locator('.card:not([hidden])').count()==1
            assert page.locator('.card:not([hidden])').get_attribute('data-story-id')==saved_story_id
            assert requests.count('/rest/v1/rpc/discovery_edition')==discovery_reads
            assert page.locator('#discovery-controls').is_hidden()
            assert page.locator('#load-more').inner_text()=='Load 20 more'
            assert page.locator('#load-more').is_hidden()
            page.locator('.chip[data-filter="__all__"]:visible').click()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            assert requests.count('/rank')>rank_reads
            restored = page.locator(f'[data-m2-card=true][data-story-id="{saved_story_id}"]')
            assert restored.get_attribute('data-state-revision') == '2'
            assert 'is-read' in (restored.get_attribute('class') or '')
            assert 'is-saved' in (restored.get_attribute('class') or '')
            assert page.locator('#load-more').inner_text()=='Load 25 more'
            page.locator('#m2-controls summary').click()
            state_events = [e['event_type'] for e in store.events]
            assert state_events[:4] == ['read_more', 'read_more', 'save', 'save']
            assert state_events[-2:] == ['read_more', 'save']
            page.locator('#load-more').click()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===50')
            ids=page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')
            assert len(ids)==len(set(ids))==50
            assert store.rank_reads[-1][2]==len(store.events)
            # Search hits are real captured publisher titles; no fabricated news.
            query=next(row['title'] for row in rows if row['language']=='zh')[:6]
            page.locator('#q').fill(query)
            page.wait_for_function('(q)=>Array.from(document.querySelectorAll("[data-m2-card=true]")).some(c=>c.dataset.m2Query===q.toLowerCase())',arg=query)
            assert any(e['event_type']=='search_query' and e['payload']['query']==query for e in store.events)
            assert store.rank_reads[-1][2]==len(store.events)
            search_card=page.locator('[data-m2-card=true]').first
            with page.expect_response(lambda response:response.url.endswith('/append_behavior_event') and
                    response.request.post_data_json.get('p_event_type')=='search_result_click'):
                search_card.locator('.accordion-toggle').click()
            with page.expect_response(lambda response:response.url.endswith('/append_behavior_event') and
                    response.request.post_data_json.get('p_event_type')=='open_original'):
                with page.expect_popup() as opened:
                    search_card.locator('.acts a').first.click()
                opened.value.close()
            assert any(event['event_type']=='open_original' for event in store.events)
            with page.expect_response(lambda response: response.url.endswith('/append_behavior_event') and
                    response.request.post_data_json.get('p_event_type')=='search_zero_results'):
                page.locator('#q').fill('___local_no_match___')
                page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("No matching stories")')
            assert any(e['event_type']=='search_zero_results' for e in store.events)
            page.locator('#q').fill('')
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            category=categories[0]
            if inject_server_selected_surprise:
                page.evaluate('window.__stallM2=true;window.__stallM2Delay=500')
                with page.expect_response(lambda response:urlsplit(response.url).path=='/rank'):
                    page.locator(f'.mobiletopics .chip[data-topic-id="{category}"]').click()
                    page.locator('#m2-language-toggle').click()
                    page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]:not([hidden])").length===0')
                page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
                page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25 && document.querySelectorAll("[data-m2-card=true]:not([hidden])").length===25')
                assert page.locator(f'[data-m2-card=true]:not([data-topic-api-ids~="{category}"])').count()==1
                return
            with page.expect_response(lambda response:urlsplit(response.url).path=='/rank'):
                page.locator(f'.mobiletopics .chip[data-topic-id="{category}"]').click()
            page.wait_for_function('(category)=>document.querySelectorAll("[data-m2-card=true]").length>0 && Array.from(document.querySelectorAll("[data-m2-card=true]")).every(c=>c.dataset.topicApiIds.split(" ").includes(category))',arg=category)
            assert store.rank_reads[-1][0]==category
            card=page.locator('[data-m2-card=true]').first
            clicked_story_id=card.get_attribute('data-story-id')
            # Capture a pre-feedback server response, then hold it at the
            # browser boundary. Its arrival must not repaint the hidden story.
            page.evaluate('window.__holdM2Response=true;window.__heldM2Releases=[]')
            with page.expect_request(lambda request:urlsplit(request.url).path=='/rank'):
                page.evaluate('document.querySelector("#m2-refresh").click()')
            page.wait_for_function('() => window.__heldM2Releases.length===1')
            card.locator('.accordion-toggle').click()
            with page.expect_response(lambda response:response.url.endswith('/set_story_interest_with_event')):
                card.locator('.less-interest-action').click()
            page.wait_for_function(
                '(story_id) => !Array.from(document.querySelectorAll("[data-m2-card=true]")).some(card => card.dataset.storyId === story_id)',
                arg=clicked_story_id)
            page.evaluate('window.__holdM2Response=false;window.__heldM2Releases[0]()')
            page.wait_for_timeout(200)
            assert page.locator(f'[data-m2-card=true][data-story-id="{clicked_story_id}"]').count()==0
            page.locator('#m2-language-toggle').click()
            assert page.locator(f'[data-m2-card=true][data-story-id="{clicked_story_id}"]').count()==0
            assert page.locator('[data-m2-card=true]').count() >= 1
            assert any(event['event_type']=='less_like_this' for event in store.events)
            with page.expect_download() as download_info:
                page.locator('#m2-download-data').click()
            export_file=tmp_path/'owner-export.json';download_info.value.save_as(export_file)
            exported=json.loads(export_file.read_text())
            assert exported['owner_id']==OWNER and exported['total_rows']==len(store.events)
            assert len(exported['rows'])==len(store.events)>1
            assert requests.count('/rest/v1/rpc/m2_owner_export_page')>=3
            completed_downloads=[];page.on('download',lambda download:completed_downloads.append(download))
            export_mode['oversized']=True;export_requests.clear()
            page.locator('#m2-download-data').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("could not be downloaded")')
            assert len(export_requests)==2 and not completed_downloads
            export_mode['oversized']=False
            page.evaluate('window.__stallExport=true')
            page.locator('#m2-download-data').click()
            page.wait_for_function('() => typeof window.__releaseExport === "function"')
            page.locator('#m2-clear-history').click()
            page.evaluate('window.__stallExport=false;window.__releaseExport()')
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length>0 && document.querySelector("#reader-status").textContent.includes("stories loaded")')
            assert store.generation==2 and not store.events
            assert not completed_downloads
            page.locator('#m2-local-learning').uncheck()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length>0 && !document.querySelector("#m2-local-learning").checked')
            assert store.learning is False
            screenshot=artifact_dir / 'reader-m2-390.png'
            page.screenshot(path=str(screenshot),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=390')
            # A delayed original request shows public cards by the visible deadline,
            # then applies its model result before the longer transport deadline.
            page.evaluate('''() => {
                const timeout=AbortSignal.timeout.bind(AbortSignal);
                window.__m2Timeouts=[];
                AbortSignal.timeout=(ms)=>{window.__m2Timeouts.push(ms);return timeout(ms===310000?(window.__transportDeadline||300):ms);};
                const later=window.setTimeout.bind(window);
                window.setTimeout=(fn,ms,...args)=>later(fn,ms===8000?(window.__visibleDeadline||30):ms===310000?(window.__transportDeadline||300):ms,...args);
                window.__stallM2=true;window.__stallM2Delay=120;
                const policy=document.querySelector("#m2-provider-retention");policy.hidden=true;policy.removeAttribute("href");
            }''')
            store.learning=True;store.provider=True
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading") && document.querySelectorAll("[data-m2-card=true]").length===0')
            assert page.evaluate('window.__m2Timeouts.includes(310000)')
            assert page.locator('#m2-local-learning').is_checked()
            assert page.locator('#m2-provider-processing').is_checked()
            assert page.locator('#m2-local-learning').is_enabled()
            assert page.locator('#m2-provider-processing').is_enabled()
            assert page.locator('#m2-provider-retention').get_attribute('href')=='https://policy.example'
            assert page.locator('#m2-provider-retention').is_visible()
            assert page.locator('.card:not([hidden])').count()>0
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            assert 'Ranked using' in page.locator('#m2-mode').inner_text()
            # A real mark-unread click advances the interaction epoch. The
            # delayed pre-click response must be discarded instead of restoring
            # its older read state after the visible loading fallback.
            unread = page.locator('[data-m2-card=true]').first
            unread_story_id = unread.get_attribute('data-story-id')
            unread.locator('.accordion-toggle').click()
            page.wait_for_function('(storyId) => document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"]`)?.classList.contains("is-read")', arg=unread_story_id)
            page.evaluate('''(storyId) => {
                window.__visibleDeadline=200;window.__transportDeadline=1000;window.__stallM2=true;window.__stallM2Delay=400;
                document.querySelector("#m2-refresh").click();
                const button=document.querySelector(`[data-m2-card=true][data-story-id="${storyId}"] .read-action`);
                button.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true}));button.click();
            }''', unread_story_id)
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading") && document.querySelectorAll("[data-m2-card=true]").length===0')
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("did not finish") && document.querySelectorAll("[data-m2-card=true]").length===0')
            assert store.states[unread_story_id]['read_at'] is None
            page.evaluate('window.__visibleDeadline=0;window.__transportDeadline=0;window.__stallM2=false;window.__stallM2Delay=0')
            # ---- the tail, from here on, depends on leaving the personalized
            # feed. Issue #48. Split out so the 46 assertions above stay
            # gating independently within the full reader drive.
            if not include_the_tail:
                return
            # Saved navigation makes M2 ineligible while this original request is
            # still delayed. Its timers must not mutate the selected surface.
            page.evaluate('window.__stallM2=true;window.__stallM2Delay=120')
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading") && document.querySelectorAll("[data-m2-card=true]").length===0')
            page.locator('.chip[data-filter="__saved__"]:visible').click()
            page.wait_for_function("() => document.querySelector(\".chip[data-filter='__saved__']\").getAttribute('aria-pressed')==='true' && document.querySelectorAll('.card:not([hidden])').length===1")
            saved_surface=page.evaluate('''() => ({
                status:document.querySelector("#reader-status").textContent,
                controlsHidden:document.querySelector("#m2-controls").hidden,
                visibleCards:document.querySelectorAll(".card:not([hidden])").length,
              })''')
            page.wait_for_timeout(350)
            assert page.locator('.chip[data-filter="__saved__"]:visible').get_attribute('aria-pressed')=='true'
            assert page.evaluate('''() => ({
                status:document.querySelector("#reader-status").textContent,
                controlsHidden:document.querySelector("#m2-controls").hidden,
                visibleCards:document.querySelectorAll(".card:not([hidden])").length,
              })''')==saved_surface
            page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
            page.locator('.chip[data-filter="__all__"]:visible').click()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            preceding_ids=page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')
            page.locator('#load-more').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading")')
            assert page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')==preceding_ids
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===50')
            page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
            # History is deliberately slower than the full transport deadline.
            # The terminal public state proves the deadline begins before fetch().
            page.evaluate('window.__stallHistory=true')
            rank_before=requests.count('/rank')
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("did not finish")')
            assert page.locator('[data-m2-card=true]').count()==0
            assert page.locator('.edition-meta').is_visible()
            assert requests.count('/rank')==rank_before
            page.wait_for_timeout(150)
            assert requests.count('/rank')==rank_before
            page.evaluate('window.__stallHistory=false')
            # A real public-card interaction changes queued learning history.
            # The delayed original result must remain discarded.
            page.evaluate('window.__stallM2=true;window.__stallM2Delay=120')
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading") && document.querySelectorAll("[data-m2-card=true]").length===0')
            page.locator('.card:not([hidden]) .accordion-toggle').first.click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("did not finish")')
            assert page.locator('[data-m2-card=true]').count()==0
            page.evaluate('window.__stallM2=false;window.__stallM2Delay=0')
            history_mode['fail']=True
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#m2-local-learning").indeterminate')
            assert page.locator('#m2-local-learning').is_disabled()
            assert page.locator('#m2-provider-processing').is_disabled()
            history_mode['fail']=False
            page.evaluate('window.__stallExport=true;document.querySelector("#m2-download-data").click()')
            page.wait_for_function('() => typeof window.__releaseExport === "function"')
            page.evaluate('window.__localSession=null;window.dispatchEvent(new Event("news-curator:auth-changed"))')
            page.evaluate('window.__stallExport=false;window.__releaseExport()')
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===0')
            assert page.locator('#m2-controls').is_hidden()
            assert not completed_downloads
            page.evaluate('window.__localSession={access_token:"local-auth-token",user_id:"'+OWNER+'"};window.dispatchEvent(new Event("news-curator:auth-changed"))')
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length>0')
            assert page.locator('#m2-controls').is_visible()
            assert page.locator('#discovery-controls').is_hidden()
            assert not page_errors,page_errors
        except BaseException:
            print({"reader_status":page.locator('#reader-status').inner_text(), "page_errors":page_errors,
                   "requests":requests[-12:], "remote_cards":page.locator('[data-m2-card=true]').count()})
            raise
        finally:context.close();browser.close()


def test_real_capture_reader_dispatch_actions_search_and_epochs(tmp_path):
    """GATING. Everything up to the point issue #48 breaks: first render, the
    real continuation route past 200 rows, save and read dispatch, the saved
    surface across a reload, search and its four event types, category
    selection, less-like-this, the owner export including the oversized refusal
    and the clear-history race, and the delayed-model-result deadlines."""
    _drive_the_reader(tmp_path, include_the_tail=False)


def test_m2_category_displays_server_selected_surprise_story(tmp_path):
    """A ranked category response owns its full membership, including surprise."""
    _drive_the_reader(tmp_path, include_the_tail=False, inject_server_selected_surprise=True)


def test_slow_valid_server_fallback_renders_after_loading_threshold(tmp_path):
    """A valid server fallback remains usable even when it arrives after the loading threshold."""
    _drive_the_reader(tmp_path, include_the_tail=False, inject_slow_valid_fallback=True)


def test_one_load_more_automatically_skips_bounded_empty_continuation(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=False, inject_empty_continuation=True)


def test_empty_continuation_retries_stop_at_configured_bound(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=False, inject_empty_continuation=4)


def test_empty_continuation_does_not_retry_after_account_epoch_change(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=False, inject_empty_owner_switch=True)


def test_empty_continuation_does_not_retry_after_operation_deadline(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=False, inject_empty_expired_deadline=True)


def test_empty_continuation_keeps_loading_after_unrelated_pointerdown(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=False, inject_empty_pointerdown=True)


def test_saved_response_arriving_after_m2_activation_is_discarded(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=True, inject_saved_race=True)


def test_saved_page_two_response_after_m2_activation_is_discarded(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=True, inject_saved_page_race=True)


def test_saved_response_after_auth_epoch_change_is_discarded(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=True, inject_saved_auth_race=True)


def test_fast_continuation_does_not_require_a_second_history_snapshot(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=True, inject_fast_page_history_failure=True)


def test_reader_surfaces_after_leaving_the_personalized_feed(tmp_path):
    _drive_the_reader(tmp_path, include_the_tail=True)


@pytest.mark.parametrize("origin,copy", [
    ("recipe", "Your reading mix, ordered by feed rules. Model ranking was not used for this view."),
    ("prepared_model", "Uses a model order prepared earlier; new stories follow feed rules."),
    ("direct_model", "Ranked using this request’s query and permitted reading history."),
    ("freshness", "Freshness order. Model ranking was not used."),
    ("legacy_fallback", "Reading order. Model ranking was not used."),
    ("legacy_model", "Ranked using the model."),
])
def test_order_origin_copy_is_exact_for_each_reader_branch(tmp_path, origin, copy):
    _drive_the_reader(tmp_path, include_the_tail=False,
                      order_case=origin, expected_order_copy=copy)
