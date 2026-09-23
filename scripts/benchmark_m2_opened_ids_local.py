"""Bounded isolated PostgreSQL benchmark. Prints counts, sizes and time, not IDs."""
from __future__ import annotations

import json
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import test_m2_phase2_postgres_runtime as suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-candidates', action='store_true')
    parser.add_argument('--runtime-suite', action='store_true')
    args = parser.parse_args()
    binaries = {name: shutil.which(name) for name in ('postgres', 'initdb', 'pg_ctl', 'psql')}
    if not all(binaries.values()):
        raise RuntimeError('installed local PostgreSQL tools required; nothing will be installed')

    def run(name, *args, **kwargs):
        result = subprocess.run([binaries[name], *args], capture_output=True,
                                text=True, timeout=120, **kwargs)
        if result.returncode:
            raise RuntimeError(f'local {name} failed; output withheld to avoid printing IDs')
        return result

    version = run('postgres', '--version').stdout.strip()
    if ' 16.' not in version:
        raise RuntimeError('this receipt requires installed PostgreSQL16')
    cluster = Path(tempfile.mkdtemp(prefix='news-curator-opened-bench-', dir='/private/tmp'))
    port = '55487'  # Unix socket inside a unique0700 directory, no TCP listener.

    def sql(_container, statement, check=True):
        result = subprocess.run([binaries['psql'], '-X', '-At', '-v', 'ON_ERROR_STOP=1',
            '-h', str(cluster), '-p', port, '-d', 'postgres'], input=statement,
            capture_output=True, text=True, timeout=120)
        if check and result.returncode:
            raise RuntimeError('local SQL failed; output withheld to avoid printing IDs')
        return result

    suite._sql = sql
    run('initdb', '-D', str(cluster / 'data'), '-A', 'trust')
    try:
        run('pg_ctl', '-D', str(cluster / 'data'), '-l', str(cluster / 'server.log'),
            '-o', f"-k {cluster} -p {port} -c listen_addresses='' -c unix_socket_permissions=0700 -c timezone=UTC",
            '-w', 'start')
        sql(None, """
          create role anon nologin;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create schema extensions;
          create extension pgcrypto with schema extensions;
          create schema auth;
          create table auth.users(id uuid primary key);
          create function auth.uid() returns uuid language sql stable as $$
            select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
          create function auth.jwt() returns jsonb language sql stable as $$
            select coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
          grant usage on schema public,auth to anon,authenticated,service_role;
          grant execute on function auth.uid(),auth.jwt() to anon,authenticated,service_role;
        """)
        for migration in suite.MIGRATIONS:
            sql(None, (ROOT / migration).read_text())
        sql(None, f"insert into auth.users(id) values ('{suite.OWNER}'), ('{suite.OTHER}');")
        if args.runtime_suite:
            import pytest
            suite._seed_corpus(None)
            suite._psql_command = lambda _db: [binaries['psql'], '-X', '-At',
                '-v', 'ON_ERROR_STOP=1', '-h', str(cluster), '-p', port, '-d', 'postgres']
            suite.db = pytest.fixture(scope='module')(lambda: None)
            status = pytest.main(['-o', 'addopts=', '-q', '-p', 'no:cacheprovider',
                                 str(ROOT / 'tests/test_m2_phase2_postgres_runtime.py'), '--tb=short'])
            if status:
                raise RuntimeError(f'local PostgreSQL runtime suite exit {status}')
            return
        if args.owner_candidates:
            suite.test_owner_candidates_security_and_disabled_policy(None)
            suite.test_owner_candidates_opened_filter_stays_after_dedupe(None)
            receipt = suite.benchmark_owner_candidates_long_opened_head(None)
        else:
            receipt = suite.benchmark_distinct_opened_candidate_ids(None)
        receipt.update(postgres=version, cluster=str(cluster), evidence_grade='B')
        print(json.dumps(receipt, sort_keys=True))
    finally:
        run('pg_ctl', '-D', str(cluster / 'data'), '-m', 'fast', '-w', 'stop')
        print('Local test cluster stopped. Files retained, no cleanup deletion.')


if __name__ == '__main__':
    main()
