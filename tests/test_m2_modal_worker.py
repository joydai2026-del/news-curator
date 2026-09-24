"""The scheduled worker is opt-in, bounded and produces aggregate receipts only."""

import importlib
import os
import sys
import types

import pytest

from tests.test_m2_modal_policy import _load, _required


def test_worker_is_absent_from_normal_service_deploy(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_CURATOR_MODAL_PREPARATION_WORKER_ENABLED", raising=False)
    module, captured = _load(monkeypatch, **_required(tmp_path))
    assert len(captured["functions"]) == 1
    assert not hasattr(module, "prepare_next_run")


def test_worker_has_bounded_config_and_same_secret(monkeypatch, tmp_path):
    values = _required(tmp_path) | {
        "NEWS_CURATOR_MODAL_PREPARATION_WORKER_ENABLED": "true",
        "NEWS_CURATOR_MODAL_PREPARATION_CRON": "*/5 * * * *",
        "NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE": "3",
        "NEWS_CURATOR_MODAL_PREPARATION_TIMEOUT_SECONDS": "480",
    }
    module, captured = _load(monkeypatch, **values)
    assert callable(module.prepare_next_run)
    assert len(captured["functions"]) == 2
    worker = captured["functions"][1]
    assert worker["schedule"] == ("cron", "*/5 * * * *")
    assert worker["timeout"] == 480
    assert worker["max_containers"] == 1
    assert worker["min_containers"] == 0
    assert worker["restrict_modal_access"] is True
    assert worker["secrets"] == captured["functions"][0]["secrets"]
    assert worker["image"][2] == {"NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE": "3"}
    assert captured["concurrent"]["max_inputs"] == 1


@pytest.mark.parametrize("key,value", [
    ("NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE", "0"),
    ("NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE", "6"),
    ("NEWS_CURATOR_MODAL_PREPARATION_TIMEOUT_SECONDS", "89"),
    ("NEWS_CURATOR_MODAL_PREPARATION_TIMEOUT_SECONDS", "1801"),
    ("NEWS_CURATOR_MODAL_PREPARATION_CRON", ""),
])
def test_worker_rejects_invalid_operational_config(monkeypatch, tmp_path, key, value):
    with pytest.raises(ValueError):
        _load(monkeypatch, **(_required(tmp_path) | {
            "NEWS_CURATOR_MODAL_PREPARATION_WORKER_ENABLED": "true",
            key: value,
        }))


def test_handler_processes_only_configured_batch_and_logs_no_ids(monkeypatch, capsys):
    from curator.recommendation import modal_handlers, preparation_worker, runtime
    private_id = "private-owner-story-id"
    service = types.SimpleNamespace(_store=object(), _adapter=object(), _policy=object())
    monkeypatch.setattr(runtime, "build_application", lambda: types.SimpleNamespace(_service=service))
    calls = []
    def fake_process(**kwargs):
        calls.append(kwargs)
        return "ready"
    monkeypatch.setattr(preparation_worker, "process_one_preparation", fake_process)
    monkeypatch.setenv("NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE", "2")
    result = modal_handlers.prepare_next_run()
    assert len(calls) == 2
    assert result["jobs"] == 2
    assert result["outcomes"] == {"ready": 2}
    assert private_id not in capsys.readouterr().out


def test_handler_stops_on_empty_or_disabled(monkeypatch):
    from curator.recommendation import modal_handlers, preparation_worker, runtime
    service = types.SimpleNamespace(_store=object(), _adapter=object(), _policy=object())
    monkeypatch.setattr(runtime, "build_application", lambda: types.SimpleNamespace(_service=service))
    monkeypatch.setenv("NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE", "5")
    for outcome in ("empty", "disabled"):
        calls = []
        def fake_process(**kwargs):
            calls.append(kwargs)
            return outcome
        monkeypatch.setattr(preparation_worker, "process_one_preparation", fake_process)
        result = modal_handlers.prepare_next_run()
        assert len(calls) == 1
        assert result["jobs"] == 0
        assert result["outcomes"] == {outcome: 1}
