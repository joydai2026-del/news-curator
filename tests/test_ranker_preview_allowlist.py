"""F5: the preview-owner allowlist must fail CLOSED, not open.

Red before the fix: `build_application` parsed an unset variable to `[]` and
booted, `ServicePolicy.preview_owner_ids` stayed empty, and the gate at
`service.py` (`if self._policy.preview_owner_ids and ...`) never ran, so every
signed-in user reached the paid provider path.
"""
from __future__ import annotations

import pytest

from curator.recommendation.runtime import build_application, preview_owner_allowlist

VARIABLE = "NEWS_CURATOR_PREVIEW_OWNER_IDS"
OWNER = "00000000-0000-0000-0000-000000000001"


def base_env(**overrides):
    env = {
        "NEWS_CURATOR_READER_ORIGIN": "https://reader.example",
        "NEWS_CURATOR_SUPABASE_URL": "https://project-ref.supabase.co",
        "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
        "NEWS_CURATOR_SUPABASE_SERVICE_ROLE_KEY": "service-role-test",
        "NEWS_CURATOR_CURSOR_SIGNING_KEY": "k" * 32,
        "NEWS_CURATOR_TENANT_ID": "tenant-test",
        "NEWS_CURATOR_MODEL_API_KEY": "provider-key-test",
    }
    env.update(overrides)
    return env


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n", "[]", "[ ]"])
def test_empty_allowlist_refuses_to_boot_an_enabled_service(raw):
    env = {} if raw is None else {VARIABLE: raw}
    with pytest.raises(ValueError, match="non-empty preview owner allowlist"):
        preview_owner_allowlist(env, enabled=True)


@pytest.mark.parametrize("raw", [None, "", "   ", "[]"])
def test_empty_allowlist_is_allowed_while_the_service_is_disabled(raw):
    env = {} if raw is None else {VARIABLE: raw}
    assert preview_owner_allowlist(env, enabled=False) == ()


def test_populated_allowlist_parses_to_its_ids():
    env = {VARIABLE: f'["{OWNER}","00000000-0000-0000-0000-000000000002"]'}
    assert preview_owner_allowlist(env, enabled=True) == (
        OWNER, "00000000-0000-0000-0000-000000000002")


@pytest.mark.parametrize("raw", ['[""]', '["   "]', '[null]', '[1]', '{}', '"owner"'])
def test_malformed_allowlist_is_refused(raw):
    with pytest.raises(ValueError, match="invalid preview owner allowlist"):
        preview_owner_allowlist({VARIABLE: raw}, enabled=True)


def test_build_application_refuses_an_enabled_service_with_no_allowlist():
    with pytest.raises(ValueError, match="non-empty preview owner allowlist"):
        build_application(environ=base_env(), policy_path="config/ranker-policy-r1.yaml")


def test_build_application_accepts_the_allowlist_and_moves_on(monkeypatch):
    # Proves the populated case is not refused HERE: the boot reaches the next
    # validated dependency (the reviewed tokenizer cache) instead of the gate.
    env = base_env(**{VARIABLE: f'["{OWNER}"]'})
    with pytest.raises(ValueError) as error:
        build_application(environ=env, policy_path="config/ranker-policy-r1.yaml")
    assert "allowlist" not in str(error.value)


# --- Fix round 1. Codex review of PR #47, must-fix 1. ---------------------
# The runtime check above closes the NORMAL boot path only. `ServicePolicy`
# defaults `preview_owner_ids` to `()` and the gate in `_authenticate` used to
# read `if self._policy.preview_owner_ids and user_id not in ...`, so any
# enabled RankingService built any other way (a fixture, a script, a future
# second composition root) still admitted every authenticated owner. Empty now
# means NOBODY in the service itself, which is where the decision belongs.

from curator.recommendation.service import (  # noqa: E402
    AuthenticationError, RankingService, ServicePolicy)


class _Auth:
    def get_user(self, token):
        assert token == "valid"
        return {"id": OWNER, "app_metadata": {}}


class _Store:
    def owner_states(self, access_token, story_ids):
        return {}

    def history_snapshot(self, token):
        raise AssertionError("an allowlist denial must happen before any store read")

    def load_frozen_order(self, *, user_id, frozen_order_id):
        raise AssertionError("an allowlist denial must happen before any store read")


def _service(preview_owner_ids, *, enabled=True):
    return RankingService(auth=_Auth(), store=_Store(), adapter=object(),
        policy=ServicePolicy("policy", "model", "provider", "tenant", enabled=enabled,
                             preview_owner_ids=preview_owner_ids),
        cursor_key=b"x" * 32, clock=lambda: 1000)


def _service_with_a_forced_empty_allowlist():
    """The object the constructor now refuses, built anyway.

    Three layers guard this, and each one has to be tested where it acts. The
    constructor refusal below is the outer layer; this forces past it (frozen
    dataclass, so `object.__setattr__`) to prove the INNER layer, the gate in
    `_authenticate`, denies on its own. If only the constructor guarded it, any
    future code path that assembles a policy differently would be open again.
    """
    subject = _service((), enabled=False)
    object.__setattr__(subject._policy, "enabled", True)
    return subject


def test_an_enabled_policy_with_an_empty_allowlist_cannot_be_constructed():
    with pytest.raises(ValueError, match="non-empty preview owner allowlist"):
        ServicePolicy("policy", "model", "provider", "tenant", enabled=True)


@pytest.mark.parametrize("entries", [("",), ("   ",), (None,), (1,)])
def test_a_malformed_allowlist_entry_is_refused_by_the_policy(entries):
    with pytest.raises(ValueError, match="preview_owner_ids entries"):
        ServicePolicy("policy", "model", "provider", "tenant", enabled=False,
                      preview_owner_ids=entries)


def test_a_service_carrying_an_empty_allowlist_admits_nobody():
    subject = _service_with_a_forced_empty_allowlist()
    with pytest.raises(AuthenticationError, match="owner is not enabled for preview"):
        subject.rank(authorization="Bearer valid", body={})


def test_the_empty_allowlist_also_closes_the_pagination_path():
    subject = _service_with_a_forced_empty_allowlist()
    cursor = subject._cursor("frozen", 0, 1100)
    with pytest.raises(AuthenticationError, match="owner is not enabled for preview"):
        subject.page(authorization="Bearer valid", cursor=cursor)


def test_a_listed_owner_still_passes_the_gate():
    subject = _service((OWNER,))
    token, owner = subject._authenticate("Bearer valid")
    assert token == "valid" and owner.user_id == OWNER


def test_an_unlisted_owner_is_refused():
    subject = _service(("00000000-0000-0000-0000-000000000099",))
    with pytest.raises(AuthenticationError, match="owner is not enabled for preview"):
        subject._authenticate("Bearer valid")
