"""Startup validation for the M2.1 Phase 1 keys. Boot fails, never clamps."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from curator.config import ConfigError, load_config, load_sources

ROOT = Path(__file__).resolve().parents[1]


def write_sources(tmp_path, mutate):
    raw = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    target = tmp_path / "sources.yaml"
    target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def test_shipped_config_declares_every_phase1_key():
    cfg = load_config(ROOT)
    for key in ("provider", "model", "api_key_env", "api_origin", "daily_cost_limit_usd",
                "input_cost_per_million_tokens_usd", "output_cost_per_million_tokens_usd",
                "characters_per_token", "max_output_tokens_per_story", "cache_ttl_days", "on_failure",
                "pairing_window_hours", "pairing_max_context_titles", "pairing_daily_call_limit",
                "pairing_max_attempts", "pairing_recheck_hours"):
        assert key in cfg.translation, key
    assert cfg.translation["on_failure"] == "show_original_marked"
    # Phase 1 ships enabled: the missing translation surface is a bug, not a flag.
    assert cfg.translation["enabled"] is True
    assert cfg.language["default_display"] in ("en", "zh")
    assert cfg.language["other_lane_enabled"] is True
    assert cfg.language["exclusive_category_id"]
    # Phase 1 is English direction only. The flip stays in the design, off.
    assert cfg.reader["chinese_site_mode_enabled"] is False
    # The pairing window is a translation key; grouping keeps only its switch.
    assert cfg.translation["pairing_window_hours"] == 48
    assert cfg.translation["pairing_daily_call_limit"] == 600
    assert set(cfg.grouping) == {"cross_language_enabled"}


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["translation"].__setitem__("on_failure", "drop"),
    lambda raw: raw["translation"].__setitem__("daily_cost_limit_usd", 25.1),
    lambda raw: raw["translation"].__setitem__("input_cost_per_million_tokens_usd", 1001),
    lambda raw: raw["translation"].__setitem__("output_cost_per_million_tokens_usd", -1),
    lambda raw: raw["translation"].__setitem__("characters_per_token", 0),
    lambda raw: raw["translation"].__setitem__("max_output_tokens_per_story", 0),
    lambda raw: raw["translation"].__setitem__("cache_ttl_days", 0),
    lambda raw: raw["translation"].__setitem__("cache_ttl_days", 366),
    lambda raw: raw["translation"].__setitem__("api_key_env", "lowercase_name"),
    lambda raw: raw["translation"].__setitem__("api_origin", "http://api.openai.com"),
    lambda raw: raw["translation"].__setitem__("model", "not a model id"),
    lambda raw: raw["language"].__setitem__("default_display", "fr"),
    lambda raw: raw["language"].__setitem__("other_lane_enabled", "yes"),
    lambda raw: raw["language"].__setitem__("exclusive_category_id", "Not A Category"),
    lambda raw: raw["reader"].__setitem__("chinese_site_mode_enabled", "false"),
    lambda raw: raw["grouping"].__setitem__("cross_language_enabled", "yes"),
    # The removed heuristic keys must fail the boot, not be silently ignored.
    lambda raw: raw["grouping"].__setitem__("window_hours", 48),
    lambda raw: raw["grouping"].__setitem__("min_shared_entity_tokens", 2),
    lambda raw: raw["grouping"].__setitem__("max_pairs_per_bucket", 2000),
    lambda raw: raw["translation"].__setitem__("pairing_window_hours", 0),
    lambda raw: raw["translation"].__setitem__("pairing_window_hours", 169),
    lambda raw: raw["translation"].__setitem__("pairing_max_context_titles", 501),
    lambda raw: raw["translation"].__setitem__("pairing_daily_call_limit", 5001),
    lambda raw: raw["translation"].__setitem__("pairing_max_attempts", 0),
    lambda raw: raw["translation"].__setitem__("pairing_recheck_hours", 49),
])
def test_out_of_range_values_fail_the_boot(tmp_path, mutation):
    with pytest.raises(ConfigError):
        load_sources(write_sources(tmp_path, mutation))


def test_no_drop_semantics_exist_anywhere_in_the_loader():
    source = (ROOT / "curator/config.py").read_text(encoding="utf-8")
    assert '"drop"' not in source and "'drop'" not in source
