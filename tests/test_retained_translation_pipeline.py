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


def test_export_writes_selected_language_public_projection_from_captured_shape(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from curator.models import Item
    item = Item(title="English title", description="English summary", url="https://example.com/a", canonical_url="https://example.com/a", source_id="public", source_name="Public", language="en", published_at=datetime(2026, 9, 15, tzinfo=timezone.utc), native_categories={"ai"})
    row = {"story_id": module.story_id_for_item(item), "title": item.title, "summary": item.description, "language": "en", "source_id": "public", "source_name": "Public", "canonical_url": item.canonical_url, "published_at": item.published_at.isoformat(), "category_ids": ["ai"], "display_title": "中文标题", "display_summary": "中文摘要", "display_language": "zh", "translation_available": True}
    cfg = type("Cfg", (), {"translation": {"supabase_url_env": "URL", "supabase_service_role_key_env": "KEY"}, "categories": [type("Category", (), {"id": "ai", "name": "AI"})()]})()
    monkeypatch.setattr(module, "load_config", lambda _root: cfg)
    monkeypatch.setattr(module, "_transport", lambda _policy: object())
    monkeypatch.setattr(module, "_rpc", lambda *_args: [row])
    monkeypatch.setenv("URL", "https://example.supabase.co"); monkeypatch.setenv("KEY", "sb_secret_test")
    out = tmp_path / "news-zh.json"
    assert module.export(tmp_path, out, "zh") == 0
    projection = json.loads(out.read_text(encoding="utf-8"))
    rendered = projection["categories"][0]["items"][0]
    assert rendered["story_id"] == row["story_id"] and rendered["title"] == "中文标题"
    assert rendered["display_language"] == "zh" and rendered["is_newsletter"] is False


def test_fair_tasks_alternate_language_before_second_item_from_same_language(monkeypatch):
    from datetime import datetime, timezone
    def row(story_id, language, category):
        item = __import__("curator.models", fromlist=["Item"]).Item(title=story_id, description="summary", url=f"https://example.com/{story_id}", canonical_url=f"https://example.com/{story_id}", source_id="public", source_name="Public", language=language, published_at=datetime(2026, 9, 15, tzinfo=timezone.utc), native_categories={category})
        return {"story_id": module.story_id_for_item(item), "title": item.title, "summary": item.description, "language": language, "source_id":"public", "source_name":"Public", "canonical_url":item.canonical_url, "published_at":item.published_at.isoformat(), "category_ids":[category]}
    tasks = module._fair_tasks([row("en-one","en","ai"), row("en-two","en","ai"), row("zh-one","zh","ai")], {"ai":"AI"})
    assert [task[0] for task in tasks] == ["en", "zh", "en"]


def test_queue_policy_accepts_bounded_four_worker_configuration():
    assert module._queue_policy({"queue_limit":60,"translation_workers":4,"queue_time_budget_seconds":240}) == (60,4,240)
