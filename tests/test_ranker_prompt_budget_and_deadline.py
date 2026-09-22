"""The prompt budget, the raised deadline, and the bounds that keep them honest.

Symptom this file is written against (2026-09-21, live, owner account): the paid
reorder reached the provider and every attempt came back
`result_mode: fallback, fallback_reason: provider_deadline`, and one POST /rank
was killed by the container at 15.2s and answered 500. Two causes: the prepared
request was far larger than a six-second call could ever finish, and the six
seconds were hardcoded in the transport where no policy could reach them.
"""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from curator.contracts.enums import ActorKind, EventType, RankingResultMode
from curator.contracts.ranking_request import (AuthenticatedOwner, ModelRankingInput,
    OrderedHistoryEvent, RankingCandidate, RankingRequest)
from curator.recommendation.async_provider import (MAXIMUM_PROVIDER_DEADLINE_SECONDS,
    AsyncOpenAIResponses)
from curator.recommendation.composition import CompositionPolicyError, parse_composition_policy
from curator.recommendation.deployment import FUNCTION_TIMEOUT_ENV, function_timeout_seconds
from curator.recommendation.engine import OpenAIRankLLMEngine
from curator.recommendation.rankllm_adapter import BudgetState, RankLLMAdapter, RankerPolicy
from curator.recommendation.runtime import (PRECLAIM_TRANSPORT_CALLS,
    assert_request_fits_function_timeout, prompt_budget)
from curator.recommendation.service import CLAIMED_SECTION_MAX_TRANSPORT_CALLS

ROOT = Path(__file__).resolve().parents[1]
RANKER_POLICY = yaml.safe_load((ROOT / 'config/ranker-policy-r1.yaml').read_text())
COMPOSITION_POLICY = yaml.safe_load((ROOT / 'config/ranking-policy-r2.yaml').read_text())


class RecordingPrompt:
    """The local prompt stand-in, capturing exactly what the engine was given."""

    def __init__(self):
        self.queries, self.passages = [], []

    def create_prompt(self, *, query, passages):
        self.queries.append(query)
        self.passages.append(tuple(passages))
        return [{'role': 'user', 'content': query + '\n' + '\n'.join(passages)}]


def public_candidates(count):
    rows = json.loads((ROOT / 'tests/fixtures/m2-retained-public.json').read_text())['rows'][:count]
    return tuple(RankingCandidate(row['story_id'], row['story_id'], row['story_id'], row['title'],
        row['summary'], row['source_id'], row['language'],
        datetime.fromisoformat(row['published_at'])) for row in rows)


def history(candidates, count):
    """`count` read events, oldest first, so the newest ones are at the end."""
    base = candidates[0].published_at
    return tuple(OrderedHistoryEvent(f'event-{index:03d}', EventType.READ_MORE,
        base - timedelta(minutes=count - index), index + 1,
        candidates[index % len(candidates)].candidate_id, None,
        f'history marker {index:03d}', f'history summary {index:03d}',
        candidates[index % len(candidates)].source_id) for index in range(count))


def local_engine(prompt, responder=None):
    return OpenAIRankLLMEngine(prompt_builder=prompt, endpoint='https://provider.example/v1',
        api_key='local-test', model='test-model',
        maximum_output_tokens=RANKER_POLICY['maximum_output_tokens'],
        reasoning_effort=RANKER_POLICY['reasoning_effort'], verbosity=RANKER_POLICY['verbosity'],
        reasoning_token_allowance=RANKER_POLICY['reasoning_token_allowance'],
        prompt_framing_token_allowance=RANKER_POLICY['prompt_framing_token_allowance'],
        prompt_framing_tokens_per_message=RANKER_POLICY['prompt_framing_tokens_per_message'],
        client_factory=(lambda: httpx.AsyncClient(transport=httpx.MockTransport(responder)))
        if responder else (lambda: pytest.fail('provider must not be called')))


def local_policy(**overrides):
    values = dict(max_retries=0, input_cost_per_million_tokens_usd=.25,
                  output_cost_per_million_tokens_usd=2.0)
    values.update(overrides)
    return RankerPolicy('test-provider', 'test-model', 'https://provider.example', 'test-policy', **values)


def local_request(candidates, events):
    return RankingRequest(1, 'budget-request',
        AuthenticatedOwner('tenant', 'user', 'principal', ActorKind.HUMAN), candidates,
        tuple(candidate.candidate_id for candidate in candidates), events, len(events), len(events),
        1, 1, 'test-policy', 'test-model')


def test_seventy_two_history_events_reach_the_prompt_as_twenty_four_newest():
    candidates = public_candidates(10)
    events = history(candidates, 72)
    prompt = RecordingPrompt()
    adapter = RankLLMAdapter(policy=local_policy(max_history_events=24, request_cost_limit_usd=1.0),
                             engine=local_engine(prompt))
    prepared = adapter.prepare(local_request(candidates, events))
    assert prepared.history_events_included == 24
    assert prepared.history_events_budget_omitted == 48
    assert prepared.history_events_omitted == 0, 'the budget fits, so cost fitting drops nothing'
    text = prompt.queries[-1]
    assert 'history marker 071' in text and 'history marker 048' in text
    assert 'history marker 047' not in text and 'history marker 000' not in text
    # The request itself is untouched: the budget is what the MODEL sees.
    assert len(local_request(candidates, events).ordered_history) == 72


def test_fifty_candidates_send_twenty_five_and_keep_the_tail_in_recipe_order():
    candidates = public_candidates(50)
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        count = body['text']['format']['schema']['properties']['order']['maxItems']
        return httpx.Response(200, json={'id': 'local-response', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps({'order': list(range(count, 0, -1))})}]}],
            'usage': {'input_tokens': 1000, 'output_tokens': 1200}})

    prompt = RecordingPrompt()
    adapter = RankLLMAdapter(policy=local_policy(max_model_candidates=25, request_cost_limit_usd=1.0),
                             engine=local_engine(prompt, respond))
    request = local_request(candidates, ())
    prepared = adapter.prepare(request)
    assert len(prepared.candidate_ids) == 25 and prepared.candidates_budget_omitted == 25
    assert len(prompt.passages[-1]) == 25
    receipt = adapter.rank(request, provider_processing_consent=True, budget=BudgetState(0),
        estimated_input_tokens=prepared.input_tokens_bound,
        estimated_output_tokens=prepared.output_tokens_budget, prepared=prepared)
    expected_ids = tuple(candidate.candidate_id for candidate in candidates)
    assert receipt.result_mode is RankingResultMode.MODEL
    assert sent[0]['text']['format']['schema']['properties']['order']['maxItems'] == 25
    # The head is the model's order (reversed here), the tail is untouched.
    assert receipt.ranked_candidate_ids[:25] == tuple(reversed(expected_ids[:25]))
    assert receipt.ranked_candidate_ids[25:] == expected_ids[25:]
    assert sorted(receipt.ranked_candidate_ids) == sorted(expected_ids)


def test_the_prompt_budget_lowers_the_measured_input_token_bound():
    """The bound is the thing the deadline has to cover, so it is measured."""
    candidates = public_candidates(50)
    events = history(candidates, 72)
    request = local_request(candidates, events)
    unbudgeted = RankLLMAdapter(policy=local_policy(max_history_events=200, max_model_candidates=100,
        request_cost_limit_usd=1.0), engine=local_engine(RecordingPrompt())).prepare(request)
    budgeted = RankLLMAdapter(policy=local_policy(
        max_history_events=RANKER_POLICY['prompt']['max_history_events'],
        max_model_candidates=RANKER_POLICY['prompt']['max_model_candidates'],
        request_cost_limit_usd=1.0), engine=local_engine(RecordingPrompt())).prepare(request)
    # About half, on this fixture. The assertion is a floor, not the measurement:
    # the exact numbers move with the corpus and are reported in the PR body.
    assert budgeted.input_tokens_bound < unbudgeted.input_tokens_bound * 0.6
    assert unbudgeted.history_events_included == 72 and budgeted.history_events_included == 24
    assert len(unbudgeted.candidate_ids) == 50 and len(budgeted.candidate_ids) == 25
    # The OUTPUT budget is the configured cap either way; only the input moves.
    assert budgeted.output_tokens_budget == unbudgeted.output_tokens_budget


def test_a_provider_that_never_answers_falls_back_instead_of_raising():
    """The reader gets a typed 200 fallback, never the container's 500."""
    import threading

    started, release = threading.Event(), threading.Event()

    class NeverAnswers:
        def prepare(self, model_input):
            from curator.recommendation.engine import PreparedProviderRequest
            return PreparedProviderRequest(object(),
                tuple(candidate.candidate_id for candidate in model_input.candidates), 100, 256)

        def rerank_prepared(self, prepared, *, timeout_seconds):
            started.set()
            # Ignores its own budget on purpose: that is the case the adapter's
            # bound exists for.
            release.wait(30)
            raise AssertionError('the adapter must not wait for this call')

    candidates = public_candidates(3)
    adapter = RankLLMAdapter(policy=local_policy(deadline_seconds=0.2, request_cost_limit_usd=1.0),
                             engine=NeverAnswers())
    request = local_request(candidates, ())
    prepared = adapter.prepare(request)
    receipt = adapter.rank(request, provider_processing_consent=True, budget=BudgetState(0),
        estimated_input_tokens=100, estimated_output_tokens=256, prepared=prepared)
    release.set()
    assert started.is_set()
    assert receipt.result_mode is RankingResultMode.FALLBACK
    assert receipt.fallback_reason == 'provider_deadline'
    assert receipt.ranked_candidate_ids == tuple(c.candidate_id for c in candidates)


@pytest.mark.parametrize('seconds', [0.5, 6, 25, MAXIMUM_PROVIDER_DEADLINE_SECONDS])
def test_the_policy_deadline_is_accepted_up_to_the_transport_ceiling(seconds):
    local_policy(deadline_seconds=seconds).validate()
    AsyncOpenAIResponses(client=object(), endpoint='https://provider.example/v1', api_key='k',
                         model='m', max_output_tokens=256, total_seconds=seconds)


@pytest.mark.parametrize('seconds', [0, -1, MAXIMUM_PROVIDER_DEADLINE_SECONDS + 1, 999])
def test_a_deadline_above_the_ceiling_is_refused_by_policy_and_transport(seconds):
    with pytest.raises(ValueError, match='ranker deadline'):
        local_policy(deadline_seconds=seconds).validate()
    with pytest.raises(ValueError, match='invalid provider transport configuration'):
        AsyncOpenAIResponses(client=object(), endpoint='https://provider.example/v1', api_key='k',
                             model='m', max_output_tokens=256, total_seconds=seconds)


@pytest.mark.parametrize(('field', 'value'), [
    ('max_history_events', -1), ('max_history_events', 201), ('max_history_events', 1.5),
    ('max_model_candidates', 4), ('max_model_candidates', 101), ('max_model_candidates', '25'),
])
def test_out_of_range_prompt_budget_values_are_refused(field, value):
    with pytest.raises(ValueError, match=f'ranker prompt.{field}'):
        local_policy(**{field: value}).validate()


def test_an_unknown_prompt_policy_key_refuses_the_boot():
    with pytest.raises(ValueError, match='unknown ranker policy prompt keys'):
        prompt_budget({'prompt': {'max_history_events': 24, 'max_story_words': 300}})


def test_the_shipping_policy_declares_the_budget_and_the_raised_deadline():
    assert RANKER_POLICY['deadline_seconds'] == 25
    assert prompt_budget(RANKER_POLICY) == (24, 25)
    # The budget cannot exceed the window the recipe composes.
    assert RANKER_POLICY['prompt']['max_model_candidates'] <= RANKER_POLICY['candidate_limit']


def _shipping_worst_case():
    return (float(RANKER_POLICY['deadline_seconds']) + float(RANKER_POLICY['settle_window_seconds'])
            + (CLAIMED_SECTION_MAX_TRANSPORT_CALLS + PRECLAIM_TRANSPORT_CALLS
               + 2 * RANKER_POLICY['supabase']['timeout_retries'])
            * float(RANKER_POLICY['supabase']['timeout_seconds']))


def test_boot_refuses_when_one_request_can_outlive_its_own_container():
    """15 is what was deployed, and it is what turned a fallback into a 500."""
    worst_case = _shipping_worst_case()
    assert worst_case == 180.0
    with pytest.raises(ValueError, match=FUNCTION_TIMEOUT_ENV):
        assert_request_fits_function_timeout(RANKER_POLICY,
            RANKER_POLICY['supabase']['timeout_seconds'], {FUNCTION_TIMEOUT_ENV: '15'})
    # Equal is not enough: the request must finish strictly inside the container.
    with pytest.raises(ValueError, match='not below the function timeout'):
        assert_request_fits_function_timeout(RANKER_POLICY,
            RANKER_POLICY['supabase']['timeout_seconds'], {FUNCTION_TIMEOUT_ENV: '180'})


def test_the_shipping_policy_fits_the_default_function_timeout():
    environment = {}
    assert function_timeout_seconds(environment) == 181
    assert assert_request_fits_function_timeout(RANKER_POLICY,
        RANKER_POLICY['supabase']['timeout_seconds'], environment) == _shipping_worst_case()
    assert _shipping_worst_case() < function_timeout_seconds(environment)


def test_function_timeout_remains_programmable_above_the_retry_budget():
    environment = {FUNCTION_TIMEOUT_ENV: '200'}
    assert function_timeout_seconds(environment) == 200
    assert assert_request_fits_function_timeout(RANKER_POLICY,
        RANKER_POLICY['supabase']['timeout_seconds'], environment) == 180.0


def test_the_claim_window_covers_the_raised_deadline():
    """The claim covers one retry: 25 + 5 + 23 x 5 + 10 = 155, below 160."""
    terms = dict(provider_deadline_seconds=RANKER_POLICY['deadline_seconds'],
                 settle_window_seconds=RANKER_POLICY['settle_window_seconds'],
                 supabase_timeout_seconds=RANKER_POLICY['supabase']['timeout_seconds'],
                 claimed_section_transport_calls=(CLAIMED_SECTION_MAX_TRANSPORT_CALLS
                     + RANKER_POLICY['supabase']['timeout_retries']))
    assert COMPOSITION_POLICY['run']['ranking_claim_seconds'] == 160
    parse_composition_policy(COMPOSITION_POLICY, **terms)
    stale = json.loads(json.dumps(COMPOSITION_POLICY))
    stale['run']['ranking_claim_seconds'] = 155
    with pytest.raises(CompositionPolicyError, match='must exceed the provider deadline'):
        parse_composition_policy(stale, **terms)
