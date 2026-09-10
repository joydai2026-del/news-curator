#!/usr/bin/env python3
"""Read signed-in discovery using protected agent credentials; export only explicitly."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from curator.personalization import AgentAuth, AuthConfig, MacOSKeychainStorage  # noqa: E402
from curator.private_discovery import DiscoveryClient, select_entries, write_private_json  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('read',))
    parser.add_argument('--edition-id')
    parser.add_argument('--lane', choices=('updates', 'hot', 'interested', 'surprise'), default='updates')
    parser.add_argument('--topic')
    parser.add_argument('--output', type=Path, help='Explicit private export of the complete validated edition; lane/topic filter the view counts.')
    args = parser.parse_args(argv)
    try:
        cfg = AuthConfig(os.environ.get('NEWS_CURATOR_SUPABASE_URL', ''), os.environ.get('NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY', ''))
        account = urllib.parse.urlsplit(cfg.supabase_url).hostname or 'news-curator'
        session = AgentAuth(cfg, MacOSKeychainStorage(account=account)).valid_session()
        response = DiscoveryClient(cfg).read(session, edition_id=args.edition_id)
        selected = select_entries(response, lane=args.lane, topic=args.topic)
        if args.output is not None:
            write_private_json(args.output, response)
        print(json.dumps({'schema_version': 1, 'status': response['status'],
            'selected_count': len(selected), 'total_count': len(response['edition']['entries']) if response['edition'] else 0,
            'private_export_written': args.output is not None}, sort_keys=True))
        return 0
    except Exception:
        print('Private discovery read failed safely. No private response was printed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
