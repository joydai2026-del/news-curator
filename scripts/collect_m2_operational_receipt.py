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

import yaml


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_RESULT_MODES = {"model", "fallback"}
_FALLBACK_REASONS = {
    "", "budget_reservation_failed", "daily_cost_limit", "invalid_provider_permutation",
    "model_policy_mismatch", "no_candidates", "observed_cost_limit", "provider_deadline",
    "provider_failure", "provider_preparation_failed", "provider_preparation_unavailable",
    "provider_processing_consent_required", "provider_retry_exhausted", "provider_http_4xx",
    "provider_http_5xx", "provider_response_invalid", "provider_transport_failure", "request_cost_limit",
    "unknown_provider_pricing",
}


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Supabase URL must be a fixed HTTPS origin")
    return value


def _get_rows(origin: str, key: str, since: datetime) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    headers = {"apikey": key, "Accept": "application/json"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = "Bearer " + key
    opener = urllib.request.build_opener(_NoRedirect)
    while len(rows) < 10_000:
        query = urllib.parse.urlencode({"select": "created_at,bindings",
            "created_at": "gte." + since.isoformat(), "order": "created_at.asc",
            "limit": "1000", "offset": str(len(rows))})
        request = urllib.request.Request(origin + "/rest/v1/m2_frozen_rankings?" + query, headers=headers)
        with opener.open(request, timeout=10) as response:
            raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            raise ValueError("operational response is too large")
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(row, dict) for row in value):
            raise ValueError("invalid operational response")
        rows.extend(value)
        if len(value) < 1000:
            return rows
    raise ValueError("operational response exceeds 10000 rows")


def _category_ids(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_bytes())
    categories = document.get("categories") if isinstance(document, dict) else None
    values = [row.get("id") for row in categories] if isinstance(categories, list) else []
    if not values or any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values)):
        raise ValueError("invalid category registry")
    return values


def _receipt(rows: list[dict[str, object]], *, runtime_revision: str, policy_hash: str,
             checklist_hash: str, category_ids: list[str], environment: str,
             observed_at: datetime) -> dict[str, object]:
    modes, fallbacks = Counter(), Counter()
    attempted = successful = 0
    for row in rows:
        bindings = row.get("bindings")
        if not isinstance(bindings, dict):
            raise ValueError("invalid frozen ranking bindings")
        mode, reason, execution = bindings.get("result_mode"), bindings.get("fallback_reason"), bindings.get("execution")
        if not isinstance(mode, str) or not isinstance(reason, str) or not isinstance(execution, dict):
            raise ValueError("incomplete frozen ranking bindings")
        mode = mode if mode in _RESULT_MODES else "unknown"
        reason = reason if reason in _FALLBACK_REASONS else "unknown"
        modes[mode] += 1
        if reason:
            fallbacks[reason] += 1
        attempts = execution.get("attempts_started")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("invalid attempt count")
        attempted += attempts > 0
        successful += mode == "model"
    return {"schema_version": 1, "runtime_source_revision": runtime_revision,
        "row_source_revision_status": "unbound",
        "policy_sha256": policy_hash, "checklist_sha256": checklist_hash,
        "environment": environment, "observed_at_utc": observed_at.isoformat(),
        "configured_category_ids": category_ids,
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
    parser.add_argument("--topics", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--environment", required=True, choices=("production",))
    args = parser.parse_args()
    if (not args.runtime_revision or any(ch not in "0123456789abcdef" for ch in args.runtime_revision)
            or not 7 <= len(args.runtime_revision) <= 64):
        raise ValueError("invalid runtime revision")
    now = datetime.now(timezone.utc)
    rows = _get_rows(_origin(os.environ["NEWS_CURATOR_SUPABASE_URL"]),
        os.environ["NEWS_CURATOR_SUPABASE_SECRET_KEY"], now - timedelta(days=7))
    result = _receipt(rows, runtime_revision=args.runtime_revision,
        policy_hash=hashlib.sha256(args.policy.read_bytes()).hexdigest(),
        checklist_hash=hashlib.sha256(args.checklist.read_bytes()).hexdigest(),
        category_ids=_category_ids(args.topics), environment=args.environment, observed_at=now)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
