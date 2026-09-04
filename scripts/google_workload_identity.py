"""Exchange GitHub OIDC for a short-lived Google service-account token.

Every request uses the repository's SafeHttpTransport. Tokens are never
printed, and the final token is written to a new owner-only file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# This script is executed as ``python scripts/google_workload_identity.py`` in
# Actions, which does not automatically make the repository package importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curator.sources import OriginBoundCredential, SafeHttpResponse, SafeHttpTransport  # noqa: E402


GITHUB_OIDC_ORIGIN = "https://pipelines.actions.githubusercontent.com"
GOOGLE_STS_ORIGIN = "https://sts.googleapis.com"
GOOGLE_STS_ENDPOINT = f"{GOOGLE_STS_ORIGIN}/v1/token"
GOOGLE_IAM_ORIGIN = "https://iamcredentials.googleapis.com"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_PROVIDER = re.compile(
    r"^projects/[0-9]{1,20}/locations/global/"
    r"workloadIdentityPools/[A-Za-z0-9_-]{4,32}/providers/[A-Za-z0-9_-]{4,32}$"
)
_SERVICE_ACCOUNT = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)
_RUNTIME_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TOKEN_TYPES = frozenset(("Bearer", "bearer"))
_MAX_JSON_BYTES = 256 * 1024
_MAX_TOKEN_CHARS = 64 * 1024


class WorkloadIdentityError(RuntimeError):
    """A deliberately non-sensitive workload-identity failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"google workload identity failed: {reason}")


@dataclass(frozen=True)
class WorkloadIdentityConfig:
    provider: str
    service_account: str

    def __post_init__(self) -> None:
        if not _PROVIDER.fullmatch(self.provider):
            raise WorkloadIdentityError("invalid_provider")
        if not _SERVICE_ACCOUNT.fullmatch(self.service_account):
            raise WorkloadIdentityError("invalid_service_account")

    @property
    def audience(self) -> str:
        return f"//iam.googleapis.com/{self.provider}"

    @property
    def iam_endpoint(self) -> str:
        return (
            f"{GOOGLE_IAM_ORIGIN}/v1/projects/-/serviceAccounts/"
            f"{self.service_account}:generateAccessToken"
        )


def acquire_google_access_token(
    *,
    config: WorkloadIdentityConfig,
    oidc_request_url: str,
    oidc_request_token: str,
    transport: SafeHttpTransport,
) -> str:
    """Return one short-lived Google access token without exposing intermediates."""

    request_token = _validated_token(oidc_request_token, "invalid_oidc_request_token")
    oidc_url = _github_oidc_url(oidc_request_url, config.audience)
    oidc = _request_json(
        transport,
        source_id="github-oidc",
        method="GET",
        url=oidc_url,
        credential=OriginBoundCredential(
            origin=GITHUB_OIDC_ORIGIN,
            header_name="Authorization",
            value=f"Bearer {request_token}",
        ),
    )
    subject_token = _validated_token(oidc.get("value"), "invalid_oidc_response")

    sts = _request_json(
        transport,
        source_id="google-sts",
        method="POST",
        url=GOOGLE_STS_ENDPOINT,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=_json_bytes(
            {
                "audience": config.audience,
                "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
                "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
                "scope": GOOGLE_SCOPE,
                "subjectToken": subject_token,
                "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt",
            }
        ),
    )
    if sts.get("token_type") not in _TOKEN_TYPES:
        raise WorkloadIdentityError("invalid_sts_response")
    federated_token = _validated_token(sts.get("access_token"), "invalid_sts_response")
    expires_in = sts.get("expires_in")
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or not 1 <= expires_in <= 3_600:
        raise WorkloadIdentityError("invalid_sts_response")

    iam = _request_json(
        transport,
        source_id="google-iam",
        method="POST",
        url=config.iam_endpoint,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=_json_bytes({"scope": [GOOGLE_SCOPE], "lifetime": "3600s"}),
        credential=OriginBoundCredential(
            origin=GOOGLE_IAM_ORIGIN,
            header_name="Authorization",
            value=f"Bearer {federated_token}",
        ),
    )
    return _validated_token(iam.get("accessToken"), "invalid_iam_response")


def write_token_file(path: Path, token: str) -> None:
    """Create a new non-symlink token file with owner-only permissions."""

    safe_token = _validated_token(token, "invalid_access_token")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        encoded = safe_token.encode("ascii")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short token-file write")
            offset += written
        os.fsync(descriptor)
    except (OSError, UnicodeEncodeError):
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise WorkloadIdentityError("token_file_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _github_oidc_url(raw_url: str, audience: str) -> str:
    try:
        parts = urlsplit(raw_url)
        port = parts.port
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise WorkloadIdentityError("invalid_oidc_endpoint") from None
    segments = parts.path.split("/")
    valid_runtime_path = (
        len(segments) == 12
        and segments[0] == ""
        and _RUNTIME_TOKEN.fullmatch(segments[1]) is not None
        and _UUID.fullmatch(segments[2]) is not None
        and segments[3:8] == ["_apis", "distributedtask", "hubs", "build", "plans"]
        and _UUID.fullmatch(segments[8]) is not None
        and segments[9] == "jobs"
        and _UUID.fullmatch(segments[10]) is not None
        and segments[11] == "idtoken"
    )
    if (
        parts.scheme != "https"
        or parts.hostname != "pipelines.actions.githubusercontent.com"
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not valid_runtime_path
        or parts.fragment
        or query != [("api-version", "2.0")]
    ):
        raise WorkloadIdentityError("invalid_oidc_endpoint")
    query.append(("audience", audience))
    encoded = urlencode(query)
    if len(encoded) > 4_096:
        raise WorkloadIdentityError("invalid_oidc_endpoint")
    return urlunsplit(("https", "pipelines.actions.githubusercontent.com", parts.path, encoded, ""))


def _request_json(
    transport: SafeHttpTransport,
    *,
    source_id: str,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    credential: OriginBoundCredential | None = None,
) -> Mapping[str, object]:
    try:
        response: SafeHttpResponse = transport.request(
            source_id,
            method,
            url,
            headers={"Accept": "application/json", **dict(headers or {})},
            body=body,
            credential=credential,
            allowed_mime_types=("application/json",),
        )
    except Exception:
        raise WorkloadIdentityError(f"{source_id}_request_failed") from None
    if response.status_code != 200:
        raise WorkloadIdentityError(f"{source_id}_rejected")
    if len(response.body) > _MAX_JSON_BYTES:
        raise WorkloadIdentityError(f"{source_id}_response_too_large")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WorkloadIdentityError(f"{source_id}_invalid_json") from None
    if not isinstance(value, dict) or not _bounded_json(value):
        raise WorkloadIdentityError(f"{source_id}_invalid_json")
    return value


def _bounded_json(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 1_000 or depth > 8:
            return False
        if isinstance(current, str):
            if len(current) > _MAX_TOKEN_CHARS:
                return False
        elif isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > 100:
                    return False
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif current is not None and not isinstance(current, (bool, int, float)):
            return False
    return True


def _json_bytes(value: Mapping[str, object]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > _MAX_JSON_BYTES:
        raise WorkloadIdentityError("request_too_large")
    return encoded


def _validated_token(value: object, reason: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_TOKEN_CHARS:
        raise WorkloadIdentityError(reason)
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise WorkloadIdentityError(reason)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = WorkloadIdentityConfig(args.provider, args.service_account)
        token = acquire_google_access_token(
            config=config,
            oidc_request_url=os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
            oidc_request_token=os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
            transport=SafeHttpTransport(),
        )
        write_token_file(args.output, token)
    except Exception:
        print("Google workload identity exchange failed", file=sys.stderr)
        return 1
    print("Google access token written to an owner-only file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
