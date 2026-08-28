"""Run the newsletter lane alone and write a JSON artifact for the build job.

Why this exists as its own entry point: the design doc scopes the Gmail secrets
to a fetch job that has NO `contents: write`, so the fetch cannot be a step
inside the build job that commits the image cache and the cursor. This CLI is
that fetch job. It reads the committed cursor, asks Gmail for the window, and
writes everything the build needs into one artifact file:

  * the sanitized story records (already through the privacy sanitizer; the
    artifact never contains a raw newsletter link, an address, or a subject),
  * the per-adapter status counts,
  * the watermark and hashes the build job passes to `state.advance()` AFTER
    the page is written.

The artifact is safe to expose as a public Actions artifact: every field in it
either already appears on the public page or is the committed state file's own
content.

Failure model matches the lane's: this process exits 0 no matter what. A dark
lane is a fact the page reports, never a reason the six healthy tabs miss an
hourly build. The one thing a human must act on (a revoked refresh token) is
surfaced as a GitHub Actions warning annotation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..config import ConfigError, load_config
from . import lane, state

log = logging.getLogger(__name__)

ARTIFACT_VERSION = 1

CONFIG_ERROR = "config_error"

# Reasons a human has to act on, surfaced as workflow warnings. `disabled` is
# a configuration choice, not a problem, so it is deliberately not here.
WARN_REASONS = {"auth_revoked", "auth_failed", "missing_credentials",
                "api_error", "network_error", "no_adapters_enabled",
                CONFIG_ERROR}


def _field(record, name: str, default=""):
    """One accessor for both shapes `lane.fetch` can emit (Item or dict)."""
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def serialize(result: lane.LaneResult) -> dict:
    items = []
    for record in result.items:
        published = _field(record, "published_at", None)
        items.append({
            "title": _field(record, "title"),
            "url": _field(record, "url"),
            "canonical_url": _field(record, "canonical_url"),
            "source_id": _field(record, "source_id"),
            "source_name": _field(record, "source_name"),
            "platform": _field(record, "platform"),
            "published_at": published.isoformat() if isinstance(published, datetime) else str(published or ""),
            "description": _field(record, "description"),
            "newsletter_sender": _field(record, "newsletter_sender"),
        })
    return {
        "version": ARTIFACT_VERSION,
        "ok": result.ok,
        "dark": result.dark,
        "reason": result.reason,
        "note": result.note,
        "unmatched_messages": result.unmatched_messages,
        # What this run did NOT see. Counts only, and they travel with the
        # artifact so the build job can print them instead of the fetch job
        # being the only place a short batch was ever visible.
        "truncated": result.truncated,
        "unreadable_messages": result.unreadable_messages,
        "unauthenticated_messages": result.unauthenticated_messages,
        "unauthenticated_missing": result.unauthenticated_missing,
        "watermark": result.watermark.isoformat() if result.watermark else None,
        "hashes": list(result.hashes),
        "status": {
            adapter_id: {
                "name": status.name,
                "seen": status.seen,
                "extracted": status.extracted,
                "dropped_links": status.dropped_links,
                "published": status.published,
                "state": status.state,
            }
            for adapter_id, status in result.status.items()
        },
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="curator.newsletter", description="Fetch the newsletter lane and write an artifact."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="where sources.yaml lives")
    parser.add_argument("--out", type=Path, required=True, help="artifact JSON path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    try:
        cfg = load_config(args.root)
    except ConfigError as exc:
        # A config error would fail the build job too, where it belongs. This
        # job still exits 0 with a dark artifact so the page can say so.
        log.error("config error: %s", exc)
        payload = {"version": ARTIFACT_VERSION, "ok": False, "dark": True,
                   "reason": CONFIG_ERROR, "note": "configuration invalid",
                   "unmatched_messages": 0, "watermark": None, "hashes": [],
                   "status": {}, "items": []}
        args.out.write_text(json.dumps(payload), encoding="utf-8")
        # This path used to return before the annotation below, so the ONE
        # reason that always needs a human got the quietest exit in the file.
        print(f"::warning::newsletter lane is dark this run: {CONFIG_ERROR}")
        return 0

    now = datetime.now(timezone.utc)
    st = state.load(args.root / state.STATE_FILENAME, now=now)
    result = lane.fetch(cfg.newsletter, st, now)

    args.out.write_text(json.dumps(serialize(result)), encoding="utf-8")
    log.info(
        "newsletter lane: %d items, dark=%s, reason=%s",
        len(result.items), result.dark, result.reason,
    )
    if result.dark and result.reason in WARN_REASONS:
        # A GitHub Actions annotation, visible on the run summary. Counts and
        # slugs only, per the lane's logging rule.
        print(f"::warning::newsletter lane is dark this run: {result.reason}")
    if result.lossy:
        # Not dark, and still a thing to look at: the run read less than the
        # window held, so the cursor deliberately did not advance to now.
        print(
            f"::warning::newsletter lane read a short batch: truncated={result.truncated}, "
            f"unreadable={result.unreadable_messages}"
        )
    rejected = result.unauthenticated_messages + result.unauthenticated_missing
    if rejected:
        print(f"::warning::newsletter lane dropped {rejected} messages on sender authentication")
    return 0


if __name__ == "__main__":
    sys.exit(main())
