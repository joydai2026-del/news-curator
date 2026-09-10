from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import pytest
from curator.config import Category, Config
from curator.discovery import (
    DiscoveryError,
    _bands,
    build_discovery,
    load_discovery_policy,
    replay_discovery,
    validate_discovery_policy,
)
from curator.source_snapshot import (
    load_source_snapshot,
    snapshot_config_digest,
    write_source_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 10, 1, 17, 22, 44155, tzinfo=timezone.utc)


def _policy(name):
    p = load_discovery_policy(ROOT / "config" / name)
    for b in ("source_diversity", "topic_diversity"):
        p["bands"][b].update(cap=1.0, min_distinct=1)
    return p


def _receipt(tmp_path, p):
    cfg = Config(
        [Category("AI", ["AI", "artificial intelligence", "Anthropic"])],
        [],
        {},
        {},
        {},
        {},
        {},
    )
    original = load_source_snapshot(
        ROOT / "tests/fixtures/discovery-captured.json", current_time=NOW
    )
    path = tmp_path / "captured.json"
    write_source_snapshot(
        original.results,
        path,
        generated_at=original.generated_at,
        configuration_digest=snapshot_config_digest(cfg),
    )
    return build_discovery(
        cfg, load_source_snapshot(path, current_time=NOW), p, now=NOW, history=[]
    )


def test_r3_captured_receipt_qualifies_only_lower_shortfalls(tmp_path):
    p = _policy("discovery-policy-r3.yaml")
    p["bands"]["deliberate_surprise"].update(floor=0.5, cap=1.0)
    r = _receipt(tmp_path, p)
    verdicts = {b["band"]: b["verdict"] for b in r["bands"]}
    assert (
        r["verdict"] == "PASS"
        and verdicts["trend"] == "PASS"
        and verdicts["deliberate_surprise"] == "QUALIFIED_SHORTFALL"
    )
    assert replay_discovery(r)


def test_r3_refuses_zero_shortfall_and_above_cap(tmp_path):
    p = _policy("discovery-policy-r3.yaml")
    r = _receipt(tmp_path, p)
    assert (
        _bands(
            r["entries"],
            p,
            True,
            {lane: 0 for lane in ("updates", "hot", "interested", "surprise")},
        )[1]
        == "FAIL"
    )
    above = deepcopy(r["entries"])
    for e in above:
        e["lane_scores"]["surprise"] = 1.0
    bands, verdict = _bands(above, p, True, r["shortfalls"])
    surprise = next(b for b in bands if b["band"] == "deliberate_surprise")
    assert (
        surprise["achieved"] > surprise["cap"]
        and surprise["verdict"] == "FAIL"
        and verdict == "FAIL"
    )


def test_r2_captured_receipt_remains_strict(tmp_path):
    p = _policy("discovery-policy-r2.yaml")
    original = deepcopy(p)
    r = _receipt(tmp_path, p)
    assert (
        r["verdict"] == "FAIL"
        and all(b["verdict"] != "QUALIFIED_SHORTFALL" for b in r["bands"])
        and p == original
    )


def test_shortfall_opt_in_requires_exact_r3_identity_and_mapping():
    p = _policy("discovery-policy-r3.yaml")
    pseudo = deepcopy(p)
    pseudo.update(revision=2, policy_id="discovery-policy-r2")
    partial = deepcopy(p)
    partial["qualified_shortfalls"] = {"trend": "hot"}
    missing = deepcopy(p)
    missing.pop("qualified_shortfalls")
    mixed = deepcopy(missing)
    mixed["revision"] = 2
    for invalid in (pseudo, partial, missing, mixed):
        with pytest.raises(DiscoveryError, match="discovery_qualified_shortfalls"):
            validate_discovery_policy(invalid)
