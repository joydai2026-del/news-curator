"""Canonical original snapshot and publication-consumer contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curator.config import ConfigError, load_config
from curator.models import Item, SourceHealth, TierResult
from curator.pipeline import main
from curator.source_snapshot import (
    SourceSnapshotError,
    load_source_snapshot,
    snapshot_config_digest,
    write_source_snapshot,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "topics.yaml").write_text(
        "categories:\n"
        "  - id: ai\n"
        "    name: AI\n"
        "    keywords: [AI]\n"
        "    keywords_by_language: {zh: [人工智能]}\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        "settings: {max_age_hours: 48}\n"
        "sources: []\n"
        "hackernews: {enabled: false}\n"
        "images: {enabled: false}\n",
        encoding="utf-8",
    )
    return tmp_path


def _item(*, language: str = "en") -> Item:
    return Item(
        title="AI original" if language == "en" else "人工智能原生报道",
        description="Publisher summary",
        url=f"https://example.com/{language}",
        canonical_url=f"https://example.com/{language}",
        source_id=f"publisher-{language}",
        source_name=f"Publisher {language}",
        published_at=NOW,
        language=language,
        native_rank=0,
    )


def _results() -> list[TierResult]:
    item = _item()
    return [
        TierResult(
            tier="sources",
            items=[item],
            ok=True,
            source_health=[
                SourceHealth(
                    source_id=item.source_id,
                    status="fresh",
                    usable_items=1,
                    newest_at=NOW,
                    age_hours=0.0,
                    max_age_hours=48.0,
                    language="en",
                    source_type="rss",
                )
            ],
        )
    ]


def test_snapshot_accepts_a_normal_source_volume_spike(tmp_path: Path) -> None:
    result = TierResult(tier="sources", items=[_item()] * 5_000, ok=True)
    path = tmp_path / "source-snapshot.json"

    write_source_snapshot(
        [result],
        path,
        generated_at=NOW,
        configuration_digest="a" * 64,
    )

    loaded = load_source_snapshot(path, current_time=NOW)
    assert len(loaded.results[0].items) == 5_000


def test_snapshot_round_trip_preserves_originals_health_and_digest(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        _results(),
        path,
        generated_at=NOW,
        configuration_digest=snapshot_config_digest(cfg),
    )

    snapshot = load_source_snapshot(
        path, expected_configuration_digest=snapshot_config_digest(cfg)
    )

    assert snapshot.generated_at == NOW
    assert snapshot.results[0].items[0].title == "AI original"
    assert snapshot.results[0].source_health[0].status == "fresh"
    assert len(snapshot.content_digest) == 64


def test_snapshot_freshness_policy_has_safe_validated_default(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    assert cfg.source_snapshot_max_age_seconds == 7_200

    for invalid in (True, 59, 86_401, 1.5):
        cfg.settings["source_snapshot_max_age_seconds"] = invalid
        with pytest.raises(ConfigError, match="source_snapshot_max_age_seconds"):
            _ = cfg.source_snapshot_max_age_seconds


def test_same_config_snapshot_replay_is_rejected_when_stale(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    digest = snapshot_config_digest(cfg)
    path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        _results(),
        path,
        generated_at=NOW - timedelta(seconds=7_201),
        configuration_digest=digest,
    )

    with pytest.raises(SourceSnapshotError, match="snapshot_stale"):
        load_source_snapshot(
            path,
            expected_configuration_digest=digest,
            current_time=NOW,
            max_age_seconds=7_200,
        )


def test_snapshot_at_freshness_boundary_is_accepted(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    digest = snapshot_config_digest(cfg)
    path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        _results(),
        path,
        generated_at=NOW - timedelta(seconds=7_200),
        configuration_digest=digest,
    )

    snapshot = load_source_snapshot(
        path,
        expected_configuration_digest=digest,
        current_time=NOW,
        max_age_seconds=7_200,
    )
    assert snapshot.generated_at == NOW - timedelta(seconds=7_200)


def test_future_snapshot_is_rejected(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    digest = snapshot_config_digest(cfg)
    path = tmp_path / "source-snapshot.json"
    write_source_snapshot(
        _results(),
        path,
        generated_at=NOW + timedelta(seconds=1),
        configuration_digest=digest,
    )

    with pytest.raises(SourceSnapshotError, match="snapshot_future"):
        load_source_snapshot(
            path,
            expected_configuration_digest=digest,
            current_time=NOW,
            max_age_seconds=7_200,
        )


def test_pipeline_filters_snapshot_rows_against_current_build_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    cfg = load_config(root)
    item = _item()
    item.published_at = NOW - timedelta(hours=48, minutes=30)
    snapshot_generated_at = NOW - timedelta(hours=1)
    path = root / "source-snapshot.json"
    write_source_snapshot(
        [TierResult("sources", [item])],
        path,
        generated_at=snapshot_generated_at,
        configuration_digest=snapshot_config_digest(cfg),
    )
    monkeypatch.setattr(
        "curator.pipeline.collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refetch")),
    )
    out = root / "site"

    assert main(
        [
            "--root", str(root),
            "--out", str(out),
            "--source-snapshot", str(path),
            "--offline",
            "--allow-empty",
        ]
    ) == 0

    projection = json.loads((out / "data/news-en.json").read_text(encoding="utf-8"))
    assert projection["generated_at"] != snapshot_generated_at.isoformat().replace("+00:00", "Z")
    assert projection["categories"][0]["items"] == []


@pytest.mark.parametrize(
    ("setting", "changed"),
    (
        ("user_agent", "news-curator-test/2 (+https://example.com)"),
        ("request_timeout", 7),
        ("max_response_bytes", 4_096),
        ("per_host_concurrency", 2),
        ("fetch_workers", 3),
        ("default_source_max_age_hours", 24),
    ),
)
def test_snapshot_digest_binds_every_global_source_request_setting(
    tmp_path: Path, setting: str, changed: object
) -> None:
    cfg = load_config(_root(tmp_path))
    original = snapshot_config_digest(cfg)

    cfg.settings[setting] = changed

    assert snapshot_config_digest(cfg) != original


def test_snapshot_digest_rejects_invalid_user_agent(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    cfg.settings["user_agent"] = "bad\r\nInjected: yes"

    with pytest.raises(SourceSnapshotError, match="snapshot_configuration"):
        snapshot_config_digest(cfg)


def test_writer_accepts_exact_item_text_bounds_and_round_trips(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    item = _item()
    item.title = "t" * 2_000
    item.description = "d" * 8_000
    path = tmp_path / "source-snapshot.json"

    write_source_snapshot(
        [TierResult("sources", [item])],
        path,
        generated_at=NOW,
        configuration_digest=snapshot_config_digest(cfg),
    )

    loaded = load_source_snapshot(path)
    assert loaded.results[0].items[0].title == item.title
    assert loaded.results[0].items[0].description == item.description


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("title", "t" * 2_001, "snapshot_item_title"),
        ("description", "d" * 8_001, "snapshot_item_description"),
        ("url", "javascript:alert(1)", "snapshot_item_url"),
        ("canonical_url", "https://example.com/" + "x" * 8_200, "snapshot_item_url"),
        ("source_id", "s" * 161, "snapshot_item_source"),
        ("source_name", "s" * 201, "snapshot_item_source"),
        ("platform", "p" * 81, "snapshot_item_platform"),
        ("language", "fr", "snapshot_language"),
        ("source_weight", float("nan"), "snapshot_item_weight"),
        ("score", 1.5, "snapshot_item_score"),
        ("native_rank", True, "snapshot_item_rank"),
        ("echo_platforms", {f"p{i}" for i in range(33)}, "snapshot_item_echo"),
        ("native_categories", {f"c{i}" for i in range(33)}, "snapshot_item_category"),
        ("matched_keywords", [f"k{i}" for i in range(65)], "snapshot_item_keyword"),
        (
            "cluster",
            [{"source_name": "Publisher", "url": f"https://example.com/{i}"} for i in range(21)],
            "snapshot_item_cluster",
        ),
    ),
)
def test_writer_rejects_item_values_outside_loader_schema_before_write(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    cfg = load_config(_root(tmp_path))
    item = _item()
    setattr(item, field, value)
    path = tmp_path / "source-snapshot.json"

    with pytest.raises(SourceSnapshotError, match=reason):
        write_source_snapshot(
            [TierResult("sources", [item])],
            path,
            generated_at=NOW,
            configuration_digest=snapshot_config_digest(cfg),
        )

    assert not path.exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("source_id", "s" * 161, "snapshot_health_source"),
        ("status", "s" * 81, "snapshot_health_status"),
        ("usable_items", -1, "snapshot_health_items"),
        ("age_hours", -0.1, "snapshot_health_age"),
        ("max_age_hours", float("inf"), "snapshot_health_max_age"),
        ("language", "fr", "snapshot_language"),
        ("source_type", "s" * 49, "snapshot_health_type"),
        ("echo_eligible", "yes", "snapshot_health_schema"),
        ("reason_code", "r" * 121, "snapshot_health_reason"),
    ),
)
def test_writer_rejects_health_values_outside_loader_schema_before_write(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    cfg = load_config(_root(tmp_path))
    results = _results()
    results[0].source_health[0] = replace(
        results[0].source_health[0], **{field: value}
    )
    path = tmp_path / "source-snapshot.json"

    with pytest.raises(SourceSnapshotError, match=reason):
        write_source_snapshot(
            results,
            path,
            generated_at=NOW,
            configuration_digest=snapshot_config_digest(cfg),
        )

    assert not path.exists()


def test_snapshot_rejects_tamper_config_drift_and_newsletter_rows(tmp_path: Path) -> None:
    cfg = load_config(_root(tmp_path))
    path = tmp_path / "source-snapshot.json"
    digest = snapshot_config_digest(cfg)
    write_source_snapshot(_results(), path, generated_at=NOW, configuration_digest=digest)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][0]["items"][0]["title"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="snapshot_content_mismatch"):
        load_source_snapshot(path, expected_configuration_digest=digest)
    with pytest.raises(SourceSnapshotError, match="snapshot_configuration_mismatch"):
        write_source_snapshot(_results(), path, generated_at=NOW, configuration_digest=digest)
        load_source_snapshot(path, expected_configuration_digest="0" * 64)

    newsletter = _item()
    newsletter.is_newsletter = True
    with pytest.raises(SourceSnapshotError, match="snapshot_newsletter_forbidden"):
        write_source_snapshot(
            [TierResult("sources", [newsletter])],
            path,
            generated_at=NOW,
            configuration_digest=digest,
        )


def test_pipeline_consumes_snapshot_once_and_keeps_newsletter_original_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    cfg = load_config(root)
    snapshot_path = root / "source-snapshot.json"
    write_source_snapshot(
        _results(),
        snapshot_path,
        generated_at=NOW,
        configuration_digest=snapshot_config_digest(cfg),
    )
    newsletter_path = root / "newsletter.json"
    newsletter_path.write_text(
        json.dumps(
            {
                "dark": False,
                "ok": True,
                "watermark": None,
                "hashes": [],
                "status": {},
                "items": [
                    {
                        "title": "Newsletter original",
                        "description": "Private-mail summary",
                        "url": "https://example.com/newsletter",
                        "canonical_url": "https://example.com/newsletter",
                        "source_id": "newsletter:tldr",
                        "source_name": "TLDR",
                        "published_at": NOW.isoformat(),
                        "newsletter_sender": "TLDR",
                        "image_url": "https://example.com/forbidden.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "curator.pipeline.collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("second fetch")),
    )
    out = root / "site-out"
    assert main(
        [
            "--root", str(root),
            "--out", str(out),
            "--source-snapshot", str(snapshot_path),
            "--newsletter-artifact", str(newsletter_path),
        ]
    ) == 0

    en = json.loads((out / "data/news-en.json").read_text(encoding="utf-8"))
    zh = json.loads((out / "data/news-zh.json").read_text(encoding="utf-8"))
    newsletter_categories = [row for row in en["categories"] if row["id"] == "newsletters"]
    assert len(newsletter_categories) == 1
    assert newsletter_categories[0]["items"][0]["title"] == "Newsletter original"
    assert newsletter_categories[0]["items"][0]["image_url"] == ""
    assert newsletter_categories[0]["items"][0]["translated"] is False
    assert newsletter_categories[0]["items"][0]["translation_available"] is False
    assert all(row["id"] != "newsletters" for row in zh["categories"])


def test_invalid_explicit_snapshot_never_falls_back_to_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    broken = root / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "curator.pipeline.collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("second fetch")),
    )

    assert main(["--root", str(root), "--source-snapshot", str(broken)]) == 2
