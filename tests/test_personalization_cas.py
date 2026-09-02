from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests._personalization_local_harness import LocalSupabase, preference_payload


pytestmark = pytest.mark.allow_socket


def test_local_supabase_compare_and_swap_allows_exactly_one_concurrent_writer() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        owner = supabase.create_user()
        status, _ = supabase.rest(
            owner,
            "POST",
            "/rest/v1/user_preferences",
            body=preference_payload(owner.user_id),
            prefer="return=representation",
        )
        assert status == 201

        def update(locale: str) -> tuple[int, object]:
            return supabase.rest(
                owner,
                "POST",
                "/rest/v1/rpc/compare_and_swap_user_preferences",
                body={
                    "expected_revision": 0,
                    "new_locale": locale,
                    "new_interests": ["agents"],
                    "new_saved_searches": [
                        {"id": "daily", "query": "agent news", "enabled": True}
                    ],
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ("en", "zh")))
        assert [status for status, _ in outcomes] == [200, 200]
        result_statuses = sorted(payload["status"] for _, payload in outcomes)
        assert result_statuses == ["conflict", "updated"]

        status, rows = supabase.rest(
            owner,
            "GET",
            "/rest/v1/user_preferences?select=user_id,revision,locale,interests,saved_searches,created_at,updated_at",
        )
        assert status == 200
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["revision"] == 1

        status, stale = supabase.rest(
            owner,
            "POST",
            "/rest/v1/rpc/compare_and_swap_user_preferences",
            body={
                "expected_revision": 0,
                "new_locale": "en",
                "new_interests": ["agents"],
                "new_saved_searches": [],
            },
        )
        assert status == 200
        assert stale == {"status": "conflict", "revision": 1}
    finally:
        supabase.cleanup()
