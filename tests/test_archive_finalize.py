import base64
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest

from curator.archive_finalize import (
    ArchiveFinalizationError,
    _RejectRedirects,
    finalize_deployment,
)
from curator.archive_candidate import stamp_archive_candidate_site
from curator.pipeline import main as pipeline_main


SITE = b"<!doctype html><html><body>deployed edition</body></html>"


def _candidate(**changes) -> dict:
    candidate = {
        "schema_version": 1,
        "build_nonce": "123:1",
        "commit_sha": "a" * 40,
        "site_sha256": hashlib.sha256(SITE).hexdigest(),
        "built_at": "2026-09-07T12:00:00+00:00",
        "stories": [],
        "aliases": [],
        "coverage_mentions": [],
        "topics": [],
        "entries": [],
    }
    candidate.update(changes)
    return candidate


def _attestation(candidate: dict, **changes) -> dict:
    attestation = {
        "publication_seq": 41,
        "build_nonce": candidate["build_nonce"],
        "candidate_digest": "d" * 64,
        "commit_sha": candidate["commit_sha"],
        "deployed_url": "https://news.example.test/",
        "site_sha256": candidate["site_sha256"],
    }
    attestation.update(changes)
    return attestation


def _prune(**changes) -> dict:
    result = {
        "cutoff": "2026-08-07T12:00:00+00:00",
        "entries_pruned": 3,
        "topics_pruned": 2,
        "runs_pruned": 1,
        "saved_canonical_stories_preserved": 4,
        "receipts_pruned": 2,
    }
    result.update(changes)
    return result


def _legacy_service_key() -> str:
    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'HS256'})}.{encode({'role': 'service_role'})}.signature"


class _Response:
    def __init__(self, value: object, *, raw: bool = False) -> None:
        self.body = value if raw else json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def _write_candidate(tmp_path: Path, candidate: dict | None = None) -> Path:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate or _candidate()), encoding="utf-8")
    return path


def test_finalize_verifies_live_page_then_attests_and_prunes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = _candidate()
    path = _write_candidate(tmp_path, candidate)
    requests = []
    responses = iter(
        [_Response(SITE, raw=True), _Response(_attestation(candidate)), _Response(_prune())]
    )

    def fake_open(request, *, timeout):
        requests.append((request, timeout))
        return next(responses)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    receipt = finalize_deployment(
        candidate_path=path,
        deployed_url="https://news.example.test/",
        expected_commit="a" * 40,
        supabase_url="https://project.supabase.co/",
        service_key="sb_secret_test-only-value",
    )

    assert receipt["status"] == "finalized"
    assert receipt["publication_seq"] == 41
    assert receipt["candidate_digest"] == "d" * 64
    assert receipt["site_sha256"] == hashlib.sha256(SITE).hexdigest()
    assert [request.full_url for request, _ in requests] == [
        "https://news.example.test/",
        "https://project.supabase.co/rest/v1/rpc/finalize_archive",
        "https://project.supabase.co/rest/v1/rpc/prune_publication_history",
    ]
    assert requests[0][0].get_header("Apikey") is None
    assert requests[0][0].get_header("Authorization") is None
    assert requests[0][0].get_header("Accept-encoding") == "identity"
    assert requests[0][0].get_header("Cache-control") == "no-cache"
    assert requests[0][0].get_header("Pragma") == "no-cache"
    assert json.loads(requests[1][0].data) == {
        "p_candidate": candidate,
        "p_deployed_url": "https://news.example.test/",
    }
    assert json.loads(requests[2][0].data) == {}
    for request, timeout in requests[1:]:
        assert timeout == 30
        assert request.get_header("Authorization") is None
        assert request.get_header("Apikey") == "sb_secret_test-only-value"


def test_pipeline_candidate_is_restamped_after_final_site_mutation_before_finalize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "topics.yaml").write_text(
        "topics:\n  - name: AI\n    keywords:\n      - AI\n", encoding="utf-8"
    )
    (tmp_path / "sources.yaml").write_text("rss: []\n", encoding="utf-8")
    site = tmp_path / "site"
    candidate_path = tmp_path / "archive-candidate.json"
    assert pipeline_main([
        "--root", str(tmp_path), "--offline", "--allow-empty", "--out", str(site),
        "--archive-candidate", str(candidate_path), "--build-nonce", "integration:1",
        "--commit-sha", "a" * 40,
    ]) == 0
    initial = json.loads(candidate_path.read_text(encoding="utf-8"))
    site_index = site / "index.html"
    final_bytes = site_index.read_bytes() + b"\n<!-- auth callback materialized -->\n"
    site_index.write_bytes(final_bytes)

    stamp_archive_candidate_site(candidate_path, site_index)

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["site_sha256"] != initial["site_sha256"]
    assert candidate["site_sha256"] == hashlib.sha256(final_bytes).hexdigest()
    responses = iter([
        _Response(final_bytes, raw=True), _Response(_attestation(candidate)), _Response(_prune())
    ])
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: next(responses),
    )
    receipt = finalize_deployment(
        candidate_path=candidate_path,
        deployed_url="https://news.example.test/",
        expected_commit="a" * 40,
        supabase_url="https://project.supabase.co",
        service_key="sb_secret_test-only-value",
    )
    assert receipt["status"] == "finalized"
    assert receipt["site_sha256"] == hashlib.sha256(final_bytes).hexdigest()


def test_prune_is_not_called_when_finalize_response_is_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_candidate(tmp_path)
    calls = []
    responses = iter([_Response(SITE, raw=True), _Response({"unexpected": True})])

    def fake_open(request, *, timeout):
        calls.append(request)
        return next(responses)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    with pytest.raises(ArchiveFinalizationError, match="finalize_archive response"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )
    assert len(calls) == 2


def test_invalid_candidate_never_reaches_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_candidate(tmp_path, _candidate(commit_sha="not-a-commit"))
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("network call was not expected"),
    )
    with pytest.raises(ArchiveFinalizationError, match="commit"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )


def test_rpc_redirect_is_rejected_without_forwarding_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_candidate(tmp_path)
    secret = "sb_secret_test-only-value"
    requests = []

    def fake_open(request, *, timeout):
        requests.append(request)
        if len(requests) == 1:
            return _Response(SITE, raw=True)
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://attacker.example.test/collect"},
            io.BytesIO(b"redirect body containing sb_secret_test-only-value"),
        )

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    with pytest.raises(ArchiveFinalizationError) as caught:
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key=secret,
        )
    assert len(requests) == 2
    assert requests[1].get_header("Apikey") == secret
    assert "attacker" not in str(caught.value)
    assert secret not in str(caught.value)
    assert _RejectRedirects().redirect_request(None, None, None, None, None, None) is None


def test_live_page_eventually_matches_before_secret_rpc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = _candidate()
    path = _write_candidate(tmp_path)
    requests = []
    responses = iter(
        [
            _Response(b"prior deployed page", raw=True),
            _Response(SITE, raw=True),
            _Response(_attestation(candidate)),
            _Response(_prune()),
        ]
    )

    def fake_open(request, *, timeout):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    receipt = finalize_deployment(
        candidate_path=path,
        deployed_url="https://news.example.test/",
        expected_commit="a" * 40,
        supabase_url="https://project.supabase.co",
        service_key="sb_secret_test-only-value",
        verification_attempts=2,
        verification_delay_seconds=0,
    )
    assert receipt["publication_seq"] == 41
    assert len(requests) == 4
    for request in requests[:2]:
        assert request.get_header("Apikey") is None
        assert request.get_header("Authorization") is None


def test_live_page_hash_mismatch_exhausts_bounded_attempts_without_secret_rpc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_candidate(tmp_path)
    requests = []

    def fake_open(request, *, timeout):
        requests.append(request)
        return _Response(b"different deployed page", raw=True)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    with pytest.raises(ArchiveFinalizationError, match="failed after 3 attempts"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
            verification_attempts=3,
            verification_delay_seconds=0,
        )
    assert len(requests) == 3
    assert all(request.get_header("Apikey") is None for request in requests)
    assert all(request.get_header("Authorization") is None for request in requests)


@pytest.mark.parametrize(
    ("attempts", "delay"),
    [(0, 0), (31, 0), (2, -1), (2, 61), (30, 10)],
)
def test_invalid_verification_policy_blocks_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, attempts: int, delay: float
) -> None:
    path = _write_candidate(tmp_path)
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("network call was not expected"),
    )
    with pytest.raises(ArchiveFinalizationError, match="policy"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
            verification_attempts=attempts,
            verification_delay_seconds=delay,
        )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("build_nonce", "wrong-run"),
        ("commit_sha", "b" * 40),
        ("deployed_url", "https://other.example.test/"),
        ("site_sha256", "e" * 64),
        ("candidate_digest", "not-a-digest"),
    ],
)
def test_attestation_mismatch_blocks_prune(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, wrong_value: str
) -> None:
    candidate = _candidate()
    path = _write_candidate(tmp_path, candidate)
    calls = []
    responses = iter(
        [_Response(SITE, raw=True), _Response(_attestation(candidate, **{field: wrong_value}))]
    )

    def fake_open(request, *, timeout):
        calls.append(request)
        return next(responses)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    with pytest.raises(ArchiveFinalizationError, match="attestation"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "built_at",
    [
        "2026-09-07T12:00:00",
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    ],
)
def test_candidate_time_must_be_aware_and_not_implausibly_future(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, built_at: str
) -> None:
    path = _write_candidate(tmp_path, _candidate(built_at=built_at))
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("network call was not expected"),
    )
    with pytest.raises(ArchiveFinalizationError, match="build time"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )


def test_candidate_commit_mismatch_never_reaches_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_candidate(tmp_path)
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("network call was not expected"),
    )
    with pytest.raises(ArchiveFinalizationError, match="deployed commit"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="b" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )


@pytest.mark.parametrize(
    "supabase_url",
    [
        "https://project.supabase.co/rest/v1",
        "https://project.supabase.co?redirect=elsewhere",
        "https://*.supabase.co",
    ],
)
def test_supabase_url_must_be_one_exact_https_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, supabase_url: str
) -> None:
    path = _write_candidate(tmp_path)
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("network call was not expected"),
    )
    with pytest.raises(ArchiveFinalizationError, match="Supabase URL"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/edition?id=1",
            expected_commit="a" * 40,
            supabase_url=supabase_url,
            service_key="sb_secret_test-only-value",
        )


def test_legacy_service_role_jwt_uses_bearer_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = _candidate()
    path = _write_candidate(tmp_path, candidate)
    requests = []
    legacy = _legacy_service_key()
    responses = iter(
        [_Response(SITE, raw=True), _Response(_attestation(candidate)), _Response(_prune())]
    )

    def fake_open(request, *, timeout):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr("curator.archive_finalize._open_no_redirect", fake_open)
    finalize_deployment(
        candidate_path=path,
        deployed_url="https://news.example.test/",
        expected_commit="a" * 40,
        supabase_url="https://project.supabase.co",
        service_key=legacy,
    )
    assert requests[0].get_header("Authorization") is None
    assert all(
        request.get_header("Authorization") == f"Bearer {legacy}"
        for request in requests[1:]
    )


def test_prune_cutoff_must_be_an_aware_timestamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    candidate = _candidate()
    path = _write_candidate(tmp_path, candidate)
    responses = iter(
        [
            _Response(SITE, raw=True),
            _Response(_attestation(candidate)),
            _Response(_prune(cutoff="2026-08-07T12:00:00")),
        ]
    )
    monkeypatch.setattr(
        "curator.archive_finalize._open_no_redirect",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(ArchiveFinalizationError, match="prune_publication_history"):
        finalize_deployment(
            candidate_path=path,
            deployed_url="https://news.example.test/",
            expected_commit="a" * 40,
            supabase_url="https://project.supabase.co",
            service_key="sb_secret_test-only-value",
        )
