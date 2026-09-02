#!/usr/bin/env python3
"""Materialize the fail-closed auth callback for one exact Supabase origin."""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curator.personalization import AuthConfig  # noqa: E402


TEMPLATE = ROOT / "static/auth/callback/index.html"
URL_PLACEHOLDER = '<meta name="supabase-url" content="">'
KEY_PLACEHOLDER = '<meta name="supabase-publishable-key" content="">'
CSP_PLACEHOLDER = "connect-src 'self';"


def materialize_callback(*, supabase_url: str, publishable_key: str, output: Path) -> None:
    config = AuthConfig(supabase_url, publishable_key)
    if output.resolve() == TEMPLATE.resolve():
        raise ValueError("The checked-in fail-closed template cannot be overwritten.")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(URL_PLACEHOLDER) != 1 or template.count(KEY_PLACEHOLDER) != 1 or template.count(CSP_PLACEHOLDER) != 1:
        raise ValueError("The auth callback template contract changed.")
    exact_origin = html.escape(config.supabase_url, quote=True)
    public_key = html.escape(config.publishable_key, quote=True)
    rendered = template.replace(URL_PLACEHOLDER, f'<meta name="supabase-url" content="{exact_origin}">')
    rendered = rendered.replace(KEY_PLACEHOLDER, f'<meta name="supabase-publishable-key" content="{public_key}">')
    rendered = rendered.replace(CSP_PLACEHOLDER, f"connect-src 'self' {exact_origin};")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supabase-url", required=True, help="Exact HTTPS Supabase origin.")
    parser.add_argument("--publishable-key", required=True, help="Public publishable or legacy anon key.")
    parser.add_argument("--output", required=True, type=Path, help="Generated callback HTML path.")
    args = parser.parse_args()
    try:
        materialize_callback(
            supabase_url=args.supabase_url,
            publishable_key=args.publishable_key,
            output=args.output,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote configured callback: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
