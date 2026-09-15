import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from curator.render import configure_m2_reader, render_site


ROOT=Path(__file__).parents[1]


def _workspace(path: Path) -> Path:
    path.mkdir()
    shutil.copytree(ROOT/'curator',path/'curator')
    shutil.copytree(ROOT/'scripts',path/'scripts')
    shutil.copytree(ROOT/'static',path/'static')
    render_site({},[],datetime(2026,9,14,tzinfo=timezone.utc),path/'site',require_summaries=False)
    return path


def _step_command() -> str:
    workflow=yaml.safe_load((ROOT/'.github/workflows/curate.yml').read_text())
    step=next(step for step in workflow['jobs']['build']['steps'] if step.get('name')=='Materialize auth callback')
    return step['run']


def _environment(workspace: Path, *, enabled: str, config: str) -> dict[str,str]:
    return {**os.environ,'RUNNER_TEMP':str(workspace/'runner-temp'),
        'NEWS_CURATOR_PERSONALIZATION_ENABLED':'true','NEWS_CURATOR_M2_ENABLED':enabled,
        'NEWS_CURATOR_M2_READER_CONFIG_JSON':config,
        'NEWS_CURATOR_SUPABASE_URL':'https://project-ref.supabase.co',
        'NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY':'sb_publishable_controlled_test'}


def test_disabled_workflow_step_is_byte_identical_to_existing_m1_callback_path(tmp_path):
    actual=_workspace(tmp_path/'actual'); expected=_workspace(tmp_path/'expected')
    (actual/'runner-temp').mkdir();(expected/'runner-temp').mkdir()
    result=subprocess.run(['/bin/bash','-euo','pipefail','-c',_step_command()],cwd=actual,
        env=_environment(actual,enabled='false',config='{invalid ignored'),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    baseline=subprocess.run(['python','scripts/build_auth_callback.py','--supabase-url','https://project-ref.supabase.co',
        '--publishable-key','sb_publishable_controlled_test','--output','./site/auth/callback/index.html',
        '--site-index','./site/index.html'],cwd=expected,capture_output=True,text=True)
    assert baseline.returncode==0,baseline.stderr
    assert (actual/'site/index.html').read_bytes()==(expected/'site/index.html').read_bytes()
    assert (actual/'site/auth/callback/index.html').read_bytes()==(expected/'site/auth/callback/index.html').read_bytes()


def test_enabled_workflow_step_validates_and_applies_public_m2_config(tmp_path):
    workspace=_workspace(tmp_path/'enabled');(workspace/'runner-temp').mkdir()
    config=json.dumps({'enabled':True,'url':'https://ranker.example','policy_version':'policy-1',
        'model_version':'model-1','provider_policy_id':'provider-policy-1',
        'provider_retention_url':'https://policy.example/privacy','page_size':25,
        'request_timeout_ms':8000,'transport_timeout_ms':20000})
    result=subprocess.run(['/bin/bash','-euo','pipefail','-c',_step_command()],cwd=workspace,
        env=_environment(workspace,enabled='true',config=config),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    page=(workspace/'site/index.html').read_text()
    assert '<meta name="news-curator-m2-enabled" content="true">' in page
    assert '<meta name="news-curator-m2-endpoint" content="https://ranker.example">' in page
    assert '<meta name="news-curator-m2-request-timeout-ms" content="8000">' in page
    assert '<meta name="news-curator-m2-transport-timeout-ms" content="20000">' in page


def test_enabled_workflow_step_fails_closed_without_valid_config(tmp_path):
    base={'enabled':True,'url':'https://ranker.example','policy_version':'policy-1',
        'model_version':'model-1','provider_policy_id':'provider-policy-1',
        'provider_retention_url':'https://policy.example/privacy','page_size':25}
    invalid_configs=('', '{invalid', json.dumps({**base,'request_timeout_ms':8001}),
        json.dumps({**base,'request_timeout_ms':30000}),
        json.dumps({**base,'request_timeout_ms':8000,'transport_timeout_ms':7999}),
        json.dumps({**base,'request_timeout_ms':8000,'transport_timeout_ms':20001}))
    for index,config in enumerate(invalid_configs):
        workspace=_workspace(tmp_path/f'invalid-{index}');(workspace/'runner-temp').mkdir()
        result=subprocess.run(['/bin/bash','-euo','pipefail','-c',_step_command()],cwd=workspace,
            env=_environment(workspace,enabled='true',config=config),capture_output=True,text=True)
        assert result.returncode!=0
        assert '<meta name="news-curator-m2-enabled" content="true">' not in (workspace/'site/index.html').read_text()


def test_renderer_request_timeout_boundary(tmp_path):
    page=tmp_path/'index.html'
    render_site({},[],datetime(2026,9,14,tzinfo=timezone.utc),tmp_path,require_summaries=False)
    base={'enabled':True,'url':'https://ranker.example','policy_version':'policy-1',
        'model_version':'model-1','provider_policy_id':'provider-policy-1',
        'provider_retention_url':'https://policy.example/privacy','page_size':25}
    configure_m2_reader(page,{**base,'request_timeout_ms':8000})
    assert '<meta name="news-curator-m2-request-timeout-ms" content="8000">' in page.read_text()
    assert '<meta name="news-curator-m2-transport-timeout-ms" content="8000">' in page.read_text()
    configure_m2_reader(page,{**base,'request_timeout_ms':8000,'transport_timeout_ms':20000})
    assert '<meta name="news-curator-m2-transport-timeout-ms" content="20000">' in page.read_text()
    for timeout in (8001,30000):
        with pytest.raises(ValueError,match='request deadline'):
            configure_m2_reader(page,{**base,'request_timeout_ms':timeout})
    for timeout in (7999,20001):
        with pytest.raises(ValueError,match='transport deadline'):
            configure_m2_reader(page,{**base,'request_timeout_ms':8000,'transport_timeout_ms':timeout})
