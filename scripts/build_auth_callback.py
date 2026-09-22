#!/usr/bin/env python3
"""Materialize the fail-closed auth callback for one exact Supabase origin."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curator.personalization import AuthConfig  # noqa: E402


TEMPLATE = ROOT / "static/auth/callback/index.html"
URL_PLACEHOLDER = '<meta name="supabase-url" content="">'
KEY_PLACEHOLDER = '<meta name="supabase-publishable-key" content="">'
CSP_PLACEHOLDER = "connect-src 'self';"
INDEX_PLACEHOLDER = "<!-- personalization-link -->"
PERSONALIZATION_LINK = (
    '<a class="profile-link" href="auth/callback/?start=google">Sign in / Interests</a>'
    '<a class="dashboard-link" href="dashboard/">Dashboard</a>'
)


def _configure_page(page: str, config: AuthConfig) -> str:
    if (
        page.count(URL_PLACEHOLDER) != 1
        or page.count(KEY_PLACEHOLDER) != 1
        or page.count(CSP_PLACEHOLDER) != 1
    ):
        raise ValueError("The page auth configuration contract changed.")
    exact_origin = html.escape(config.supabase_url, quote=True)
    public_key = html.escape(config.publishable_key, quote=True)
    return (
        page.replace(URL_PLACEHOLDER, f'<meta name="supabase-url" content="{exact_origin}">')
        .replace(KEY_PLACEHOLDER, f'<meta name="supabase-publishable-key" content="{public_key}">')
        .replace(CSP_PLACEHOLDER, f"connect-src 'self' {exact_origin};")
    )


def materialize_callback(*, supabase_url: str, publishable_key: str, output: Path) -> None:
    config = AuthConfig(supabase_url, publishable_key)
    if output.resolve() == TEMPLATE.resolve():
        raise ValueError("The checked-in fail-closed template cannot be overwritten.")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(URL_PLACEHOLDER) != 1 or template.count(KEY_PLACEHOLDER) != 1 or template.count(CSP_PLACEHOLDER) != 1:
        raise ValueError("The auth callback template contract changed.")
    rendered = _configure_page(template, config)
    client_version = hashlib.sha256((ROOT / "static/auth/client.js").read_bytes()).hexdigest()[:16]
    if rendered.count('src="../client.js"') != 1:
        raise ValueError("The auth callback client asset contract changed.")
    rendered = rendered.replace('src="../client.js"', f'src="../client.js?v={client_version}"')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def validate_m2_ranker_contract(config: object, ranker_policy: Path) -> dict[str, object]:
    """Refuse a Pages build that would reject the deployed ranker contract."""
    if not isinstance(config, dict):
        raise ValueError("M2 reader configuration must be an object.")
    policy = yaml.safe_load(ranker_policy.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("The ranker policy is invalid.")
    expected = {
        "policy_version": policy.get("prompt_revision"),
        "provider_policy_id": policy.get("prompt_revision"),
        "model_version": policy.get("model"),
        "page_size": policy.get("maximum_page_size"),
    }
    mismatched = sorted(key for key, value in expected.items() if config.get(key) != value)
    if mismatched:
        raise ValueError("M2 reader configuration does not match the ranker policy: " + ", ".join(mismatched))
    return config


def activate_personalization_link(
    site_index: Path,
    *,
    supabase_url: str | None = None,
    publishable_key: str | None = None,
    m2_config: dict[str, object] | None = None,
) -> None:
    """Expose the settings entry point only in a configured site build."""
    page = site_index.read_text(encoding="utf-8")
    if page.count(INDEX_PLACEHOLDER) != 1:
        raise ValueError("The rendered site personalization-link contract changed.")
    page = page.replace(INDEX_PLACEHOLDER, PERSONALIZATION_LINK)
    if supabase_url is not None or publishable_key is not None:
        config = AuthConfig(supabase_url or "", publishable_key or "")
        if (
            page.count(URL_PLACEHOLDER) != 1
            or page.count(KEY_PLACEHOLDER) != 1
            or page.count(CSP_PLACEHOLDER) != 1
        ):
            raise ValueError("The rendered site auth configuration contract changed.")
        page = _configure_page(page, config)
        dashboard = site_index.parent / "dashboard/index.html"
        if dashboard.is_file():
            configured_dashboard = _configure_page(dashboard.read_text(encoding="utf-8"), config)
            dashboard.write_text(configured_dashboard, encoding="utf-8")
    site_index.write_text(page, encoding="utf-8")
    if m2_config is not None:
        from curator.render import configure_m2_reader
        configure_m2_reader(site_index, m2_config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supabase-url", required=True, help="Exact HTTPS Supabase origin.")
    parser.add_argument("--publishable-key", required=True, help="Public publishable or legacy anon key.")
    parser.add_argument("--output", required=True, type=Path, help="Generated callback HTML path.")
    parser.add_argument("--site-index", type=Path, help="Rendered site index whose personalization link should be activated.")
    parser.add_argument("--m2-config", type=Path, default=os.environ.get("NEWS_CURATOR_M2_READER_CONFIG"),
                        help="Optional validated public M2 reader configuration JSON path; disabled when absent.")
    parser.add_argument("--ranker-policy", type=Path,
                        help="Ranker policy that the enabled public M2 reader must match exactly.")
    args = parser.parse_args()
    try:
        m2_config = json.loads(args.m2_config.read_text()) if args.m2_config else None
        if m2_config is not None:
            if args.ranker_policy is None:
                raise ValueError("The enabled M2 reader requires a ranker policy contract.")
            m2_config = validate_m2_ranker_contract(m2_config, args.ranker_policy)
        materialize_callback(
            supabase_url=args.supabase_url,
            publishable_key=args.publishable_key,
            output=args.output,
        )
        if args.site_index is not None:
            activate_personalization_link(
                args.site_index,
                supabase_url=args.supabase_url,
                publishable_key=args.publishable_key,
                m2_config=m2_config,
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Wrote configured callback: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
