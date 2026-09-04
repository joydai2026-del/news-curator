#!/usr/bin/env python3
"""Build or validate the credential-free M1 saved-interest ranking artifact."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curator.config import ConfigError, load_config  # noqa: E402
from curator.personalization.materializer import (  # noqa: E402
    MaterializationError,
    SecretPreferenceConfig,
    fetch_interest_profile,
)
from curator.personalization.ranking import (  # noqa: E402
    InterestArtifactError,
    build_interest_artifact,
    load_interest_artifact,
    ranking_config_digest,
    story_key,
)
from curator.source_snapshot import (  # noqa: E402
    SourceSnapshotError,
    load_source_snapshot,
    snapshot_config_digest,
)


def _snapshot(root: Path, path: Path):
    config = load_config(root)
    snapshot_digest = snapshot_config_digest(config)
    snapshot = load_source_snapshot(
        path,
        expected_configuration_digest=snapshot_digest,
        max_age_seconds=config.source_snapshot_max_age_seconds,
    )
    return snapshot, ranking_config_digest(config)


def _build(args: argparse.Namespace) -> int:
    config = SecretPreferenceConfig(
        os.environ.get("NEWS_CURATOR_SUPABASE_URL", ""),
        os.environ.get("NEWS_CURATOR_SUPABASE_SECRET_KEY", ""),
        os.environ.get("NEWS_CURATOR_OWNER_USER_ID", ""),
    )
    snapshot, config_digest = _snapshot(args.root, args.source_snapshot)
    profile = fetch_interest_profile(config)
    items = [item for result in snapshot.results for item in result.items]
    payload = build_interest_artifact(
        profile,
        items,
        source_snapshot_digest=snapshot.content_digest,
        configuration_digest=config_digest,
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Raw preferences cannot enter the exact artifact schema. Credentials and
    # the owner id are still checked byte-for-byte as a final output boundary.
    # Do not substring-scan arbitrary interests: valid terms such as "score"
    # naturally occur in the schema and would make the build fail incorrectly.
    forbidden = [config.secret_key, config.owner_user_id]
    if any(value and value.encode("utf-8") in encoded for value in forbidden):
        raise MaterializationError("The ranking artifact retained private input.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    args.output.chmod(0o600)
    print("saved-interest ranking ready")
    return 0


def _validate(args: argparse.Namespace) -> int:
    snapshot, config_digest = _snapshot(args.root, args.source_snapshot)
    allowed_story_keys = {
        story_key(item)
        for result in snapshot.results
        for item in result.items
    }
    load_interest_artifact(
        args.input,
        expected_source_snapshot_digest=snapshot.content_digest,
        expected_configuration_digest=config_digest,
        allowed_story_keys=allowed_story_keys,
    )
    print("saved-interest ranking valid")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--root", type=Path, default=Path.cwd())
    build.add_argument("--source-snapshot", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.set_defaults(handler=_build)

    validate = commands.add_parser("validate")
    validate.add_argument("--root", type=Path, default=Path.cwd())
    validate.add_argument("--source-snapshot", type=Path, required=True)
    validate.add_argument("--input", type=Path, required=True)
    validate.set_defaults(handler=_validate)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        ConfigError,
        InterestArtifactError,
        MaterializationError,
        OSError,
        SourceSnapshotError,
        ValueError,
    ):
        print("saved-interest ranking failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
