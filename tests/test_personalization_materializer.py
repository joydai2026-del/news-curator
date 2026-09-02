from pathlib import Path

import pytest

from scripts.build_auth_callback import TEMPLATE, materialize_callback


def test_materializer_emits_exact_origin_csp_and_public_config(tmp_path: Path) -> None:
    output = tmp_path / "auth/callback/index.html"
    materialize_callback(
        supabase_url="https://project-ref.supabase.co",
        publishable_key="sb_publishable_example",
        output=output,
    )
    rendered = output.read_text()
    assert '<meta name="supabase-url" content="https://project-ref.supabase.co">' in rendered
    assert '<meta name="supabase-publishable-key" content="sb_publishable_example">' in rendered
    assert "connect-src 'self' https://project-ref.supabase.co;" in rendered
    assert "*.supabase.co" not in rendered
    assert "sb_secret_" not in rendered


def test_checked_in_template_remains_fail_closed() -> None:
    template = TEMPLATE.read_text()
    assert '<meta name="supabase-url" content="">' in template
    assert '<meta name="supabase-publishable-key" content="">' in template
    assert "connect-src 'self';" in template


def test_materializer_rejects_secret_key_and_template_overwrite(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="public publishable"):
        materialize_callback(
            supabase_url="https://project-ref.supabase.co",
            publishable_key="sb_secret_x",
            output=tmp_path / "index.html",
        )
    with pytest.raises(ValueError, match="cannot be overwritten"):
        materialize_callback(
            supabase_url="https://project-ref.supabase.co",
            publishable_key="sb_publishable_example",
            output=TEMPLATE,
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://project-ref.supabase.co",
        "https://*.supabase.co",
        "https://project-ref.supabase.co/path",
        "https://user@project-ref.supabase.co",
    ],
)
def test_materializer_rejects_non_exact_https_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        materialize_callback(
            supabase_url=origin,
            publishable_key="sb_publishable_example",
            output=tmp_path / "index.html",
        )
