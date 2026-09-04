"""Deterministic tests for the GitHub OIDC to Google token exchange."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from curator.sources import SafeHttpResponse
from scripts.google_workload_identity import (
    GOOGLE_IAM_ORIGIN,
    GOOGLE_STS_ENDPOINT,
    GITHUB_OIDC_ORIGIN,
    WorkloadIdentityConfig,
    WorkloadIdentityError,
    acquire_google_access_token,
    write_token_file,
)


PROVIDER = (
    "projects/123456789/locations/global/"
    "workloadIdentityPools/news-curator/providers/github-actions"
)
SERVICE_ACCOUNT = "news-curator@news-curator-prod.iam.gserviceaccount.com"
OIDC_URL = (
    "https://pipelines.actions.githubusercontent.com/"
    "JIdNBcQ6kMWGQ1Emvk4eWkVClMzQPdc1GeXypChXNSBsqbvPVO/"
    "00000000-0000-0000-0000-000000000000/"
    "_apis/distributedtask/hubs/build/plans/"
    "99435d5c-744f-40ef-8ace-52f2f419aeed/jobs/"
    "f72e1371-574f-5793-8b43-f4df44fd5814/idtoken?api-version=2.0"
)
ROOT = Path(__file__).parents[1]


def test_workflow_direct_script_entrypoint_imports_repo_package() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/google_workload_identity.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "--service-account" in result.stdout


class FakeTransport:
    def __init__(self, responses: list[dict[str, object] | bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, source_id: str, method: str, url: str, **kwargs: object) -> SafeHttpResponse:
        self.calls.append({"source_id": source_id, "method": method, "url": url, **kwargs})
        payload = self.responses.pop(0)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return SafeHttpResponse(200, url, {"content-type": "application/json"}, body)


def _exchange(transport: FakeTransport) -> str:
    return acquire_google_access_token(
        config=WorkloadIdentityConfig(PROVIDER, SERVICE_ACCOUNT),
        oidc_request_url=OIDC_URL,
        oidc_request_token="github-request-token",
        transport=transport,  # type: ignore[arg-type]
    )


def test_exchange_uses_fixed_endpoints_and_origin_bound_credentials() -> None:
    transport = FakeTransport(
        [
            {"value": "github-subject-token"},
            {"access_token": "federated-token", "token_type": "Bearer", "expires_in": 600},
            {"accessToken": "google-access-token", "expireTime": "2026-08-30T00:00:00Z"},
        ]
    )
    assert _exchange(transport) == "google-access-token"
    oidc, sts, iam = transport.calls
    assert oidc["method"] == "GET"
    assert str(oidc["url"]).startswith(f"{OIDC_URL}&audience=")
    assert "audience=%2F%2Fiam.googleapis.com%2Fprojects%2F123456789" in str(oidc["url"])
    oidc_credential = oidc["credential"]
    assert oidc_credential.origin == GITHUB_OIDC_ORIGIN  # type: ignore[union-attr]
    assert oidc_credential.value == "Bearer github-request-token"  # type: ignore[union-attr]

    assert sts["url"] == GOOGLE_STS_ENDPOINT
    assert sts["credential"] is None
    sts_body = json.loads(sts["body"])  # type: ignore[arg-type]
    assert sts_body["subjectToken"] == "github-subject-token"
    assert sts_body["audience"] == f"//iam.googleapis.com/{PROVIDER}"

    assert iam["url"] == (
        f"{GOOGLE_IAM_ORIGIN}/v1/projects/-/serviceAccounts/"
        f"{SERVICE_ACCOUNT}:generateAccessToken"
    )
    iam_credential = iam["credential"]
    assert iam_credential.origin == GOOGLE_IAM_ORIGIN  # type: ignore[union-attr]
    assert iam_credential.value == "Bearer federated-token"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("provider", "service_account"),
    [
        ("projects/1/locations/us/pools/p/providers/x", SERVICE_ACCOUNT),
        (PROVIDER, "owner@example.com"),
        ("//iam.googleapis.com/" + PROVIDER, SERVICE_ACCOUNT),
    ],
)
def test_config_rejects_noncanonical_resources(provider: str, service_account: str) -> None:
    with pytest.raises(WorkloadIdentityError):
        WorkloadIdentityConfig(provider, service_account)


@pytest.mark.parametrize(
    "url",
    [
        OIDC_URL.replace("https://", "http://", 1),
        OIDC_URL.replace("pipelines.actions.githubusercontent.com", "evil.example", 1),
        OIDC_URL.replace("/idtoken?", "/not-token?", 1),
        OIDC_URL.replace("/hubs/build/", "/hubs/Actions/", 1),
        OIDC_URL + "&audience=attacker",
        OIDC_URL.replace("api-version=2.0", "api-version=1.0"),
    ],
)
def test_oidc_endpoint_is_fixed_before_any_request(url: str) -> None:
    transport = FakeTransport([])
    with pytest.raises(WorkloadIdentityError, match="invalid_oidc_endpoint"):
        acquire_google_access_token(
            config=WorkloadIdentityConfig(PROVIDER, SERVICE_ACCOUNT),
            oidc_request_url=url,
            oidc_request_token="github-request-token",
            transport=transport,  # type: ignore[arg-type]
        )
    assert transport.calls == []


def test_malformed_or_oversized_json_fails_without_token_leak() -> None:
    sentinel = "github-request-token-DO-NOT-LEAK"
    transport = FakeTransport([b"{" + b"x" * (256 * 1024)])
    with pytest.raises(WorkloadIdentityError) as caught:
        acquire_google_access_token(
            config=WorkloadIdentityConfig(PROVIDER, SERVICE_ACCOUNT),
            oidc_request_url=OIDC_URL,
            oidc_request_token=sentinel,
            transport=transport,  # type: ignore[arg-type]
        )
    assert sentinel not in str(caught.value)


def test_token_file_is_owner_only_and_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "google-token"
    write_token_file(output, "google-access-token")
    assert output.read_text(encoding="ascii") == "google-access-token"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(WorkloadIdentityError, match="token_file_write_failed"):
        write_token_file(output, "replacement-token")
    assert output.read_text(encoding="ascii") == "google-access-token"


def test_successful_exchange_does_not_print_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    transport = FakeTransport(
        [
            {"value": "github-subject-token"},
            {"access_token": "federated-token", "token_type": "Bearer", "expires_in": 600},
            {"accessToken": "google-access-token"},
        ]
    )
    assert _exchange(transport) == "google-access-token"
    captured = capsys.readouterr()
    for token in ("github-subject-token", "federated-token", "google-access-token"):
        assert token not in captured.out
        assert token not in captured.err
