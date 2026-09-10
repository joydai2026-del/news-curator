"""Guard private route grants and boundaries that regressions must never widen."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/202609090001_discovery_lanes.sql'


def test_discovery_routes_keep_subject_attribution_at_the_server():
    sql = MIGRATION.read_text()
    assert 'create function public.discovery_edition(p_edition_id text default null)' in sql
    assert 'return public.private_discovery_read(auth.uid(),p_edition_id)' in sql
    assert 'grant execute on function public.discovery_edition(text) to authenticated' in sql
    for name, signature in [('finalize_private_discovery', 'text,text'), ('private_discovery_context', 'uuid'),
                            ('private_discovery_identity', 'uuid,text')]:
        assert f'revoke all on function public.{name}({signature}) from public,anon,authenticated' in sql
        assert f'grant execute on function public.{name}({signature}) to service_role' in sql
    assert 'insert into public.publication_entries' not in sql
    assert 'update public.publication_entries' not in sql
    assert "convert_to(p_payload_text,'UTF8')" in sql
    assert 'discovery_story_access(caller,p_story_id)' in sql
    assert 'private, no-store' in sql
