"""Provider-boundary tests. Captured public candidates, no external model calls."""
import json
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import pytest
import yaml
from curator.contracts.enums import ActorKind, EventType, M2HistoryEventType
from curator.contracts.ranking_request import (AuthenticatedOwner, ModelRankingInput, OrderedHistoryEvent,
    RankingCandidate, RankingRequest)
from curator.recommendation.engine import OpenAIRankLLMEngine
from curator.recommendation.async_provider import (ProviderHTTPError, ProviderResponseInvalid,
    ProviderTimeout, ProviderTransportFailure, exact_order_schema as sent_schema)

ROOT=Path(__file__).resolve().parents[1]

class InstrumentedPrompt:
    def __init__(self):self.calls=0
    def create_prompt(self, *, query, passages):
        self.calls+=1
        return [{'role':'system','content':'Local protocol test, not model quality proof.'},
                {'role':'user','content':query+'\n'+'\n'.join(passages)}]


def captured_input():
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:200]
    candidates=tuple(RankingCandidate(row['story_id'],row['story_id'],row['story_id'],row['title'],
        row['summary'],row['source_id'],row['language'],datetime.fromisoformat(row['published_at'])) for row in rows)
    return ModelRankingInput(None,candidates,(),0,0,1,1,'test-policy','test-model')


def test_prompt_includes_fixture_publication_times_and_current_query_precedence():
    model_input=captured_input()
    older, newer=model_input.candidates[-1],model_input.candidates[0]
    history=(OrderedHistoryEvent('event-query',M2HistoryEventType.SEARCH_QUERY,newer.published_at,1,
        query_text='fashion week'),)
    prompt=InstrumentedPrompt()
    engine=OpenAIRankLLMEngine(prompt_builder=prompt,endpoint='https://provider.example/v1',api_key='unused',
        model='test-model',maximum_output_tokens=4096,reasoning_effort='minimal',verbosity='low',
        client_factory=lambda:pytest.fail('provider must not be called'))
    prepared=engine.prepare(ModelRankingInput('bitcoin',(older,newer),history,1,1,1,1,'test-policy','test-model'))
    prompt_text=prepared.prompt[-1]['content']
    assert f'Published: {older.published_at.isoformat()}' in prompt_text
    assert f'Published: {newer.published_at.isoformat()}' in prompt_text
    assert prompt_text.index(older.published_at.isoformat()) < prompt_text.index(newer.published_at.isoformat())
    assert 'The current query is the primary intent.' in prompt_text
    assert 'only to personalize among candidates relevant to the current query' in prompt_text
    assert 'preserving explicit negative feedback constraints' in prompt_text
    assert 'Current query: bitcoin' in prompt_text and '"query":"fashion week"' in prompt_text


def test_fifty_candidate_preparation_reserves_schema_and_sends_identical_prompt():
    policy=yaml.safe_load((ROOT/'config/ranker-policy-r1.yaml').read_text())
    prompt=InstrumentedPrompt(); sent=[]
    def respond(request):
        body=json.loads(request.content);sent.append(body)
        return httpx.Response(200,json={'id':'local-provider-response','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'order':list(range(1,51))},separators=(',',':'))}]}],
            'usage':{'input_tokens':1000,'output_tokens':1600}})
    engine=OpenAIRankLLMEngine(prompt_builder=prompt,endpoint='https://provider.example/v1',api_key='local-test',
        model='test-model',maximum_output_tokens=policy['maximum_output_tokens'],reasoning_effort=policy['reasoning_effort'],
        verbosity=policy['verbosity'], reasoning_token_allowance=policy['reasoning_token_allowance'],
        prompt_framing_token_allowance=policy['prompt_framing_token_allowance'],
        client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    model_input=ModelRankingInput(None,captured_input().candidates[:50],(),0,0,1,1,'test-policy','test-model')
    prepared=engine.prepare(model_input)
    assert prepared.output_tokens_budget==policy['maximum_output_tokens']
    assert prepared.output_tokens_budget >= len(json.dumps({'order':list(range(1,51))},separators=(',',':')).encode()) + policy['reasoning_token_allowance']
    envelope={'input':prepared.prompt,'text':{'format':sent_schema(50)}}
    assert prepared.input_tokens_bound==len(json.dumps(envelope,ensure_ascii=False,separators=(',',':')).encode()) + policy['prompt_framing_token_allowance'] + len(prepared.prompt)*policy['prompt_framing_tokens_per_message']
    outcome=engine.rerank_prepared(prepared,timeout_seconds=6)
    assert prompt.calls==1
    assert sent[0]['input']==prepared.prompt
    assert sent[0]['text']['format']==sent_schema(50)
    assert sent[0]['max_output_tokens']==prepared.output_tokens_budget
    assert outcome.ranked_candidate_ids==tuple(c.candidate_id for c in model_input.candidates)
    assert outcome.output_tokens==1600


def test_small_output_cap_rejects_before_prompt_or_provider_dispatch():
    prompt=InstrumentedPrompt()
    engine=OpenAIRankLLMEngine(prompt_builder=prompt,endpoint='https://provider.example/v1',api_key='local-test',
        model='test-model',maximum_output_tokens=128,reasoning_effort='minimal',verbosity='low',
        client_factory=lambda:pytest.fail('provider must not be constructed'))
    with pytest.raises(ValueError,match='cannot accommodate'):
        engine.prepare(captured_input())
    assert prompt.calls==0


def test_successful_usage_reconciles_cost_and_keeps_private_execution_out_of_response():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:2]
    class LocalStore:
        def history_snapshot(self, token):
            return dict(history_revision=0,included_history_revision=0,history_generation=1,consent_revision=1,
                learning_enabled=True,provider_processing_enabled=True,provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): self.reserved=kwargs['amount_usd'];return True
        def settle_budget(self, **kwargs): self.settled=kwargs['actual_usd']
        def owner_states(self, token, story_ids):return {}
        def save_frozen_order(self, **kwargs):self.frozen=kwargs;return 'local-frozen'
    class Auth:
        def get_user(self, token):return {'id':'local-owner'}
    def respond(request):
        return httpx.Response(200,json={'id':'local-provider-response','output':[{'type':'message','content':[
            {'type':'output_text','text':'{"order":[2,1]}'}]}],'usage':{'input_tokens':100,'output_tokens':20}})
    engine=OpenAIRankLLMEngine(prompt_builder=InstrumentedPrompt(),endpoint='https://provider.example/v1',
        api_key='local-protocol-only',model='test-model',maximum_output_tokens=4096,
        reasoning_effort='minimal',verbosity='low',client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=engine)
    store=LocalStore()
    service=RankingService(auth=Auth(),store=store,adapter=adapter,
        policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,server_commit_revision=0,
        history_generation=1,consent_revision=1,policy_version='test-policy',model_version='test-model'))
    assert response['result_mode']=='model'
    assert store.settled==pytest.approx(.000065) and store.settled<store.reserved
    assert store.frozen['bindings']['execution']['cost_basis']=='observed_with_unknown_attempt_reserves'
    assert store.frozen['bindings']['execution']['input_tokens']==100
    assert store.frozen['bindings']['execution']['history_events_included']==0
    assert store.frozen['bindings']['execution']['history_events_omitted']==0
    assert not {'execution','eligibility','corpus_cursor','corpus_start','corpus_has_more','excluded_story_ids'} & response.keys()


def test_invalid_permutation_still_settles_reported_usage():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:2]
    class Store:
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=1,learning_enabled=True,provider_processing_enabled=True,
            provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): self.reserved=kwargs['amount_usd']; return True
        def settle_budget(self, **kwargs): self.settlement=kwargs
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Auth:
        def get_user(self, token): return {'id':'local-owner'}
    def respond(request):
        return httpx.Response(200,json={'id':'charged-invalid','output':[{'type':'message','content':[
            {'type':'output_text','text':'{"order":[1,1]}'}]}],'usage':{'input_tokens':100,'output_tokens':20}})
    engine=OpenAIRankLLMEngine(prompt_builder=InstrumentedPrompt(),endpoint='https://provider.example/v1',
        api_key='local-protocol-only',model='test-model',maximum_output_tokens=4096,
        reasoning_effort='minimal',verbosity='low',client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=engine)
    store=Store(); service=RankingService(auth=Auth(),store=store,adapter=adapter,
        policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,server_commit_revision=0,
        history_generation=1,consent_revision=1))
    assert response['result_mode']=='fallback' and response['fallback_reason']=='invalid_provider_permutation'
    assert store.settlement['actual_usd']==pytest.approx(.000065)
    assert store.settlement['status']=='settled'
    assert store.frozen['bindings']['execution']['cost_basis']=='observed_with_unknown_attempt_reserves'


@pytest.mark.parametrize(("provider_error", "expected_reason"), [
    (RuntimeError("private-unknown"), "provider_failure"),
    (ProviderTimeout("private-timeout"), "provider_deadline"),
    (ProviderHTTPError("provider_http_4xx"), "provider_http_4xx"),
    (ProviderTransportFailure("private-transport"), "provider_transport_failure"),
    (ProviderResponseInvalid("private-response"), "provider_response_invalid"),
])
def test_ambiguous_provider_failure_keeps_reservation_unsettled(provider_error, expected_reason):
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:1]
    class Store:
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=1,learning_enabled=True,provider_processing_enabled=True,
            provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): return True
        def settle_budget(self, **kwargs): pytest.fail('ambiguous charge must remain reserved')
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Engine:
        def prepare(self, model_input): return type('Prepared',(),{'input_tokens_bound':100,'output_tokens_budget':100})()
        def rerank_prepared(self, prepared, timeout_seconds): raise provider_error
    class Auth:
        def get_user(self, token): return {'id':'local-owner'}
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=Engine())
    store=Store(); service=RankingService(auth=Auth(),store=store,adapter=adapter,
        policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,server_commit_revision=0,
        history_generation=1,consent_revision=1))
    assert response['fallback_reason']==expected_reason
    assert 'private' not in response['fallback_reason']
    assert store.frozen['bindings']['execution']['attempts_started']==1
    assert store.frozen['bindings']['execution']['cost_basis']=='unknown_provider_charge_reserved'


@pytest.mark.parametrize(("failure", "expected_reason"), [
    ("http_4xx", "provider_http_4xx"),
    ("http_5xx", "provider_http_5xx"),
    ("transport", "provider_transport_failure"),
    ("timeout", "provider_deadline"),
])
def test_real_engine_preserves_safe_provider_category_and_unknown_charge(failure, expected_reason):
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:1]
    class Store:
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=1,learning_enabled=True,provider_processing_enabled=True,
            provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): return True
        def settle_budget(self, **kwargs): pytest.fail('unknown charge must remain reserved')
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Auth:
        def get_user(self, token): return {'id':'local-owner'}
    def respond(request):
        if failure=='transport': raise httpx.ConnectError('private-transport',request=request)
        if failure=='timeout': raise httpx.ReadTimeout('private-timeout',request=request)
        return httpx.Response(401 if failure=='http_4xx' else 503,json={'private':'body'})
    engine=OpenAIRankLLMEngine(prompt_builder=InstrumentedPrompt(),endpoint='https://provider.example/v1',
        api_key='local-protocol-only',model='test-model',maximum_output_tokens=4096,
        reasoning_effort='minimal',verbosity='low',
        client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=engine)
    store=Store(); service=RankingService(auth=Auth(),store=store,adapter=adapter,
        policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,
        server_commit_revision=0,history_generation=1,consent_revision=1))
    assert response['fallback_reason']==expected_reason
    assert store.frozen['bindings']['execution']['attempts_started']==1
    assert store.frozen['bindings']['execution']['cost_basis']=='unknown_provider_charge_reserved'


@pytest.mark.parametrize(('adapter_model','adapter_clock','expected_reason'), [
    ('different-model', None, 'model_policy_mismatch'),
    ('test-model', iter((0.0, 7.0)).__next__, 'provider_deadline'),
])
def test_definite_pre_call_fallback_releases_zero_charge_reservation(
        adapter_model, adapter_clock, expected_reason):
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:1]
    class Store:
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=1,learning_enabled=True,provider_processing_enabled=True,
            provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): return True
        def settle_budget(self, **kwargs): self.settlement=kwargs
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Engine:
        def prepare(self, model_input): return type('Prepared',(),{'input_tokens_bound':100,'output_tokens_budget':100})()
        def rerank_prepared(self, prepared, timeout_seconds): pytest.fail('pre-call fallback must not invoke engine')
    class Auth:
        def get_user(self, token): return {'id':'local-owner'}
    kwargs={} if adapter_clock is None else {'clock':adapter_clock}
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider',adapter_model,'https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=Engine(),**kwargs)
    store=Store(); service=RankingService(auth=Auth(),store=store,adapter=adapter,
        policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,server_commit_revision=0,
        history_generation=1,consent_revision=1))
    assert response['fallback_reason']==expected_reason
    assert store.settlement['actual_usd']==0.0 and store.settlement['status']=='released'
    assert store.frozen['bindings']['execution']['attempts_started']==0
    assert store.frozen['bindings']['execution']['cost_basis']=='released_no_provider_call'


@pytest.mark.parametrize(('case','expected_reason','expected_reserve_calls'), [
    ('missing_pricing', 'unknown_provider_pricing', 0),
    ('missing_prepare', 'provider_preparation_unavailable', 0),
    ('noncallable_prepare', 'provider_preparation_unavailable', 0),
    ('prepare_error', 'provider_preparation_failed', 0),
    ('prepare_os_error', 'provider_preparation_failed', 0),
    ('prepare_runtime_error', 'provider_preparation_failed', 0),
    ('prepare_type_error', 'provider_preparation_failed', 0),
    ('request_limit', 'request_cost_limit', 0),
    ('store_refusal', 'budget_reservation_failed', 1),
])
def test_service_reports_truthful_pre_call_fallback_reason(case, expected_reason, expected_reserve_calls):
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:1]
    class Store:
        reserve_calls=0
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=1,learning_enabled=True,provider_processing_enabled=True,
            provider_policy_id='test-policy',events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): self.reserve_calls+=1; return case!='store_refusal'
        def settle_budget(self, **kwargs): pytest.fail('pre-call rejection has no reservation to settle')
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Engine:
        def prepare(self, model_input):
            errors={'prepare_error':ValueError, 'prepare_os_error':OSError,
                'prepare_runtime_error':RuntimeError, 'prepare_type_error':TypeError}
            if case in errors: raise errors[case]('known prompt preparation failure')
            return type('Prepared',(),{'input_tokens_bound':100,'output_tokens_budget':100})()
        def rerank_prepared(self, prepared, timeout_seconds): pytest.fail('pre-call fallback must not invoke engine')
    engine=(object() if case=='missing_prepare' else
        type('NoncallableEngine',(),{'prepare':None})() if case=='noncallable_prepare' else Engine())
    input_price=None if case=='missing_pricing' else .25
    request_limit=.0001 if case=='request_limit' else .02
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,request_cost_limit_usd=request_limit,input_cost_per_million_tokens_usd=input_price,
        output_cost_per_million_tokens_usd=2),engine=engine)
    store=Store(); service=RankingService(auth=type('Auth',(),{'get_user':lambda self,token:{'id':'local-owner'}})(),
        store=store,adapter=adapter,policy=ServicePolicy('test-policy','test-model','test-policy','local-tenant',enabled=True),
        cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(history_revision=0,server_commit_revision=0,
        history_generation=1,consent_revision=1))
    assert response['fallback_reason']==expected_reason
    assert store.reserve_calls==expected_reserve_calls
    assert store.frozen['bindings']['execution']['attempts_started']==0
    assert store.frozen['bindings']['execution']['cost_basis']=='no_provider_call'


def test_service_accepts_no_consent_snapshot_zero_without_provider_or_budget_calls():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    from curator.recommendation.service import RankingService, ServicePolicy
    rows=json.loads((ROOT/'tests/fixtures/m2-retained-public.json').read_text())['rows'][:1]
    class Store:
        reserve_calls=settle_calls=0
        def history_snapshot(self, token): return dict(history_revision=0,included_history_revision=0,
            history_generation=1,consent_revision=0,learning_enabled=False,
            provider_processing_enabled=False,provider_policy_id=None,events=[])
        def retained_candidates(self, **kwargs): return rows
        def reserve_budget(self, **kwargs): self.reserve_calls+=1; return True
        def settle_budget(self, **kwargs): self.settle_calls+=1
        def owner_states(self, token, story_ids): return {}
        def save_frozen_order(self, **kwargs): self.frozen=kwargs; return 'local-frozen'
    class Engine:
        def prepare(self, model_input): pytest.fail('no-consent fallback must not prepare provider input')
        def rerank_prepared(self, prepared, timeout_seconds): pytest.fail('no-consent fallback must not call provider')
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example',
        'test-policy',max_retries=0,input_cost_per_million_tokens_usd=.25,
        output_cost_per_million_tokens_usd=2),engine=Engine())
    store=Store(); service=RankingService(auth=type('Auth',(),{'get_user':lambda self,token:{'id':'local-owner'}})(),
        store=store,adapter=adapter,policy=ServicePolicy('test-policy','test-model','test-policy',
        'local-tenant',enabled=True),cursor_key=b'x'*32)
    response=service.rank(authorization='Bearer local-test',body=dict(schema_version=1,
        policy_version='test-policy',model_version='test-model',history_revision=0,
        server_commit_revision=0,history_generation=1,consent_revision=0,page_size=25,
        eligibility={'category':None,'query':None},exclude_story_ids=[]))
    assert response['result_mode']=='fallback'
    assert response['fallback_reason']=='provider_processing_consent_required'
    assert len(response['cards'])==1 and response['cards'][0]['story_id']==rows[0]['story_id']
    assert store.frozen['bindings']['consent_revision']==0
    assert store.frozen['bindings']['execution']['attempts_started']==0
    assert store.frozen['bindings']['execution']['cost_basis']=='no_provider_call'
    assert store.reserve_calls==0 and store.settle_calls==0


def test_prepare_with_reason_does_not_swallow_process_control():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    class Engine:
        def prepare(self, model_input): raise KeyboardInterrupt()
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=Engine())
    model_input=captured_input()
    request=RankingRequest(1,'process-control-request',AuthenticatedOwner('tenant','user','principal',ActorKind.HUMAN),
        model_input.candidates,tuple(c.candidate_id for c in model_input.candidates),(),0,0,1,1,
        'test-policy','test-model')
    with pytest.raises(KeyboardInterrupt):
        adapter.prepare_with_reason(request)


def test_observed_retry_settlement_retains_only_unknown_attempt_ceiling():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=1,input_cost_per_million_tokens_usd=.25,output_cost_per_million_tokens_usd=2),engine=object())
    assert adapter.settle_observed_cost(input_tokens=100,output_tokens=20,unknown_attempts=1,reserved_usd=.01)==pytest.approx(.005065)
    with pytest.raises(ValueError,match='exceeded'):
        adapter.settle_observed_cost(input_tokens=100000,output_tokens=20000,unknown_attempts=0,reserved_usd=.01)


def test_tokenizer_rejects_unknown_or_missing_cache_before_library_import(tmp_path):
    from curator.recommendation.runtime import configured_token_counter
    with pytest.raises(ValueError,match='unreviewed'):
        configured_token_counter({'tokenizer_encoding':'unreviewed'}, {})
    with pytest.raises(ValueError,match='cache missing'):
        configured_token_counter({'tokenizer_encoding':'o200k_base'}, {'TIKTOKEN_CACHE_DIR':str(tmp_path)})


def test_budget_fit_trims_only_oldest_whole_events_and_keeps_newest_negative_and_repetition():
    from curator.recommendation.rankllm_adapter import RankLLMAdapter, RankerPolicy
    model_input=captured_input(); base_time=model_input.candidates[0].published_at
    repeated=model_input.candidates[2]
    events=(
        OrderedHistoryEvent('event-1',EventType.READ_MORE,base_time,1,model_input.candidates[0].candidate_id,
            None,'oldest marker',model_input.candidates[0].summary,model_input.candidates[0].source_id),
        OrderedHistoryEvent('event-2',EventType.LESS_LIKE_THIS,base_time+timedelta(seconds=1),2,repeated.candidate_id,
            None,'explicit reversal marker',repeated.summary,repeated.source_id),
        OrderedHistoryEvent('event-3',EventType.MORE_LIKE_THIS,base_time+timedelta(seconds=2),3,repeated.candidate_id,
            None,'explicit reversal marker',repeated.summary,repeated.source_id),
        OrderedHistoryEvent('event-4',EventType.SAVE,base_time+timedelta(seconds=3),4,repeated.candidate_id,
            None,'newest negative marker',repeated.summary,repeated.source_id,False),
    )
    prompt=InstrumentedPrompt()
    engine=OpenAIRankLLMEngine(prompt_builder=prompt,endpoint='https://provider.example/v1',api_key='unused',
        model='test-model',maximum_output_tokens=4096,reasoning_effort='minimal',verbosity='low',
        client_factory=lambda:pytest.fail('provider must not be called'),token_counter=lambda value:len(value.encode()),
        reasoning_token_allowance=2048,prompt_framing_token_allowance=1024,prompt_framing_tokens_per_message=8)
    with_history=ModelRankingInput(None,model_input.candidates,events,4,4,1,1,'test-policy','test-model')
    protected=engine.prepare(ModelRankingInput(None,model_input.candidates,events[1:],4,4,1,1,'test-policy','test-model'))
    full=engine.prepare(with_history)
    input_price=.25; output_price=2.0
    protected_cost=(protected.input_tokens_bound*input_price+4096*output_price)/1_000_000
    full_cost=(full.input_tokens_bound*input_price+4096*output_price)/1_000_000
    request=RankingRequest(1,'budget-fit-request',AuthenticatedOwner('tenant','user','principal',ActorKind.HUMAN),
        model_input.candidates,tuple(c.candidate_id for c in model_input.candidates),events,4,4,1,1,
        'test-policy','test-model')
    original_events=request.ordered_history
    adapter=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,request_cost_limit_usd=(protected_cost+full_cost)/2,
        input_cost_per_million_tokens_usd=input_price,output_cost_per_million_tokens_usd=output_price),engine=engine)
    prepared=adapter.prepare(request)
    assert prepared is not None and prepared.history_events_omitted>0
    assert 0<prepared.history_events_included<4
    prompt_text=prepared.prompt[-1]['content']
    assert 'newest negative marker' in prompt_text and '"action_value":false' in prompt_text
    assert 'oldest marker' not in prompt_text
    assert prompt_text.count('explicit reversal marker')==2
    assert prompt_text.index('less_like_this')<prompt_text.index('more_like_this')
    assert request.ordered_history is original_events and request.ordered_history==events
    assert adapter.reservation_estimate(estimated_input_tokens=prepared.input_tokens_bound,
        estimated_output_tokens=prepared.output_tokens_budget) is not None
    protected_only_request=RankingRequest(1,'protected-feedback-request',request.owner,request.candidates,
        request.selected_candidate_registry_ids,events[1:],4,4,1,1,'test-policy','test-model')
    too_small=RankLLMAdapter(policy=RankerPolicy('test-provider','test-model','https://provider.example','test-policy',
        max_retries=0,request_cost_limit_usd=protected_cost-0.0000001,
        input_cost_per_million_tokens_usd=input_price,output_cost_per_million_tokens_usd=output_price),engine=engine)
    assert too_small.prepare(protected_only_request) is None
