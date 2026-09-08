"""Finalize a deployed archive candidate through service-role Supabase RPCs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_CANDIDATE_BYTES = 4_000_000
MAX_CANDIDATE_FILE_BYTES = 8_000_000
MAX_DEPLOYED_PAGE_BYTES = 16_000_000
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 30
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=10)
DEFAULT_VERIFICATION_ATTEMPTS = 6
DEFAULT_VERIFICATION_DELAY_SECONDS = 10.0
MAX_VERIFICATION_WAIT_SECONDS = 240.0
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_KEYS = {
    "schema_version",
    "build_nonce",
    "commit_sha",
    "site_sha256",
    "built_at",
    "stories",
    "aliases",
    "coverage_mentions",
    "topics",
    "entries",
}
_PRUNE_KEYS = {
    "cutoff",
    "entries_pruned",
    "topics_pruned",
    "runs_pruned",
    "saved_canonical_stories_preserved",
    "receipts_pruned",
}
_FINALIZE_KEYS = {
    "publication_seq",
    "build_nonce",
    "candidate_digest",
    "commit_sha",
    "deployed_url",
    "site_sha256",
}


class ArchiveFinalizationError(RuntimeError):
    """Raised when a deployment cannot be safely recorded as published."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def _open_no_redirect(request: Request, *, timeout: int):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _read_candidate(
    path: Path, *, now: datetime | None = None
) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArchiveFinalizationError("archive candidate is unavailable") from exc
    if not raw or len(raw) > MAX_CANDIDATE_FILE_BYTES:
        raise ArchiveFinalizationError("archive candidate size is invalid")
    try:
        candidate = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveFinalizationError("archive candidate JSON is invalid") from exc
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
        raise ArchiveFinalizationError("archive candidate schema is invalid")
    if candidate["schema_version"] != 1:
        raise ArchiveFinalizationError("archive candidate version is invalid")
    nonce = candidate["build_nonce"]
    if not isinstance(nonce, str) or not nonce or len(nonce.encode("utf-8")) > 1_000:
        raise ArchiveFinalizationError("archive candidate build nonce is invalid")
    if not isinstance(candidate["commit_sha"], str) or not _SHA.fullmatch(
        candidate["commit_sha"]
    ):
        raise ArchiveFinalizationError("archive candidate commit is invalid")
    try:
        built_at = datetime.fromisoformat(str(candidate["built_at"]))
    except ValueError as exc:
        raise ArchiveFinalizationError("archive candidate build time is invalid") from exc
    if built_at.tzinfo is None or built_at.utcoffset() is None:
        raise ArchiveFinalizationError("archive candidate build time must include a timezone")
    current_time = now or datetime.now(timezone.utc)
    if built_at.astimezone(timezone.utc) > current_time + MAX_FUTURE_CLOCK_SKEW:
        raise ArchiveFinalizationError("archive candidate build time is implausibly future")
    if not isinstance(candidate["site_sha256"], str) or not _SHA256.fullmatch(
        candidate["site_sha256"]
    ):
        raise ArchiveFinalizationError("archive candidate site digest is invalid")
    for key, maximum in (
        ("stories", 500),
        ("aliases", 500),
        ("coverage_mentions", 5_000),
        ("topics", 100),
        ("entries", 5_000),
    ):
        value = candidate[key]
        if not isinstance(value, list) or len(value) > maximum:
            raise ArchiveFinalizationError(f"archive candidate {key} is invalid")
    canonical = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(canonical) > MAX_CANDIDATE_BYTES:
        raise ArchiveFinalizationError("archive candidate size is invalid")
    return candidate, hashlib.sha256(canonical).hexdigest()


def _validate_https_url(value: str, *, label: str) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > 8_192
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ArchiveFinalizationError(f"{label} is invalid")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ArchiveFinalizationError(f"{label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or "*" in parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ArchiveFinalizationError(f"{label} is invalid")
    return value


def _validate_supabase_origin(value: str) -> str:
    value = _validate_https_url(value, label="Supabase URL")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or "*" in parsed.hostname
    ):
        raise ArchiveFinalizationError("Supabase URL is invalid")
    return value.rstrip("/")


def _legacy_service_role_key(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3 or not parts[1]:
        return False
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "service_role"


def _validate_service_key(value: str) -> str:
    if not value or len(value) > 8_192:
        raise ArchiveFinalizationError("Supabase service credential is unavailable")
    if value.startswith("sb_secret_"):
        valid = len(value) > len("sb_secret_")
    else:
        valid = _legacy_service_role_key(value)
    if not valid:
        raise ArchiveFinalizationError("Supabase service credential is invalid")
    return value


def _rpc(
    supabase_url: str,
    service_key: str,
    function_name: str,
    payload: dict[str, Any],
) -> Any:
    url = f"{supabase_url}/rest/v1/rpc/{function_name}"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "apikey": service_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if not service_key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {service_key}"
    request = Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with _open_no_redirect(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ArchiveFinalizationError(
            f"Supabase {function_name} RPC failed with HTTP {exc.code}"
        ) from exc
    except (OSError, URLError) as exc:
        raise ArchiveFinalizationError(f"Supabase {function_name} RPC failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ArchiveFinalizationError(f"Supabase {function_name} response is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveFinalizationError(
            f"Supabase {function_name} response is invalid"
        ) from exc


def _fetch_deployed_page(deployed_url: str) -> bytes:
    request = Request(
        deployed_url,
        method="GET",
        headers={
            "Accept": "text/html",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with _open_no_redirect(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_DEPLOYED_PAGE_BYTES + 1)
    except HTTPError as exc:
        raise ArchiveFinalizationError(
            f"deployed page verification failed with HTTP {exc.code}"
        ) from exc
    except (OSError, URLError) as exc:
        raise ArchiveFinalizationError("deployed page verification failed") from exc
    if not raw or len(raw) > MAX_DEPLOYED_PAGE_BYTES:
        raise ArchiveFinalizationError("deployed page response size is invalid")
    return raw


def _validate_verification_policy(attempts: int, delay_seconds: float) -> None:
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 1
        or attempts > 30
        or isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, (int, float))
        or delay_seconds < 0
        or delay_seconds > 60
        or (attempts - 1) * delay_seconds > MAX_VERIFICATION_WAIT_SECONDS
    ):
        raise ArchiveFinalizationError("deployed page verification policy is invalid")


def _verify_deployed_page(
    deployed_url: str,
    expected_digest: str,
    *,
    attempts: int,
    delay_seconds: float,
) -> None:
    _validate_verification_policy(attempts, delay_seconds)
    last_error: ArchiveFinalizationError | None = None
    for attempt in range(attempts):
        try:
            deployed_page = _fetch_deployed_page(deployed_url)
        except ArchiveFinalizationError as exc:
            last_error = exc
        else:
            if hashlib.sha256(deployed_page).hexdigest() == expected_digest:
                return
            last_error = ArchiveFinalizationError(
                "deployed page does not match archive candidate"
            )
        if attempt + 1 < attempts and delay_seconds:
            time.sleep(delay_seconds)
    raise ArchiveFinalizationError(
        f"deployed page verification failed after {attempts} attempts"
    ) from last_error


def finalize_deployment(
    *,
    candidate_path: Path,
    deployed_url: str,
    expected_commit: str,
    supabase_url: str,
    service_key: str,
    verification_attempts: int = DEFAULT_VERIFICATION_ATTEMPTS,
    verification_delay_seconds: float = DEFAULT_VERIFICATION_DELAY_SECONDS,
) -> dict[str, Any]:
    """Archive one deployed candidate, then prune expired publication history."""
    candidate, local_digest = _read_candidate(candidate_path)
    if not _SHA.fullmatch(expected_commit) or candidate["commit_sha"] != expected_commit:
        raise ArchiveFinalizationError("archive candidate does not match deployed commit")
    base_url = _validate_supabase_origin(supabase_url)
    deployed_url = _validate_https_url(deployed_url, label="deployed URL")
    _verify_deployed_page(
        deployed_url,
        candidate["site_sha256"],
        attempts=verification_attempts,
        delay_seconds=verification_delay_seconds,
    )
    service_key = _validate_service_key(service_key)
    attestation = _rpc(
        base_url,
        service_key,
        "finalize_archive",
        {"p_candidate": candidate, "p_deployed_url": deployed_url},
    )
    if not isinstance(attestation, dict) or set(attestation) != _FINALIZE_KEYS:
        raise ArchiveFinalizationError("Supabase finalize_archive response is invalid")
    publication_seq = attestation["publication_seq"]
    if (
        isinstance(publication_seq, bool)
        or not isinstance(publication_seq, int)
        or publication_seq < 1
        or attestation["build_nonce"] != candidate["build_nonce"]
        or attestation["commit_sha"] != candidate["commit_sha"]
        or attestation["deployed_url"] != deployed_url
        or attestation["site_sha256"] != candidate["site_sha256"]
        or not isinstance(attestation["candidate_digest"], str)
        or not _SHA256.fullmatch(attestation["candidate_digest"])
    ):
        raise ArchiveFinalizationError("Supabase finalize_archive attestation is invalid")
    prune = _rpc(base_url, service_key, "prune_publication_history", {})
    if not isinstance(prune, dict) or set(prune) != _PRUNE_KEYS:
        raise ArchiveFinalizationError("Supabase prune_publication_history response is invalid")
    for key in _PRUNE_KEYS - {"cutoff"}:
        value = prune[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArchiveFinalizationError(
                "Supabase prune_publication_history response is invalid"
            )
    try:
        cutoff = datetime.fromisoformat(prune["cutoff"])
    except (TypeError, ValueError) as exc:
        raise ArchiveFinalizationError(
            "Supabase prune_publication_history response is invalid"
        ) from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ArchiveFinalizationError("Supabase prune_publication_history response is invalid")
    return {
        "status": "finalized",
        "publication_seq": publication_seq,
        "candidate_digest": attestation["candidate_digest"],
        "site_sha256": candidate["site_sha256"],
        "artifact_sha256": local_digest,
        "prune": prune,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Finalize a successfully deployed News Curator archive candidate."
    )
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--deployed-url", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument(
        "--verification-attempts",
        type=int,
        default=os.environ.get(
            "NEWS_CURATOR_DEPLOY_VERIFY_ATTEMPTS", str(DEFAULT_VERIFICATION_ATTEMPTS)
        ),
    )
    parser.add_argument(
        "--verification-delay-seconds",
        type=float,
        default=os.environ.get(
            "NEWS_CURATOR_DEPLOY_VERIFY_DELAY_SECONDS",
            str(DEFAULT_VERIFICATION_DELAY_SECONDS),
        ),
    )
    args = parser.parse_args(argv)
    supabase_url = os.environ.get("NEWS_CURATOR_SUPABASE_URL", "")
    service_key = os.environ.get("NEWS_CURATOR_SUPABASE_SECRET_KEY", "")
    receipt = finalize_deployment(
        candidate_path=args.candidate,
        deployed_url=args.deployed_url,
        expected_commit=args.expected_commit,
        supabase_url=supabase_url,
        service_key=service_key,
        verification_attempts=args.verification_attempts,
        verification_delay_seconds=args.verification_delay_seconds,
    )
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
