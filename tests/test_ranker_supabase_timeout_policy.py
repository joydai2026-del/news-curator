"""The per-call Supabase budget is policy, and the boot validates it.

Red before the fix: `SupabaseHTTP.__init__` defaulted `timeout_seconds=3.0` and
`runtime.build_application` never passed one, so the only way to give the Phase
2 candidate query more than three seconds was to edit source. Production
answered every POST /rank with 503 "Supabase request failed" after ~4.8s on
2026-09-21 because of exactly that ceiling.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from curator.recommendation.runtime import (RANKER_POLICY_DEFAULT, load_ranker_policy,
                                            supabase_general_candidate_query,
                                            supabase_timeout_retries, supabase_timeout_seconds)
from curator.recommendation import runtime
from curator.recommendation.deployment import FUNCTION_TIMEOUT_ENV

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_policy_declares_a_timeout_the_heavy_query_can_finish_in():
    _, policy = load_ranker_policy({}, RANKER_POLICY_DEFAULT, root=ROOT)
    value = supabase_timeout_seconds(policy)
    assert value == 5.0
    assert supabase_timeout_retries(policy) == 1
    # 5, not 10: this value multiplies CLAIMED_SECTION_MAX_TRANSPORT_CALLS inside
    # composition.py Check 10, so every second here lengthens the validated
    # reading-run claim, and a long claim is how long a crashed request blocks
    # the feed. The two known-broken candidate lanes (15s and 102s at 7,000
    # corpus rows) cannot be rescued by ANY legal value of this key; their fix is
    # the query.


def test_an_absent_section_refuses_the_boot_rather_than_defaulting():
    """A misspelled section name (`supabse:`) would otherwise silently keep 3.0
    on a policy file that reads as correct: the original outage, with the fix
    apparently applied. Caught by Codex review on 2026-09-21."""
    with pytest.raises(ValueError, match="must declare a `supabase` section"):
        supabase_timeout_seconds({})
    with pytest.raises(ValueError, match="must declare a `supabase` section"):
        supabase_timeout_seconds({"supabse": {"timeout_seconds": 10}})
    with pytest.raises(ValueError, match="`supabase` must be a mapping"):
        supabase_timeout_seconds({"supabase": None})
    with pytest.raises(ValueError, match="must declare timeout_seconds"):
        supabase_timeout_seconds({"supabase": {}})


@pytest.mark.parametrize("value", [1, 1.0, 5, 10, 30])
def test_in_range_values_are_accepted(value):
    assert supabase_timeout_seconds({"supabase": {"timeout_seconds": value}}) == float(value)


@pytest.mark.parametrize("value", [0, 0.5, 30.5, 600, -1, "10", None, True, []])
def test_out_of_range_or_wrong_typed_values_refuse_the_boot(value):
    with pytest.raises(ValueError, match="timeout_seconds"):
        supabase_timeout_seconds({"supabase": {"timeout_seconds": value}})


def test_a_misspelled_key_is_refused_rather_than_silently_ignored():
    """A typo that silently keeps 3.0 is the same outage with a config file that
    looks correct."""
    with pytest.raises(ValueError, match="unknown ranker policy supabase keys"):
        supabase_timeout_seconds({"supabase": {"timeout_second": 10}})
    with pytest.raises(ValueError, match="`supabase` must be a mapping"):
        supabase_timeout_seconds({"supabase": 10})


def test_the_policy_file_on_disk_parses_to_the_value_the_transport_receives():
    raw = yaml.safe_load((ROOT / RANKER_POLICY_DEFAULT).read_text())
    assert raw["supabase"] == {"timeout_seconds": 5, "timeout_retries": 1,
                               "general_candidate_query": "owner_narrow"}
    assert supabase_general_candidate_query(raw) == "owner_narrow"


@pytest.mark.parametrize("value", ["owner", "owner_narrow"])
def test_general_candidate_query_accepts_only_reviewed_routes(value):
    assert supabase_general_candidate_query({"supabase": {"general_candidate_query": value}}) == value


@pytest.mark.parametrize("value", [None, "unfiltered", 0, True])
def test_general_candidate_query_rejects_other_routes(value):
    with pytest.raises(ValueError, match="general_candidate_query"):
        supabase_general_candidate_query({"supabase": {"general_candidate_query": value}})


@pytest.mark.parametrize("value", [0, 1, 2])
def test_timeout_retry_policy_accepts_bounded_integers(value):
    assert supabase_timeout_retries({"supabase": {"timeout_retries": value}}) == value


@pytest.mark.parametrize("value", [-1, 3, 1.0, True, "1", None])
def test_timeout_retry_policy_rejects_unbounded_or_wrong_typed_values(value):
    with pytest.raises(ValueError, match="timeout_retries"):
        supabase_timeout_retries({"supabase": {"timeout_retries": value}})


def _boot_env():
    return {
        "NEWS_CURATOR_READER_ORIGIN": "https://reader.example",
        "NEWS_CURATOR_SUPABASE_URL": "https://project.supabase.co",
        "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY": "public-test",
        "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY": "service-test",
        "NEWS_CURATOR_CURSOR_SIGNING_KEY": "k" * 32,
        "NEWS_CURATOR_TENANT_ID": "tenant-test",
        "NEWS_CURATOR_MODEL_API_KEY": "provider-test",
        "NEWS_CURATOR_PREVIEW_OWNER_IDS": '["00000000-0000-0000-0000-000000000001"]',
    }


@pytest.mark.parametrize(("retries", "worst_case"), [(0, 225), (1, 235), (2, 245)])
def test_boot_requires_room_for_both_continuation_owner_state_reads(monkeypatch, retries, worst_case):
    path, policy = load_ranker_policy({}, RANKER_POLICY_DEFAULT, root=ROOT)
    policy["supabase"]["timeout_retries"] = retries
    monkeypatch.setattr(runtime, "load_ranker_policy", lambda *_args: (path, policy))
    environment = {**_boot_env(), FUNCTION_TIMEOUT_ENV: str(worst_case)}
    with pytest.raises(ValueError, match=f"one request may take up to {worst_case}.0s"):
        runtime.build_application(environ=environment)


def test_boot_keeps_the_claim_budget_to_one_owner_state_read(monkeypatch):
    claim_counts = []
    load_composition = runtime.load_composition_policy

    def capture_composition(*args, **kwargs):
        claim_counts.append(kwargs["claimed_section_transport_calls"])
        return load_composition(*args, **kwargs)

    def stop_at_tokenizer(*_args):
        raise RuntimeError("reached tokenizer after both budget checks")

    monkeypatch.setattr(runtime, "load_composition_policy", capture_composition)
    monkeypatch.setattr(runtime, "configured_token_counter", stop_at_tokenizer)
    with pytest.raises(RuntimeError, match="reached tokenizer"):
        runtime.build_application(environ={**_boot_env(), FUNCTION_TIMEOUT_ENV: "240"})
    assert claim_counts == [34]
