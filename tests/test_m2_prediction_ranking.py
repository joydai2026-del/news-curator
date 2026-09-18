"""B4: the model predicts actions, the server turns them into an order."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from curator.recommendation.async_provider import (
    PREDICTED_ACTIONS,
    AsyncRankLLMProvider,
    ProviderResponseError,
    exact_prediction_schema,
)
from curator.recommendation.composition import load_composition_policy
from curator.recommendation.engine import OpenAIRankLLMEngine, ScoringPolicy

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "ranking-policy-r2.yaml"


@pytest.fixture()
def scoring():
    return ScoringPolicy.from_composition(load_composition_policy(POLICY_PATH))


def prediction(identifier, **values):
    entry = {"id": identifier}
    entry.update({action: values.get(action, 0.0) for action in PREDICTED_ACTIONS})
    return entry


class Transport:
    def __init__(self, payload, *, predicts=True):
        self.payload = payload
        self.predicts_actions = predicts
        self.seen = None

    async def create(self, prompt, *, candidate_count):
        self.seen = candidate_count
        return {"id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 20},
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(self.payload)}]}]}


async def parse(payload, count, *, predicts=True):
    provider = AsyncRankLLMProvider(prompt_builder=object(), transport=Transport(payload, predicts=predicts))
    return await provider.rerank_prompt(prompt=[], candidate_count=count)


# --- schema ---------------------------------------------------------------

def test_the_schema_asks_for_one_entry_per_candidate_and_nothing_else():
    schema = exact_prediction_schema(3)["schema"]
    array = schema["properties"]["predictions"]
    assert array["minItems"] == array["maxItems"] == 3
    assert array["items"]["additionalProperties"] is False
    assert set(array["items"]["required"]) == {"id", *PREDICTED_ACTIONS}
    assert schema["additionalProperties"] is False


def test_only_captured_actions_are_predicted():
    assert PREDICTED_ACTIONS == ("p_open", "p_read_original", "p_save", "p_more_like_this", "p_less_like_this")
    for uncaptured in ("p_ask_question", "p_dwell", "p_dismiss"):
        assert uncaptured not in PREDICTED_ACTIONS


# --- parsing --------------------------------------------------------------


def test_a_valid_response_parses(scoring):
    import asyncio
    outcome = asyncio.run(parse({"predictions": [prediction(1, p_open=0.5), prediction(2, p_open=0.9)]}, 2))
    assert outcome.order == (1, 2)
    assert outcome.predictions[1]["p_open"] == 0.9


@pytest.mark.parametrize("payload", [
    {"predictions": [{"id": 1}, {"id": 2}]},                               # missing probabilities
    {"predictions": [dict(prediction(1)), dict(prediction(1))]},           # duplicate id
    {"predictions": [dict(prediction(1))]},                                # missing an id
    {"predictions": [dict(prediction(1), p_open=1.5), dict(prediction(2))]},  # out of range
    {"predictions": [dict(prediction(1), p_open=True), dict(prediction(2))]},  # a boolean is not a probability
    {"order": [1, 2]},                                                     # the old shape
    {"predictions": "no"},                                                 # free text
])
def test_a_malformed_prediction_is_rejected_while_its_real_cost_still_settles(payload):
    import asyncio
    with pytest.raises(ProviderResponseError) as caught:
        asyncio.run(parse(payload, 2))
    # The truthful-cost property survives: a rejected response that reported
    # trustworthy usage still carries that usage so it can be settled.
    assert caught.value.input_tokens == 10 and caught.value.output_tokens == 20


# --- scoring --------------------------------------------------------------

def test_the_gate_collapses_an_article_nobody_will_open(scoring):
    strong_save = {"p_open": 0.01, "p_read_original": 0.0, "p_save": 0.9,
                   "p_more_like_this": 0.0, "p_less_like_this": 0.0}
    moderate = {"p_open": 0.5, "p_read_original": 0.3, "p_save": 0.2,
                "p_more_like_this": 0.1, "p_less_like_this": 0.0}
    assert scoring.score(strong_save) < scoring.score(moderate)


def test_a_more_likely_open_never_ranks_a_story_lower(scoring):
    """The gate used to invert once the weighted total went negative: a bigger
    p_open made a negative total MORE negative, so the story she was more likely
    to open ranked below the one she was not."""
    disliked = {"p_open": 0.2, "p_read_original": 0.0, "p_save": 0.0,
                "p_more_like_this": 0.0, "p_less_like_this": 0.9}
    same_but_likelier = dict(disliked, p_open=0.9)
    assert scoring.score(same_but_likelier) >= scoring.score(disliked)


def test_one_negative_lowers_a_score_without_zeroing_it(scoring):
    clean = {"p_open": 0.6, "p_read_original": 0.4, "p_save": 0.5,
             "p_more_like_this": 0.2, "p_less_like_this": 0.0}
    disliked = dict(clean, p_less_like_this=0.4)
    assert 0 < scoring.score(disliked) < scoring.score(clean)


def test_the_model_never_sees_the_weights(scoring):
    """The real claim, asserted on the real PROMPT. The old test checked the
    answer shape, which never contained a weight under any implementation, so it
    could not have failed even when the prompt was carrying every weight."""
    from datetime import datetime, timedelta, timezone

    from curator.contracts.enums import EventType
    from curator.contracts.ranking_request import ModelRankingInput, OrderedHistoryEvent

    engine = OpenAIRankLLMEngine(prompt_builder=object(), endpoint="https://provider.invalid",
        api_key="k", model="gpt-5-mini", maximum_output_tokens=8192, reasoning_effort="minimal",
        verbosity="low", client_factory=lambda: None, scoring=scoring)
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    history = (OrderedHistoryEvent("event:" + "1" * 64, EventType.SAVE, now - timedelta(hours=3), 1,
                                   "story:" + "1" * 64, None, "A saved story", None, "reuters", True),)
    query = engine._query_with_history(ModelRankingInput(
        candidates=(), ordered_history=history, history_revision=1, server_commit_revision=1,
        history_generation=1, consent_revision=1, policy_version="policy", model_version="model",
        query=None))
    for weight in scoring.engagement_weights.values():
        assert str(weight) not in query, f"the prompt is carrying the weight {weight}"
    # The only "weight" left in the prompt is the sentence telling the model the
    # ordering weights are not its business. No weight FIELD and no weight VALUE.
    assert '"weight"' not in query, "the prompt is carrying a weight field"
    assert "the ordering weights are not yours" in query
    # What it DOES carry: what happened, and how long ago.
    assert "save" in query and "age_hours" in query


# --- the output budget guard ---------------------------------------------

def test_the_budget_guard_is_sized_from_the_payload_actually_requested(scoring):
    def engine(maximum):
        return OpenAIRankLLMEngine(prompt_builder=object(), endpoint="https://provider.invalid",
            api_key="k", model="gpt-5-mini", maximum_output_tokens=maximum, reasoning_effort="minimal",
            verbosity="low", client_factory=lambda: None, scoring=scoring,
            reasoning_token_allowance=1024)
    predictions = len(json.dumps(engine(8192)._answer_shape(50)).encode())
    permutation = len(json.dumps({"order": list(range(1, 51))}, separators=(",", ":")).encode())
    assert predictions > permutation * 5, "the object array is much larger than a permutation"
    # The shipped budget accommodates a 50-candidate prediction payload.
    assert predictions + 1024 < 8192
