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
                                            supabase_timeout_seconds)

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_policy_declares_a_timeout_the_heavy_query_can_finish_in():
    _, policy = load_ranker_policy({}, RANKER_POLICY_DEFAULT, root=ROOT)
    value = supabase_timeout_seconds(policy)
    assert value == 10.0
    # The failing production request gave up at about 4.8 seconds. A ceiling at
    # or below that reproduces the outage, so this asserts the gap, not the
    # literal.
    assert value > 4.8


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
    assert raw["supabase"] == {"timeout_seconds": 10}
