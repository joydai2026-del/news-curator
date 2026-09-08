"""Actions-facing source health stays warning-only and privacy-safe."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from curator.health import main, render_summary, write_report
from curator.models import SourceHealth, TierResult
from curator.config import RssSource
from curator.fetchers.rss import source_health


def test_forced_configured_source_failure_reaches_actions_warning_and_summary(tmp_path, capsys):
    from curator.config import load_config
    from curator.pipeline import configured_source_specs
    from curator.sources import SourceContext, build_builtin_registry, collect_sources
    from curator.sources.errors import SafeTransportError, SafeTransportReason

    registry = build_builtin_registry()
    configured = configured_source_specs(load_config(Path(__file__).resolve().parents[1]), registry)
    source = next(spec for spec in configured if spec.enabled and spec.type == "rss")

    class ForcedFailureTransport:
        def get(self, source_id, _url, **_kwargs):
            raise SafeTransportError(source_id, SafeTransportReason.DEADLINE_EXCEEDED)

    context = SourceContext(
        registry=registry,
        transport=ForcedFailureTransport(),
        clock=lambda: datetime.now(timezone.utc),
        environment=lambda _name: None,
    )
    result, = collect_sources([source], context, max_workers=1)
    assert result.health.status == "unavailable"
    assert result.health.reason_code == "deadline_exceeded"
    assert not result.items
    report = tmp_path / "source-health.json"
    summary = tmp_path / "step-summary.md"
    write_report([TierResult("sources", source_health=[result.health])], report)
    assert main(["--report", str(report), "--summary", str(summary)]) == 0
    output = capsys.readouterr().out
    assert f"::warning title=News source {source.id}::unavailable" in output
    assert "deadline exceeded" in output
    assert f"| {source.id} | unavailable |" in summary.read_text()
    assert source.url not in output + report.read_text() + summary.read_text()


def _health(status: str = "stale") -> SourceHealth:
    return SourceHealth(
        source_id="cnn-news",
        status=status,
        usable_items=12,
        newest_at=datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
        age_hours=9.5,
        max_age_hours=6,
        language="en",
        source_type="news_sitemap",
        reason_code="newest_item_too_old",
    )


def test_report_contains_structured_safe_source_health(tmp_path):
    path = tmp_path / "health.json"
    write_report([TierResult(tier="rss", source_health=[_health()])], path)
    payload = json.loads(path.read_text())
    assert payload["sources"][0]["source_id"] == "cnn-news"
    assert payload["sources"][0]["status"] == "stale"
    assert "url" not in path.read_text().casefold()
    assert "error" not in path.read_text().casefold()


def test_actions_warning_and_summary_never_block_healthy_publication(tmp_path, capsys):
    report = tmp_path / "health.json"
    summary = tmp_path / "summary.md"
    write_report([TierResult(tier="rss", source_health=[_health()])], report)
    assert main(["--report", str(report), "--summary", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "::warning" in out and "cnn-news" in out
    text = summary.read_text()
    assert "Source freshness" in text
    assert "cnn-news" in text and "9.5h" in text and "6h" in text
    assert "http" not in text.casefold()


def test_disabled_sources_are_not_counted_or_annotated_as_warnings(tmp_path, capsys):
    report = tmp_path / "health.json"
    summary = tmp_path / "summary.md"
    write_report(
        [
            TierResult(
                tier="rss",
                source_health=[_health("fresh"), _health("disabled")],
            )
        ],
        report,
    )

    payload = json.loads(report.read_text())
    rendered = render_summary(payload)
    assert "1 fresh, 0 warning, 1 checked." in rendered
    assert "disabled" not in rendered

    assert main(["--report", str(report), "--summary", str(summary)]) == 0
    assert "disabled" not in capsys.readouterr().out


def test_empty_health_always_has_a_safe_reason_code():
    source = RssSource(id="udn-zh", name="UDN World", url="https://udn.com/rssfeed/news/2/6638?ch=news")
    health = source_health(
        source,
        [],
        datetime(2026, 8, 29, 20, tzinfo=timezone.utc),
        status_hint="empty",
    )
    assert health.status == "empty"
    assert health.reason_code == "no_usable_items"
