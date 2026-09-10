"""Protected discovery materializer CLI wiring contracts."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import scripts.materialize_discovery as cli


def test_automatic_baseline_loader_receives_verified_context_anchor(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    current_path = tmp_path / 'current.json'
    current_path.write_text('{}')
    anchor = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    current = SimpleNamespace(configuration_digest='a' * 64)
    previous = SimpleNamespace(configuration_digest='a' * 64)
    loaded = []
    observed = {}

    monkeypatch.setattr(cli.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='b' * 40 + '\n'))
    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(source_snapshot_max_age_seconds=3600))
    monkeypatch.setattr(cli, 'snapshot_config_digest', lambda cfg: 'a' * 64)
    monkeypatch.setattr(cli, 'load_discovery_policy', lambda path: {'windows': {'updates': 24}})

    def load(path, **kwargs):
        loaded.append(Path(path))
        return current if Path(path) == current_path else previous

    def fetch(root_arg, current_arg, output, repository, run_id, **kwargs):
        observed.update(root=root_arg, current=current_arg, repository=repository,
                        run_id=run_id, anchor=kwargs['anchor_before'],
                        attempts=kwargs['attempts'], timeout=kwargs['timeout'],
                        policy=kwargs['policy'], evaluation_clock=kwargs['evaluation_clock'])
        output.write_text('{}')
        return True

    def materialize(cfg, snapshot, policy, secret, **kwargs):
        assert kwargs['previous_snapshot'] is None
        assert kwargs['baseline_loader'](anchor) is previous
        return {'status': 'stored'}

    monkeypatch.setattr(cli, 'load_source_snapshot', load)
    monkeypatch.setattr(cli, 'fetch_baseline', fetch)
    monkeypatch.setattr(cli, 'materialize_private_discovery', materialize)
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_URL', 'https://project.invalid')
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_SECRET_KEY', 'sb_secret_test_only')
    monkeypatch.setenv('NEWS_CURATOR_OWNER_USER_ID', '11111111-1111-4111-8111-111111111111')

    assert cli.main(['--root', str(root), '--source-snapshot', str(current_path),
                     '--code-revision', 'b' * 40, '--baseline-attempts', '7',
                     '--baseline-timeout', '19']) == 0
    assert {key: value for key, value in observed.items() if key != 'evaluation_clock'} == {
        'root': root, 'current': current_path, 'repository': 'owner/repo',
        'run_id': '123', 'anchor': anchor, 'attempts': 7, 'timeout': 19,
        'policy': {'windows': {'updates': 24}},
    }
    assert observed['evaluation_clock'].tzinfo == timezone.utc
    assert len(loaded) == 2


def test_explicit_previous_snapshot_does_not_create_automatic_loader(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    current_path = tmp_path / 'current.json'
    previous_path = tmp_path / 'previous.json'
    current_path.write_text('{}')
    previous_path.write_text('{}')
    snapshots = {current_path: SimpleNamespace(), previous_path: SimpleNamespace()}
    observed = {}
    monkeypatch.setattr(cli.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='c' * 40 + '\n'))
    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(source_snapshot_max_age_seconds=3600))
    monkeypatch.setattr(cli, 'snapshot_config_digest', lambda cfg: 'a' * 64)
    monkeypatch.setattr(cli, 'load_discovery_policy', lambda path: {'windows': {'updates': 24}})
    monkeypatch.setattr(cli, 'load_source_snapshot', lambda path, **kwargs: snapshots[Path(path)])
    monkeypatch.setattr(cli, 'fetch_baseline', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected automatic fetch')))
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_URL', 'https://project.invalid')
    monkeypatch.setenv('NEWS_CURATOR_SUPABASE_SECRET_KEY', 'sb_secret_test_only')
    monkeypatch.setenv('NEWS_CURATOR_OWNER_USER_ID', '11111111-1111-4111-8111-111111111111')

    def materialize(cfg, snapshot, policy, secret, **kwargs):
        observed.update(kwargs)
        return {'status': 'stored'}

    monkeypatch.setattr(cli, 'materialize_private_discovery', materialize)
    assert cli.main(['--root', str(root), '--source-snapshot', str(current_path),
                     '--previous-source-snapshot', str(previous_path), '--code-revision', 'c' * 40]) == 0
    assert observed['previous_snapshot'] is snapshots[previous_path]
    assert observed['baseline_loader'] is None
