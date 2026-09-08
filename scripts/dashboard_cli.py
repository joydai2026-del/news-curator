#!/usr/bin/env python3
"""Read one bounded private News Curator dashboard snapshot."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curator.dashboard import DashboardClient, MAX_SAVED_PAGES  # noqa: E402
from curator.personalization import AgentAuth, AuthConfig, AuthError, MacOSKeychainStorage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot",))
    parser.add_argument("--saved-pages", type=int, default=1)
    args = parser.parse_args(argv)
    if not 1 <= args.saved_pages <= MAX_SAVED_PAGES:
        parser.error(f"--saved-pages must be between 1 and {MAX_SAVED_PAGES}")
    try:
        config = AuthConfig(
            os.environ.get("NEWS_CURATOR_SUPABASE_URL", ""),
            os.environ.get("NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", ""),
        )
        account = urllib.parse.urlsplit(config.supabase_url).hostname or "news-curator"
        session = AgentAuth(config, MacOSKeychainStorage(account=account)).valid_session()
        print(json.dumps(DashboardClient(config).snapshot(session, saved_pages=args.saved_pages)))
        return 0
    except (AuthError, ValueError, socket.error):
        print("Dashboard read failed safely. No credential or private response was printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
