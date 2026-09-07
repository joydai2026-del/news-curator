"""Server-side read of one owner's saved interests for an unattended build."""

from __future__ import annotations

import base64
import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from uuid import UUID

from .auth import AuthError
from .preferences import JsonRestTransport, PreferenceInput, RestTransport
from .ranking import InterestProfile


class MaterializationError(RuntimeError):
    """A deliberately low-information preference materialization failure."""


def _legacy_service_role_key(value: str) -> bool:
    if len(value) > 8192:
        return False
    parts = value.split(".")
    if len(parts) != 3 or not parts[1]:
        return False
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "service_role"


def _is_secret_key(value: str) -> bool:
    if not value or len(value) > 8192:
        return False
    if value.startswith("sb_secret_"):
        return len(value) > len("sb_secret_")
    return _legacy_service_role_key(value)


@dataclass(frozen=True)
class SecretPreferenceConfig:
    supabase_url: str
    secret_key: str = field(repr=False)
    owner_user_id: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.supabase_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or "*" in parsed.hostname
        ):
            raise ValueError("Supabase URL must be one exact HTTPS origin.")
        if not _is_secret_key(self.secret_key):
            raise ValueError("A Supabase secret or legacy service-role key is required.")
        try:
            canonical_owner = str(UUID(self.owner_user_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Owner user id must be a UUID.") from exc
        object.__setattr__(self, "supabase_url", self.supabase_url.rstrip("/"))
        object.__setattr__(self, "owner_user_id", canonical_owner)


def fetch_interest_profile(
    config: SecretPreferenceConfig,
    *,
    transport: RestTransport | None = None,
) -> InterestProfile:
    """Read exactly one configured owner row and project only ranking inputs."""

    client = transport or JsonRestTransport()
    query = urllib.parse.urlencode(
        {
            "select": "revision,interests",
            "user_id": f"eq.{config.owner_user_id}",
            "limit": "2",
        }
    )
    headers = {
        "apikey": config.secret_key,
        "accept": "application/json",
    }
    # New Supabase secret keys are opaque API keys, not JWTs, and official
    # guidance requires the apikey header. The legacy service-role key remains
    # a JWT and keeps the Bearer header until it is retired.
    if not config.secret_key.startswith("sb_secret_"):
        headers["authorization"] = f"Bearer {config.secret_key}"
    try:
        status, payload = client.request(
            "GET",
            f"{config.supabase_url}/rest/v1/user_preferences?{query}",
            headers=headers,
        )
    except (AuthError, OSError, TimeoutError) as exc:
        raise MaterializationError("Saved interests could not be materialized.") from exc
    if status != 200 or not isinstance(payload, list) or len(payload) != 1:
        raise MaterializationError("Saved interests could not be materialized.")
    row = payload[0]
    if not isinstance(row, dict) or set(row) != {"revision", "interests"}:
        raise MaterializationError("Saved interests could not be materialized.")
    revision = row["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise MaterializationError("Saved interests could not be materialized.")
    try:
        validated = PreferenceInput.from_mapping(
            {
                "expected_revision": revision,
                "locale": "en",
                "interests": row["interests"],
                "saved_searches": [],
            }
        )
    except (KeyError, ValueError) as exc:
        raise MaterializationError("Saved interests could not be materialized.") from exc
    try:
        signal_status, signal_payload = client.request(
            "POST",
            f"{config.supabase_url}/rest/v1/rpc/materialize_user_interest_signals",
            headers={**headers, "content-type": "application/json"},
            body={"p_user_id": config.owner_user_id},
        )
    except (AuthError, OSError, TimeoutError) as exc:
        raise MaterializationError("Saved interests could not be materialized.") from exc
    if (
        signal_status != 200
        or not isinstance(signal_payload, dict)
        or set(signal_payload) != {
            "revision",
            "topic_adjustments",
            "more_like_topic_weight",
            "topic_signal_limit",
        }
    ):
        raise MaterializationError("Saved interests could not be materialized.")
    signal_revision = signal_payload["revision"]
    topic_signal_limit = signal_payload["topic_signal_limit"]
    signal_rows = signal_payload["topic_adjustments"]
    if (
        isinstance(signal_revision, bool)
        or not isinstance(signal_revision, int)
        or signal_revision < 0
        or isinstance(topic_signal_limit, bool)
        or not isinstance(topic_signal_limit, int)
        or not 1 <= topic_signal_limit <= 100
        or not isinstance(signal_rows, list)
        or len(signal_rows) > topic_signal_limit
    ):
        raise MaterializationError("Saved interests could not be materialized.")
    topic_adjustments: list[tuple[str, float]] = []
    for signal_row in signal_rows:
        if not isinstance(signal_row, dict) or set(signal_row) != {"topic_id", "adjustment"}:
            raise MaterializationError("Saved interests could not be materialized.")
        topic_id = signal_row["topic_id"]
        adjustment = signal_row["adjustment"]
        if (
            not isinstance(topic_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", topic_id)
            or isinstance(adjustment, bool)
            or not isinstance(adjustment, (int, float))
            or not math.isfinite(float(adjustment))
            or adjustment == 0
        ):
            raise MaterializationError("Saved interests could not be materialized.")
        topic_adjustments.append((topic_id, float(adjustment)))
    weight = signal_payload["more_like_topic_weight"]
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise MaterializationError("Saved interests could not be materialized.")
    weight = float(weight)
    if not math.isfinite(weight) or not 0 <= weight <= 10:
        raise MaterializationError("Saved interests could not be materialized.")
    return InterestProfile(
        revision=max(revision, signal_revision),
        interests=validated.interests,
        topic_adjustments=tuple(topic_adjustments),
        more_like_topic_weight=weight,
    )
