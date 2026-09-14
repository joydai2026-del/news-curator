#!/usr/bin/env python3
"""Reduce bound M2 receipts against a reviewed metrics-policy YAML file."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.m2_evaluation import reduce

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--checklist", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    policy_bytes=args.policy.read_bytes()
    checklist_bytes=args.checklist.read_bytes()
    policy=yaml.safe_load(policy_bytes)
    docs=[json.loads(path.read_text(encoding="utf-8")) for path in args.receipt]
    result=reduce(
        policy,
        docs,
        expected_policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        expected_checklist_sha256=hashlib.sha256(checklist_bytes).hexdigest(),
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    return 0 if result["status"] == "pass" else 2
if __name__ == "__main__": raise SystemExit(main())
