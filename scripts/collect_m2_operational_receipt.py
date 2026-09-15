#!/usr/bin/env python3
"""Collect a sanitized, fail-closed M2 operational receipt from Supabase."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Supabase URL must be a fixed HTTPS origin")
    return value


def _get_rows(origin: str, key: str, since: datetime) -> list[dict[str, object]]:
    query = urllib.parse.urlencode({"select": "created_at,bindings",
        "created_at": "gte." + since.isoformat(), "order": "created_at.asc", "limit": "1001"})
    headers = {"apikey": key, "Accept": "application/json"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(origin + "/rest/v1/m2_frozen_rankings?" + query, headers=headers)
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
        raw = response.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError("operational response is too large")
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(row, dict) for row in value):
        raise ValueError("invalid operational response")
    return value


def _receipt(rows: list[dict[str, object]], *, commit: str, policy_hash: str,
             checklist_hash: str, observed_at: datetime) -> dict[str, object]:
    modes, fallbacks = Counter(), Counter()
    attempted = successful = 0
    for row in rows:
        bindings = row.get("bindings")
        if not isinstance(bindings, dict):
            raise ValueError("invalid frozen ranking bindings")
        mode, reason, execution = bindings.get("result_mode"), bindings.get("fallback_reason"), bindings.get("execution")
        if not isinstance(mode, str) or not isinstance(reason, str) or not isinstance(execution, dict):
            raise ValueError("incomplete frozen ranking bindings")
        modes[mode] += 1
        if reason:
            fallbacks[reason] += 1
        attempts = execution.get("attempts_started")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid attempt count")
        attempted += attempts > 0
        successful += mode == "model"
    return {"schema_version": 1, "git_commit_sha": commit,
        "policy_sha256": policy_hash, "checklist_sha256": checklist_hash,
        "observed_at_utc": observed_at.isoformat(), "configured_category_ids": [],
        "freshness": [], "coverage_sentinels": [], "ranked_slates": [], "search_queries": [],
        "slice_judgments": [], "profile_updates": [], "profile_visibility": [],
        "operational_model_path": {"window_days": 7, "frozen_responses": len(rows),
            "result_modes": dict(sorted(modes.items())), "fallback_reasons": dict(sorted(fallbacks.items())),
            "requests_with_provider_attempt": attempted, "recorded_model_results": successful,
            "status": "insufficient_evidence",
            "reason": "frozen results omit failed request denominator and cold/warm classification"}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--checklist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if not args.commit or any(ch not in "0123456789abcdef" for ch in args.commit) or not 7 <= len(args.commit) <= 64:
        raise ValueError("invalid commit")
    now = datetime.now(timezone.utc)
    rows = _get_rows(_origin(os.environ["NEWS_CURATOR_SUPABASE_URL"]),
        os.environ["NEWS_CURATOR_SUPABASE_SECRET_KEY"], now - timedelta(days=7))
    result = _receipt(rows, commit=args.commit,
        policy_hash=hashlib.sha256(args.policy.read_bytes()).hexdigest(),
        checklist_hash=hashlib.sha256(args.checklist.read_bytes()).hexdigest(), observed_at=now)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
