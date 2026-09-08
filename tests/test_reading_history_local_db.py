from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

import pytest

from tests._personalization_local_harness import LocalSupabase, LocalUser


pytestmark = pytest.mark.allow_socket

STORIES = tuple(
    (
        "story:" + hashlib.sha256(url.encode("utf-8")).hexdigest(),
        url,
    )
    for url in (
        "https://publisher.example/security-one",
        "https://publisher.example/security-two",
    )
)


def _service_request(
    supabase: LocalSupabase, method: str, path: str, *, body: object | None = None
) -> tuple[int, object]:
    return supabase.request(
        method,
        path,
        apikey=supabase.service_key,
        bearer=supabase.service_key,
        body=body,
        prefer="return=representation",
    )


def _seed_archive(supabase: LocalSupabase) -> None:
    built_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "story_id": story_id,
            "canonical_url": url,
            "title": f"Security story {position}",
            "summary": f"Public summary {position}",
            "language": "en",
            "published_at": built_at,
            "source_kind": "outlet",
            "source_name": "Publisher",
            "distinct_coverage_source_count": 1,
        }
        for position, (story_id, url) in enumerate(STORIES, start=1)
    ]
    candidate = {
        "schema_version": 1,
        "build_nonce": f"local-security-{secrets.token_hex(12)}",
        "commit_sha": "a" * 40,
        "site_sha256": "b" * 64,
        "built_at": built_at,
        "stories": rows,
        "aliases": [
            {"normalized_url": url, "story_id": story_id, "match_method": "exact"}
            for story_id, url in STORIES
        ],
        "coverage_mentions": [],
        "topics": [{"topic_id": "ai", "name": "AI"}],
        "entries": [
            {
                "story_id": story_id,
                "topic_id": "ai",
                "position": position,
                "score_components": {"freshness": 1, "final_score": 1},
                "ordering_mode": "weighted_total",
                "ordering_key": {"weighted_total": 1},
                "topic_ranks": {"ai": position},
                "source_kind": "outlet",
                "source_name": "Publisher",
                "ranking_explanation": "Weighted using freshness.",
            }
            for position, (story_id, _url) in enumerate(STORIES, start=1)
        ],
    }
    status, result = _service_request(
        supabase,
        "POST",
        "/rest/v1/rpc/finalize_archive",
        body={"p_candidate": candidate, "p_deployed_url": "https://news.example/"},
    )
    assert status == 200
    assert isinstance(result, dict) and result["build_nonce"] == candidate["build_nonce"]


def _state_body(
    *, story_id: str = STORIES[0][0], read: bool = True, saved: bool = True,
    revision: int = 0, key: str,
) -> dict[str, object]:
    return {
        "p_story_id": story_id,
        "p_read": read,
        "p_saved": saved,
        "p_expected_revision": revision,
        "p_idempotency_key": key,
    }


def _interest_body(
    *, story_id: str = STORIES[0][0], topic_id: str = "ai", signal: str = "more_like",
    revision: int = 0, key: str,
) -> dict[str, object]:
    return {
        "p_story_id": story_id,
        "p_topic_id": topic_id,
        "p_signal": signal,
        "p_expected_revision": revision,
        "p_idempotency_key": key,
    }


def _rpc(
    supabase: LocalSupabase, user: LocalUser, name: str, body: dict[str, object]
) -> tuple[int, object]:
    return supabase.rest(user, "POST", f"/rest/v1/rpc/{name}", body=body)


def test_public_reads_are_anonymous_safe_and_expired_jwts_are_denied() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        _seed_archive(supabase)
        owner = supabase.create_user()
        for name, body in (
            ("latest_publication", {}),
            (
                "feed_page",
                {
                    "p_topic_id": "ai",
                    "p_order_mode": "edition_rank",
                    "p_after_position": None,
                    "p_after_story_id": None,
                    "p_before_published_at": None,
                    "p_before_story_id": None,
                    "p_limit": 20,
                },
            ),
            (
                "updates_since",
                {
                    "p_since_publication_seq": 0,
                    "p_after_publication_seq": None,
                    "p_after_published_at": None,
                    "p_after_story_id": None,
                    "p_limit": 20,
                },
            ),
        ):
            status, payload = supabase.request(
                "POST", f"/rest/v1/rpc/{name}", apikey=supabase.anon_key, body=body
            )
            assert status == 200, name
            if name == "feed_page":
                assert payload
                assert all(row["read_at"] is None for row in payload)
                assert all(row["saved_at"] is None for row in payload)
                assert all(row["interests"] == [] for row in payload)

        expired = supabase.expired_access_token(owner.user_id)
        status, _ = supabase.request(
            "POST",
            "/rest/v1/rpc/feed_page",
            apikey=supabase.anon_key,
            bearer=expired,
            body={
                "p_topic_id": "ai",
                "p_order_mode": "edition_rank",
                "p_after_position": None,
                "p_after_story_id": None,
                "p_before_published_at": None,
                "p_before_story_id": None,
                "p_limit": 20,
            },
        )
        assert status in (401, 403)
    finally:
        supabase.cleanup()


def test_private_state_and_interest_are_owner_scoped_with_cas_and_idempotency() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        _seed_archive(supabase)
        owner = supabase.create_user()
        other = supabase.create_user()

        state = _state_body(key="state-create")
        status, created = _rpc(supabase, owner, "set_story_state", state)
        assert status == 200 and created["status"] == "updated" and created["revision"] == 1
        assert _rpc(supabase, owner, "set_story_state", state) == (status, created)

        status, _ = _rpc(
            supabase, owner, "set_story_state",
            _state_body(read=False, saved=True, key="state-create"),
        )
        assert status == 400
        status, conflict = _rpc(
            supabase, owner, "set_story_state", _state_body(key="state-stale")
        )
        assert status == 200 and conflict == {"status": "conflict", "revision": 1}

        interest = _interest_body(key="interest-create")
        status, created_interest = _rpc(supabase, owner, "set_story_interest", interest)
        assert status == 200
        assert created_interest == {"status": "updated", "revision": 1, "signal": "more_like"}
        assert _rpc(supabase, owner, "set_story_interest", interest) == (status, created_interest)

        status, _ = _rpc(
            supabase, owner, "set_story_interest",
            _interest_body(signal="less_like", key="interest-create"),
        )
        assert status == 400
        status, conflict = _rpc(
            supabase, owner, "set_story_interest", _interest_body(key="interest-stale")
        )
        assert status == 200 and conflict == {"status": "conflict", "revision": 1}

        status, owner_saved = _rpc(
            supabase, owner, "saved_page",
            {"p_before_saved_at": None, "p_before_story_id": None, "p_limit": 20},
        )
        assert status == 200 and len(owner_saved) == 1
        assert owner_saved[0]["interests"] == [
            {"topic_id": "ai", "signal": "more_like", "revision": 1}
        ]
        status, other_saved = _rpc(
            supabase, other, "saved_page",
            {"p_before_saved_at": None, "p_before_story_id": None, "p_limit": 20},
        )
        assert status == 200 and other_saved == []

        status, removed = _rpc(
            supabase, owner, "set_story_state",
            _state_body(read=False, saved=False, revision=1, key="state-remove"),
        )
        assert status == 200 and removed["revision"] == 2
        assert removed["read_at"] is None and removed["saved_at"] is None
        status, changed_interest = _rpc(
            supabase, owner, "set_story_interest",
            _interest_body(signal="less_like", revision=1, key="interest-update"),
        )
        assert status == 200
        assert changed_interest == {"status": "updated", "revision": 2, "signal": "less_like"}
    finally:
        supabase.cleanup()


def test_direct_tables_and_privileged_rpcs_are_not_client_accessible() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        _seed_archive(supabase)
        owner = supabase.create_user()
        tables = (
            "feed_policy", "canonical_stories", "story_aliases", "coverage_mentions",
            "story_topics", "publication_runs", "publication_topics", "publication_entries",
            "user_story_state", "user_story_interests", "user_action_receipts",
        )
        for table in tables:
            path = f"/rest/v1/{table}?select=*"
            assert supabase.request("GET", path, apikey=supabase.anon_key)[0] in (401, 403)
            assert supabase.rest(owner, "GET", path)[0] in (401, 403)
        state_path = f"/rest/v1/user_story_state?story_id=eq.{STORIES[0][0]}"
        assert supabase.rest(owner, "PATCH", state_path, body={"revision": 99})[0] in (401, 403)
        assert supabase.rest(owner, "DELETE", state_path)[0] in (401, 403)

        for name, body in (
            ("finalize_archive", {"p_candidate": {}, "p_deployed_url": "https://news.example/"}),
            ("prune_publication_history", {}),
            ("materialize_user_interest_signals", {"p_user_id": owner.user_id}),
        ):
            path = f"/rest/v1/rpc/{name}"
            assert supabase.request("POST", path, apikey=supabase.anon_key, body=body)[0] in (401, 403, 404)
            assert supabase.rest(owner, "POST", path, body=body)[0] in (401, 403, 404)

        for name, body in (
            ("saved_page", {"p_before_saved_at": None, "p_before_story_id": None, "p_limit": 20}),
            ("set_story_state", _state_body(key="anon-state")),
            ("set_story_interest", _interest_body(key="anon-interest")),
        ):
            status, _ = supabase.request(
                "POST", f"/rest/v1/rpc/{name}", apikey=supabase.anon_key, body=body
            )
            assert status in (401, 403)
    finally:
        supabase.cleanup()


def test_resource_validation_and_receipt_cap_fail_closed() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        _seed_archive(supabase)
        owner = supabase.create_user()
        status, _ = _rpc(
            supabase, owner, "set_story_state",
            _state_body(story_id="story:" + "f" * 64, key="missing-story"),
        )
        assert status == 400
        status, _ = _rpc(
            supabase, owner, "set_story_interest",
            _interest_body(topic_id="not-a-published-topic", key="missing-topic"),
        )
        assert status == 400
        status, _ = _rpc(
            supabase,
            owner,
            "feed_page",
            {
                "p_topic_id": "ai",
                "p_order_mode": "edition_rank",
                "p_after_position": None,
                "p_after_story_id": None,
                "p_before_published_at": None,
                "p_before_story_id": None,
                "p_limit": 21,
            },
        )
        assert status == 400

        status, rows = _service_request(
            supabase,
            "PATCH",
            "/rest/v1/feed_policy?singleton=eq.true",
            body={"receipt_max_per_user": 2},
        )
        assert status == 200 and rows[0]["receipt_max_per_user"] == 2
        assert _rpc(
            supabase, owner, "set_story_state", _state_body(key="cap-state")
        )[0] == 200
        assert _rpc(
            supabase, owner, "set_story_interest", _interest_body(key="cap-interest")
        )[0] == 200
        status, _ = _rpc(
            supabase, owner, "set_story_state",
            _state_body(story_id=STORIES[1][0], key="over-cap"),
        )
        assert status == 400
        assert _rpc(
            supabase, owner, "set_story_state", _state_body(key="cap-state")
        )[0] == 200
    finally:
        _service_request(
            supabase,
            "PATCH",
            "/rest/v1/feed_policy?singleton=eq.true",
            body={"receipt_max_per_user": 1000},
        )
        supabase.cleanup()
