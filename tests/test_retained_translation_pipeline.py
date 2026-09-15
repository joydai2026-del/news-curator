import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("retained_translation_pipeline", ROOT / "scripts/retained_translation_pipeline.py")
module = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(module)


def test_queue_limit_is_bounded_before_any_network_command():
    with pytest.raises(SystemExit):
        module.main(["translate", "--limit", "1001"])


def test_export_rejects_invalid_locale_before_any_network_command(tmp_path):
    with pytest.raises(ValueError, match="locale"):
        module.main(["export", "--locale", "fr", "--output", str(tmp_path / "x.json")])


def test_localized_projection_row_requires_the_original_retained_identity_fields():
    with pytest.raises(ValueError, match="queue row"):
        module._item({"story_id": "story:x"})


def test_disabled_translation_returns_before_credentials_or_network(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "load_config", lambda _root: type("Cfg", (), {"translation": {"enabled": False, "provider": "openai"}})())
    assert module.translate(tmp_path, 1) == 0


def test_sb_secret_key_uses_apikey_without_bearer_header():
    class Transport:
        def request(self, *_args, **kwargs):
            self.credentials = kwargs["credentials"]
            return type("Response", (), {"status_code": 200, "body": b"[]"})()
    transport = Transport()
    assert module._rpc(transport, "https://example.supabase.co", "sb_secret_test", "m2_translation_queue", {}) == []
    assert [credential.header_name for credential in transport.credentials] == ["apikey"]


def test_bearer_key_keeps_both_scoped_headers():
    class Transport:
        def request(self, *_args, **kwargs):
            self.credentials = kwargs["credentials"]
            return type("Response", (), {"status_code": 200, "body": b"[]"})()
    transport = Transport()
    module._rpc(transport, "https://example.supabase.co", "eyJ.test.key", "m2_translation_queue", {})
    assert [credential.header_name for credential in transport.credentials] == ["Authorization", "apikey"]

@pytest.mark.parametrize("policy", [
    {"queue_limit": 0, "translation_workers": 1, "queue_time_budget_seconds": 240},
    {"queue_limit": 12, "translation_workers": 5, "queue_time_budget_seconds": 240},
    {"queue_limit": 12, "translation_workers": 1, "queue_time_budget_seconds": 241},
])
def test_queue_policy_rejects_unbounded_work(policy):
    with pytest.raises(ValueError):
        module._queue_policy(policy)


def test_captured_public_retained_row_preserves_its_canonical_identity():
    rows = json.loads((ROOT / "tests/fixtures/m2-retained-public.json").read_text(encoding="utf-8"))["rows"]
    row = next(row for row in rows if row["category_ids"] and row["summary"])
    item, categories, story_id = module._item(row)
    assert story_id == row["story_id"]
    assert categories == tuple(row["category_ids"])
    assert item.title == row["title"] and item.description == row["summary"]
