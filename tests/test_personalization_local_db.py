from __future__ import annotations

import urllib.parse

import pytest

from tests._personalization_local_harness import LocalSupabase, preference_payload


pytestmark = pytest.mark.allow_socket


def test_local_supabase_denies_anon_and_expired_jwt() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        owner = supabase.create_user()

        status, _ = supabase.request(
            "GET",
            "/rest/v1/user_preferences?select=user_id",
            apikey=supabase.anon_key,
        )
        assert status in (401, 403)

        status, _ = supabase.request(
            "GET",
            "/rest/v1/user_preferences?select=user_id",
            apikey=supabase.anon_key,
            bearer=supabase.expired_access_token(owner.user_id),
        )
        assert status in (401, 403)
    finally:
        supabase.cleanup()


def test_local_supabase_rejects_saved_search_json_boundaries() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        owner = supabase.create_user()
        oversized_total = [
            {"id": f"search-{index}", "query": "é" * 300, "enabled": True}
            for index in range(20)
        ]
        invalid_saved_searches = {
            "unknown field": [
                {"id": "daily", "query": "agent news", "enabled": True, "extra": "rejected"}
            ],
            "duplicate ids": [
                {"id": "daily", "query": "agent news", "enabled": True},
                {"id": "daily", "query": "different query", "enabled": False},
            ],
            "oversized id": [
                {"id": "x" * 65, "query": "agent news", "enabled": True}
            ],
            "oversized query": [
                {"id": "daily", "query": "x" * 301, "enabled": True}
            ],
            "oversized serialized value": oversized_total,
        }
        for case, saved_searches in invalid_saved_searches.items():
            invalid = preference_payload(owner.user_id)
            invalid["saved_searches"] = saved_searches
            status, _ = supabase.rest(
                owner,
                "POST",
                "/rest/v1/user_preferences",
                body=invalid,
                prefer="return=representation",
            )
            assert status == 400, case
    finally:
        supabase.cleanup()


def test_local_supabase_enforces_owner_rls_and_column_grants() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        owner = supabase.create_user()
        other = supabase.create_user()

        invalid = preference_payload(owner.user_id)
        invalid["interests"] = ["x" * 81]
        status, _ = supabase.rest(
            owner,
            "POST",
            "/rest/v1/user_preferences",
            body=invalid,
            prefer="return=representation",
        )
        assert status == 400

        status, inserted = supabase.rest(
            owner,
            "POST",
            "/rest/v1/user_preferences",
            body=preference_payload(owner.user_id),
            prefer="return=representation",
        )
        assert status == 201
        assert isinstance(inserted, list) and inserted[0]["user_id"] == owner.user_id

        status, hidden = supabase.rest(
            other,
            "GET",
            "/rest/v1/user_preferences?select=user_id,revision,locale",
        )
        assert status == 200
        assert hidden == []

        status, _ = supabase.rest(
            other,
            "POST",
            "/rest/v1/user_preferences",
            body=preference_payload(owner.user_id),
            prefer="return=representation",
        )
        assert status in (401, 403)

        server_field_bypass = preference_payload(other.user_id)
        server_field_bypass["revision"] = 99
        status, _ = supabase.rest(
            other,
            "POST",
            "/rest/v1/user_preferences",
            body=server_field_bypass,
            prefer="return=representation",
        )
        assert status in (401, 403)

        status, _ = supabase.rest(
            owner,
            "PATCH",
            "/rest/v1/user_preferences",
            body={"revision": 99},
            prefer="return=representation",
        )
        assert status in (401, 403)
    finally:
        supabase.cleanup()


def test_local_supabase_owner_delete_and_cross_user_delete_isolation() -> None:
    supabase = LocalSupabase.from_environment()
    try:
        owner = supabase.create_user()
        other = supabase.create_user()
        status, _ = supabase.rest(
            owner,
            "POST",
            "/rest/v1/user_preferences",
            body=preference_payload(owner.user_id),
            prefer="return=representation",
        )
        assert status == 201
        owner_filter = urllib.parse.quote(owner.user_id, safe="")

        status, hidden = supabase.rest(
            other,
            "DELETE",
            f"/rest/v1/user_preferences?user_id=eq.{owner_filter}",
            prefer="return=representation",
        )
        assert status == 200
        assert hidden == []

        status, rows = supabase.rest(
            owner,
            "GET",
            "/rest/v1/user_preferences?select=user_id",
        )
        assert status == 200
        assert rows == [{"user_id": owner.user_id}]

        status, deleted = supabase.rest(
            owner,
            "DELETE",
            f"/rest/v1/user_preferences?user_id=eq.{owner_filter}",
            prefer="return=representation",
        )
        assert status == 200
        assert isinstance(deleted, list) and deleted[0]["user_id"] == owner.user_id

        status, rows = supabase.rest(
            owner,
            "GET",
            "/rest/v1/user_preferences?select=user_id",
        )
        assert status == 200
        assert rows == []
    finally:
        supabase.cleanup()


def test_local_supabase_exposes_only_the_least_privilege_rpc() -> None:
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

        status, _ = supabase.rest(
            owner,
            "POST",
            "/rest/v1/rpc/valid_interests",
            body={"value": ["agents"]},
        )
        assert status in (401, 403, 404)

        status, _ = supabase.request(
            "POST",
            "/rest/v1/rpc/compare_and_swap_user_preferences",
            apikey=supabase.anon_key,
            body={
                "expected_revision": 0,
                "new_locale": "zh",
                "new_interests": ["agents"],
                "new_saved_searches": [],
            },
        )
        assert status in (401, 403)

        status, result = supabase.rest(
            owner,
            "POST",
            "/rest/v1/rpc/compare_and_swap_user_preferences",
            body={
                "expected_revision": 0,
                "new_locale": "zh",
                "new_interests": ["agents"],
                "new_saved_searches": [],
            },
        )
        assert status == 200
        assert result["status"] == "updated"
        assert result["revision"] == 1
    finally:
        supabase.cleanup()
