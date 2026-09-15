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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from curator.models import Item
from curator.render import render_site, configure_m2_reader
from curator.recommendation.service import RankingService, ServicePolicy
from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
from curator.recommendation.asgi import RankingASGI
from scripts.build_auth_callback import activate_personalization_link

playwright = pytest.importorskip('playwright.sync_api')
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


def test_real_capture_reader_dispatch_actions_search_and_epochs(tmp_path):
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
    (site/'data').mkdir(exist_ok=True)
    for locale in ('en','zh'):
        projection={'schema_version':1,'generated_at':capture['generated_at'],'language':locale,'categories':[]}
        for category in categories:
            projection['categories'].append({'id':category,'name':category if locale=='en' else f'中文 {category}',
                'items':[{'story_id':row['story_id'],'title':row['title'] if locale=='en' else f'中文 {row["title"]}',
                    'description':row['summary'] if locale=='en' else f'中文 {row["summary"]}',
                    'display_language':locale,'translation_available':True}
                    for row in rows if category in row['category_ids']]})
        (site/'data'/f'news-{locale}.json').write_text(json.dumps(projection),encoding='utf-8')
    activate_personalization_link(site/'index.html',supabase_url=DATABASE,publishable_key='sb_publishable_localtest',
        m2_config={'enabled':True,'url':RANKER,'policy_version':'test-policy',
        'model_version':'test-model','provider_policy_id':'test-policy','provider_retention_url':'https://policy.example',
        'page_size':25,'request_timeout_ms':8000,'transport_timeout_ms':20000})
    store=LocalStore(rows)
    service=RankingService(auth=LocalAuth(),store=store,adapter=RankLLMAdapter(
        policy=RankerPolicy('test-provider','test-model','https://provider.example','test-prompt'),engine=NoProvider()),
        policy=ServicePolicy('test-policy','test-model','test-policy','test-tenant',enabled=True),cursor_key=b'k'*32)
    app=RankingASGI(service=service,reader_origin=READER)
    requests=[]; page_errors=[]; export_mode={'oversized':False}; export_requests=[]; history_mode={'fail':False}
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
            status,payload=asgi_request(app,request)
            return route.fulfill(status=status,content_type='application/json',body=payload)
        if request.url.startswith(DATABASE):
            name=parsed.path.rsplit('/',1)[-1]
            if name=='latest_publication':
                payload={'publication_seq':1,'finalized_at':capture['generated_at'],'initial_history_cursor':None,
                    'page_size':20,'poll_seconds':60,'topics':[{'topic_id':c,'name':c} for c in categories]}
            elif name=='m2_history_snapshot':
                if history_mode['fail']:
                    return route.fulfill(status=500,content_type='application/json',body='{}')
                payload=store.history_snapshot('local-auth-token')
            elif name=='m2_localized_story_text':
                locale=body['p_locale']; selected=set(body['p_story_ids'])
                payload=[{'story_id':row['story_id'],'title':row['title'] if locale=='en' else f'中文 {row["title"]}',
                    'summary':row['summary'] if locale=='en' else f'中文 {row["summary"]}',
                    'display_language':locale,'translation_available':True} for row in rows if row['story_id'] in selected]
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
                payload=[]
                for row in rows:
                    state=store.states.get(row['story_id'])
                    if not state or not state['saved_at']:continue
                    payload.append({'story_id':row['story_id'],'canonical_url':row['canonical_url'],
                        'title':row['title'],'summary':row['summary'],'language':row['language'],
                        'published_at':row['published_at'],'publication_seq':0,'position':0,
                        'ordering_mode':'preference_then_freshness','ordering_key':{},'page_order_mode':'saved_at',
                        'next_cursor':{'before_saved_at':state['saved_at'],'before_story_id':row['story_id']},
                        'score_components':{},'topic_ids':row['category_ids'],'topic_ranks':{},
                        'source_kind':row['source_kind'],'source_name':row['source_name'],
                        'ranking_explanation':'Saved story','coverage_mentions':[],**copy.deepcopy(state)})
            elif name=='feed_page':payload=[]
            elif name=='discovery_edition':payload={'schema_version':1,'status':'unavailable','reason_code':'no_private_edition','edition':None}
            else:raise AssertionError(name)
            return route.fulfill(status=200,content_type='application/json',body=json.dumps(payload))
        route.abort()
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True,channel='chrome',args=['--mute-audio'])
        context=browser.new_context(viewport={'width':390,'height':844})
        context.add_init_script('''(() => {
            const originalFetch=window.fetch.bind(window);
            window.fetch=(url,options)=>{
              const requestUrl=typeof url==="string"?url:url.url;
              if(window.__stallState && requestUrl.includes("/set_story_state_with_event"))
                return new Promise((resolve,reject)=>{window.__releaseState=()=>originalFetch(url,options).then(resolve,reject);});
              if(window.__stallExport && String(url).endsWith("/m2_owner_export_page"))
                return new Promise((resolve,reject)=>{window.__releaseExport=()=>originalFetch(url,options).then(resolve,reject);});
              if(window.__stallHistory && String(url).endsWith("/m2_history_snapshot"))
                return new Promise((resolve,reject)=>setTimeout(()=>originalFetch(url,options).then(resolve,reject),400));
              return window.__stallM2 && String(url).startsWith("https://ranker.example")
                ?new Promise((resolve,reject)=>{
                    const timer=setTimeout(()=>originalFetch(url,options).then(async(response)=>{
                      const payload=await response.json();
                      payload.result_mode="model";payload.fallback_reason="";
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
            page.goto(READER,wait_until='networkidle')
            page.wait_for_function("() => document.querySelectorAll('[data-m2-card=true]').length===25")
            assert '/rank' in requests and 'Freshness order' in page.locator('#m2-mode').inner_text()
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
            page.evaluate('window.__stallState=true')
            card.locator('.save-action').click()
            assert card.locator('.save-action').inner_text()=='Saving…'
            assert card.locator('.save-action').get_attribute('aria-busy')=='true'
            assert card.locator('.save-action').get_attribute('aria-pressed')=='false'
            page.wait_for_function('() => typeof window.__releaseState==="function"')
            page.evaluate('window.__stallState=false;window.__releaseState()')
            page.wait_for_function('() => document.querySelector("[data-m2-card=true]").dataset.stateRevision==="2"')
            assert card.locator('.save-action').inner_text()=='Saved ✓'
            assert card.locator('.save-action').get_attribute('aria-label')=='Remove from Saved'
            assert card.locator('.save-action').get_attribute('aria-pressed')=='true'
            assert card.locator('.save-action').get_attribute('aria-busy') is None
            assert page.locator('#reader-status').inner_text()=='Saved. You can find it in Saved.'
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
            assert page.locator('#load-more').inner_text()=='Load 25 more'
            page.locator('#m2-controls summary').click()
            assert [e['event_type'] for e in store.events[:2]]==['read_more','save']
            page.locator('#load-more').click()
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===50')
            ids=page.locator('[data-m2-card=true]').evaluate_all('(cards)=>cards.map(card=>card.dataset.storyId)')
            assert len(ids)==len(set(ids))==50
            assert store.rank_reads[-1][2]==2
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
            page.locator(f'.mobiletopics .chip[data-topic-id="{category}"]').click()
            page.wait_for_function('(category)=>document.querySelectorAll("[data-m2-card=true]").length>0 && Array.from(document.querySelectorAll("[data-m2-card=true]")).every(c=>c.dataset.topicApiIds.split(" ").includes(category))',arg=category)
            assert store.rank_reads[-1][0]==category
            card=page.locator('[data-m2-card=true]').first
            card.locator('.accordion-toggle').click()
            with page.expect_response(lambda response:response.url.endswith('/set_story_interest_with_event')):
                card.locator('.less-interest-action').click()
            page.wait_for_function('() => document.querySelector("[data-m2-card=true] .less-interest-action").getAttribute("aria-pressed")==="true"')
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
                AbortSignal.timeout=(ms)=>{window.__m2Timeouts.push(ms);return timeout(ms===20000?300:ms);};
                const later=window.setTimeout.bind(window);
                window.setTimeout=(fn,ms,...args)=>later(fn,ms===8000?30:ms===20000?300:ms,...args);
                window.__stallM2=true;window.__stallM2Delay=120;
                const policy=document.querySelector("#m2-provider-retention");policy.hidden=true;policy.removeAttribute("href");
            }''')
            store.learning=True;store.provider=True
            page.locator('#m2-refresh').click()
            page.wait_for_function('() => document.querySelector("#reader-status").textContent.includes("Personalized feed is still loading") && document.querySelectorAll("[data-m2-card=true]").length===0')
            assert page.evaluate('window.__m2Timeouts.includes(20000)')
            assert page.locator('#m2-local-learning').is_checked()
            assert page.locator('#m2-provider-processing').is_checked()
            assert page.locator('#m2-local-learning').is_enabled()
            assert page.locator('#m2-provider-processing').is_enabled()
            assert page.locator('#m2-provider-retention').get_attribute('href')=='https://policy.example'
            assert page.locator('#m2-provider-retention').is_visible()
            assert page.locator('.card:not([hidden])').count()>0
            page.wait_for_function('() => document.querySelectorAll("[data-m2-card=true]").length===25')
            assert 'Ranked using' in page.locator('#m2-mode').inner_text()
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
            page.evaluate('document.querySelector("#load-more").click()')
            pending=page.evaluate('''() => ({
                label:document.querySelector("#load-more").textContent,
                busy:document.querySelector("#load-more").getAttribute("aria-busy"),
                disabled:document.querySelector("#load-more").disabled,
                status:document.querySelector("#reader-status").textContent,
              })''')
            assert pending=={'label':'Loading 25 more…','busy':'true','disabled':True,
                'status':'Loading 25 more stories…'}
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
