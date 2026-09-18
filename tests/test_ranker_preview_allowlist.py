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
