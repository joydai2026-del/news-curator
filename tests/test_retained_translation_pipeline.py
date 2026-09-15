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
    {"queue_limit": 0, "translation_workers": 1, "queue_dispatch_budget_seconds": 240},
    {"queue_limit": 12, "translation_workers": 5, "queue_dispatch_budget_seconds": 240},
    {"queue_limit": 12, "translation_workers": 1, "queue_dispatch_budget_seconds": 241},
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
    assert module._queue_policy({"queue_limit":60,"translation_workers":4,"queue_dispatch_budget_seconds":240}) == (60,4,240)


def test_deadline_stops_queued_provider_dispatch_and_preserves_one_run_id(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from threading import Lock
    rows=json.loads((ROOT/"tests/fixtures/m2-retained-public.json").read_text())["rows"]
    rows=[r for r in rows if r["category_ids"]][:2]
    cfg=SimpleNamespace(translation={"enabled":True,"provider":"openai","queue_limit":2,"translation_workers":1,"queue_dispatch_budget_seconds":1,"supabase_url_env":"URL","supabase_service_role_key_env":"KEY","openai_api_key_env":"API"},categories=[SimpleNamespace(id=k,name=k) for k in set(k for r in rows for k in r["category_ids"])])
    monkeypatch.setattr(module,"load_config",lambda _:cfg)
    monkeypatch.setattr(module,"_rpc",lambda *_args:rows)
    monkeypatch.setattr(module,"_provider_store",lambda *_args:(object(),object()))
    monkeypatch.setenv("URL","https://example.supabase.co"); monkeypatch.setenv("KEY","sb_secret_test");monkeypatch.setenv("API","not-a-key")
    clock=[0]; calls=[]
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    def produce(**kwargs):
        calls.append(kwargs); clock[0]=2
        return SimpleNamespace(counters={"translated":1},fatal_persistence_failure=False)
    monkeypatch.setattr(module,"produce_translation_records",produce)
    assert module.translate(tmp_path,2)==0
    assert len(calls)==1
    language=next(iter(calls[0]["ranked_by_language"]))
    assert calls[0]["cfg"].translation["targets"]==["zh" if language=="en" else "en"]

def test_background_workers_share_whole_job_budget_identity(monkeypatch,tmp_path):
    from types import SimpleNamespace
    rows=json.loads((ROOT/"tests/fixtures/m2-retained-public.json").read_text())["rows"]
    rows=[r for r in rows if r["category_ids"]][:3]
    cfg=SimpleNamespace(translation={"enabled":True,"provider":"openai","queue_limit":3,"translation_workers":2,"queue_dispatch_budget_seconds":10,"supabase_url_env":"URL","supabase_service_role_key_env":"KEY","openai_api_key_env":"API"},categories=[SimpleNamespace(id=k,name=k) for k in set(k for r in rows for k in r["category_ids"])])
    monkeypatch.setattr(module,"load_config",lambda _:cfg); monkeypatch.setattr(module,"_rpc",lambda *_args:rows)
    stores=[]; calls=[]
    def factory(*args):
        store=object();stores.append(store);return object(),store
    monkeypatch.setattr(module,"_provider_store",factory)
    monkeypatch.setenv("URL","https://example.supabase.co");monkeypatch.setenv("KEY","sb_secret_test");monkeypatch.setenv("API","not-a-key")
    def produce(**kwargs):
        calls.append(kwargs);return SimpleNamespace(counters={"translated":1},fatal_persistence_failure=False)
    monkeypatch.setattr(module,"produce_translation_records",produce)
    assert module.translate(tmp_path,3)==0
    assert len(calls)==3 and len({call["run_id"] for call in calls})==1
    assert len(set(stores))==3


def test_multicategory_queue_deduplicates_after_each_category_gets_a_turn():
    rows=json.loads((ROOT/"tests/fixtures/m2-retained-public.json").read_text())["rows"]
    rows=[dict(r) for r in rows[:4]]
    rows[0]["category_ids"]=["ai","world"]
    rows[1]["category_ids"]=["ai"]
    rows[2]["category_ids"]=["world"]
    rows[3]["category_ids"]=[]
    tasks=module._fair_tasks(rows,{"ai":"AI","world":"World"})
    assert len(tasks)==len({module.story_id_for_item(t[2]) for t in tasks})==4
    assert {t[1] for t in tasks} >= {"AI","World","All"}


def test_public_export_preserves_existing_sanitized_newsletters_without_provider(monkeypatch,tmp_path):
    from types import SimpleNamespace
    cfg=SimpleNamespace(translation={"supabase_url_env":"URL","supabase_service_role_key_env":"KEY"},categories=[])
    monkeypatch.setattr(module,"load_config",lambda _:cfg)
    monkeypatch.setenv("URL","https://example.supabase.co");monkeypatch.setenv("KEY","sb_secret_test")
    # Empty native lane is a real supported M1 state; no invented newsletter prose.
    newsletter={"id":"newsletters","name":"Newsletters","items":[]}
    output=tmp_path/"news-en.json"
    output.write_text(json.dumps({"schema_version":1,"language":"en","categories":[newsletter]}))
    monkeypatch.setattr(module,"_rpc",lambda *args:pytest.fail("no category/provider call expected"))
    assert module.export(tmp_path,output,"en")==0
    assert json.loads(output.read_text())["categories"] == [newsletter]

def test_newsletter_projection_rejects_cross_language_or_nonprivate_injection(tmp_path):
    path=tmp_path/"news-zh.json"
    path.write_text(json.dumps({"schema_version":1,"language":"en","categories":[]}))
    with pytest.raises(ValueError): module._preserved_newsletters(path,"zh")
