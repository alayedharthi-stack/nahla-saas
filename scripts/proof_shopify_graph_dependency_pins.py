"""Focused full-Docker-tree guard proof. Synthetic identities/data only; no app startup."""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path('/app')
sys.path.insert(0, str(ROOT))
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate_render as render

DEPENDENCIES = ('backend/__init__.py', 'backend/payments/__init__.py', 'backend/payments/models.py')
IDENTITY = {'project': '11111111-1111-4111-8111-111111111111',
            'environment': '22222222-2222-4222-8222-222222222222',
            'service': '33333333-3333-4333-8333-333333333333'}
BACKUP = {'service': '55555555-5555-4555-8555-555555555555', 'volume': '66666666-6666-4666-8666-666666666666'}
DB = 'postgres:5432/graph_guard_proof'
SENTINEL = Path('/tmp/graph_pin_execution_sentinel')


def iso(value):
    return value.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def inputs():
    release = os.environ['GRAPH_PROOF_SOURCE_SHA']
    assert len(release) == 40 and all(c in '0123456789abcdef' for c in release)
    count, digest = gate.inventory(ROOT)
    now = datetime.now(timezone.utc)
    record = {'record_version': k.APPROVAL_RECORD_VERSION, 'release_sha': release,
        'runtime_inventory_count': count, 'runtime_inventory_digest': digest,
        'target': {**IDENTITY, 'db': DB}, 'phase': 'S0',
        'approval': {'approved': True, 'reviewer': 'synthetic-reviewer', 'independent_review_ref': 'synthetic-proof',
                     'approved_at_utc': iso(now - timedelta(minutes=1)), 'valid_until_utc': iso(now + timedelta(hours=1))},
        'deployment_rollback_snapshot': {'railway_deployment_id': '44444444-4444-4444-8444-444444444444',
            'source_sha': 'b' * 40, 'verified_by': 'synthetic-operator', 'verified_redeployable_at_utc': iso(now - timedelta(minutes=3))},
        'db_recovery_evidence': {'backup': {**{'service_id': BACKUP['service'], 'volume_id': BACKUP['volume']},
            'card_timestamp_utc': iso(now - timedelta(minutes=3)), 'size_mb': 1, 'completed': True,
            'restore_available': True, 'observed_by': 'synthetic-operator', 'observed_at_utc': iso(now - timedelta(minutes=2))},
            'restore_verified': False, 'restore_verification_ref': None, 'restore_verified_at_utc': None}}
    raw = (json.dumps(record, sort_keys=True) + '\n').encode()
    path = Path(tempfile.mkdtemp(prefix='graph-proof-record-')) / 'approval.json'
    path.write_bytes(raw)
    kwargs = {'release_sha': release, 'phase': 'S0', 'approval_sha256': hashlib.sha256(raw).hexdigest(),
              **IDENTITY, 'db': DB, 'backup_service': BACKUP['service'], 'backup_volume': BACKUP['volume']}
    # Use the unchanged strict CLI, then independently match its decoded source.
    argv = [sys.executable, '-B', str(ROOT / 'scripts/operators/shopify_release_gate_render.py'),
            'render', '--root', str(ROOT), '--release-sha', release, '--phase', 'S0', '--approval-file', str(path)]
    for key in ('project', 'environment', 'service', 'db', 'backup_service', 'backup_volume'):
        argv.extend(('--' + key.replace('_', '-'), kwargs[key]))
    rendered = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert rendered.returncode == 0, rendered.stderr[-1000:]
    command = rendered.stdout.strip()
    source = render.decode_command(command)
    assert source == render.render_source(ROOT, **kwargs)
    ast.parse(source)
    env = {name: os.environ[name] for name in ('PATH', 'LANG') if name in os.environ}
    env.update({'RAILWAY_GIT_COMMIT_SHA': release, 'PYTHONDONTWRITEBYTECODE': '1', 'ENVIRONMENT': 'production',
                'DATABASE_URL': 'postgresql://nahla:nahla_password@postgres:5432/graph_guard_proof',
                k.APPROVAL_PAYLOAD_ENV: base64.b64encode(raw).decode()})
    env.update({k.IDENTITY_ENVS[key]: value for key, value in IDENTITY.items()})
    env.update({flag: '0' for flag in k.SHOPIFY_FLAG_ENVS})
    return command, source, env, {'release': release, 'inventory_count': count, 'inventory_digest': digest}


def run(command, env):
    result = subprocess.run(['bash', '-c', command], env=env, capture_output=True, text=True, timeout=70)
    output = result.stdout + result.stderr
    checks = {}
    for line in output.splitlines():
        if ' CHECK ' in line:
            parts = line.split(' CHECK ', 1)[1].split(' ', 2)
            checks[parts[0]] = parts[1]
    return result.returncode, output, checks


def baseline():
    command, source, env, identity = inputs()
    assert identity['inventory_count'] == 143
    code, output, checks = run(command, env)
    assert code == 2 and 'BOOTSTRAP FAIL reason=repository_import_unverified' in output
    assert checks == {name: 'PASS' for name in ('root', 'release_sha', 'inventory', 'libraries', 'migration_preflight')}
    assert 'CHECK connect' not in output and 'CHECK graph' not in output
    # Diagnostic second run: a stdlib profile observes fail()'s caller only.
    # The stock source and every refusal condition remain unchanged.
    prelude = '''import sys,json,os
def trace(frame,event,arg):
 if event=='call' and frame.f_code.co_name=='fail' and frame.f_locals.get('reason')=='repository_import_unverified':
  caller=frame.f_back
  values=caller.f_locals
  spec=values.get('spec')
  origin=values.get('absolute') or getattr(spec,'origin',None)
  relative=os.path.relpath(origin,'/app') if origin and os.path.abspath(origin).startswith('/app/') else '<outside>'
  print('IMPORT_REJECTION '+json.dumps({'module':values.get('fullname'),'origin':relative,'callsite':caller.f_code.co_name}),flush=True)
sys.setprofile(trace)
'''
    import shlex
    diagnostic = 'python -I -B -c ' + shlex.quote(prelude + source)
    traced_code, traced_output, _ = run(diagnostic, env)
    assert traced_code == code and 'BOOTSTRAP FAIL reason=repository_import_unverified' in traced_output
    rejected = json.loads(next(line.split('IMPORT_REJECTION ', 1)[1] for line in traced_output.splitlines() if line.startswith('IMPORT_REJECTION ')))
    assert rejected == {'module': 'backend', 'origin': 'backend/__init__.py', 'callsite': 'find_spec'}
    dependency = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in DEPENDENCIES}
    print('GRAPH_BASELINE_REPORT ' + json.dumps({**identity, 'status': 'EXPECTED_REFUSAL_REPRODUCED',
          'stock_command_unmodified': True, 'diagnostic_profile_did_not_change_refusal': True,
          'rejected_origin': rejected, 'dependency_hashes': dependency, 'database_checks_reached': False,
          'docker_source_tree': 'complete repository as copied by unchanged production Dockerfile'}, sort_keys=True))


def positive():
    command, _, env, identity = inputs()
    assert identity['inventory_count'] == 146
    import sqlalchemy as sa
    engine = sa.create_engine(env['DATABASE_URL'], poolclass=sa.pool.NullPool)
    def snapshot():
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            assert int(conn.exec_driver_sql('SHOW server_version_num').scalar()) == 180006
            return {'rows': gate.read_version_rows(conn), 'retained': k.normalize(gate.observe_retained(conn)),
                    'oids': gate.relation_oids(conn, k.IDENTITY_TABLES),
                    'footprint': gate.observe_footprint(conn, k.DEPLOY_PHASES['S0'])}
    before = snapshot()
    try:
        code, output, checks = run(command, env)
        assert code == 0 and 'RESULT PASS' in output, output[-3500:]
        assert len(checks) == 19 and all(value == 'PASS' for value in checks.values()), checks
        assert snapshot() == before
        assert set(before['rows']) == {'0111', '0113', '0120'}
        assert before['footprint'] == {'tables': [], 'stray_count': 0, 'definitions': {}, 'sequences': []}
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            financial = conn.exec_driver_sql("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND "
                "(tablename LIKE 'merchant_payment_%' OR tablename LIKE 'merchant_balance_%')").scalar()
            assert financial == 0
        print('GRAPH_POSITIVE_REPORT ' + json.dumps({**identity, 'status': 'PASS', 'checks': checks,
              'source_only_guard_unchanged': True, 'schema_rows_oids_preserved': True,
              'shopify_tables': 0, 'financial_tables': financial, 'provider_calls': 0,
              'stock_command_unmodified': True, 'full_application_started': False}, sort_keys=True))
    finally:
        engine.dispose()


def fixture():
    """Create only the synthetic adopted S0 database on the private proof network."""
    import sqlalchemy as sa
    admin = sa.create_engine('postgresql://nahla:nahla_password@postgres:5432/postgres',
                             poolclass=sa.pool.NullPool, isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as conn:
            assert int(conn.exec_driver_sql('SHOW server_version_num').scalar()) == 180006
            conn.exec_driver_sql('CREATE DATABASE graph_guard_proof')
    finally:
        admin.dispose()
    url = 'postgresql://nahla:nahla_password@postgres:5432/graph_guard_proof'
    env = {name: os.environ[name] for name in ('PATH', 'LANG') if name in os.environ}
    env.update({'DATABASE_URL': url, 'PYTHONDONTWRITEBYTECODE': '1', 'ENVIRONMENT': 'test'})
    env.update({flag: '0' for flag in k.SHOPIFY_FLAG_ENVS})
    def upgrade(revision):
        result = subprocess.run([sys.executable, '-B', '-m', 'alembic', 'upgrade', revision],
            cwd=ROOT / 'database', env=env, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr[-2500:]
    upgrade('0093')
    upgrade('0102')
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT to_regclass('public.whatsapp_oauth_nonces')").scalar() is None
            conn.exec_driver_sql("""CREATE TABLE whatsapp_oauth_nonces (
                id SERIAL PRIMARY KEY, nonce_hash VARCHAR(64) NOT NULL,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                connection_mode VARCHAR(32) NOT NULL, redirect_uri_fingerprint VARCHAR(64) NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ NULL,
                created_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT uq_whatsapp_oauth_nonces_hash UNIQUE (nonce_hash))""")
            conn.exec_driver_sql('CREATE INDEX ix_whatsapp_oauth_nonces_expires_at ON whatsapp_oauth_nonces (expires_at)')
            conn.exec_driver_sql('CREATE INDEX ix_whatsapp_oauth_nonces_tenant_id ON whatsapp_oauth_nonces (tenant_id)')
            nonce_oid = conn.exec_driver_sql("SELECT 'whatsapp_oauth_nonces'::regclass::oid").scalar()
        for revision in ('0103', '0111', '0113', '0120'):
            upgrade(revision)
        with engine.connect() as conn:
            assert set(gate.read_version_rows(conn)) == {'0111', '0113', '0120'}
            assert gate.nonce_definition(conn, k.NONCE_TABLE)['columns'][-1][3] is None
            assert conn.exec_driver_sql("SELECT 'whatsapp_oauth_nonces'::regclass::oid").scalar() == nonce_oid
        print('GRAPH_FIXTURE_REPORT ' + json.dumps({'status': 'PASS', 'private_disposable_database': True,
              'postgres_server_version_num': 180006, 'heads': ['0111', '0113', '0120'],
              'nonce_server_default': None, 'adoption_preserved_nonce_oid': True,
              'no_0115_or_shopify_revision_applied': True}, sort_keys=True))
    finally:
        engine.dispose()


def tamper(kind):
    command, source, env, identity = inputs()
    assert identity['inventory_count'] == 146
    assert not SENTINEL.exists()
    original = None
    if kind in DEPENDENCIES:
        path = ROOT / kind
        original = path.read_bytes()
        path.write_bytes(original + b"\n__import__('pathlib').Path('/tmp/graph_pin_execution_sentinel').write_text('executed')\n")
        expected = 'inventory_mismatch'
    elif kind == 'unreviewed_module':
        path = ROOT / 'backend/payments/unreviewed_guard_probe.py'
        assert not path.exists()
        path.write_text("__import__('pathlib').Path('/tmp/graph_pin_execution_sentinel').write_text('executed')\n")
        # Deliberately request the planted module AFTER stock source verification.
        # The actual import guard is unchanged and must refuse before its loader.
        source = source.replace('sys.argv = [gate_path] + ARGV',
            "__import__('backend.payments.unreviewed_guard_probe')\nsys.argv = [gate_path] + ARGV")
        import shlex
        command = 'python -I -B -c ' + shlex.quote(source)
        expected = 'repository_import_unverified'
    else:
        raise AssertionError('unsupported negative case')
    try:
        code, output, _ = run(command, env)
        assert code == 2 and 'BOOTSTRAP FAIL reason=' + expected in output, output[-2500:]
        assert not SENTINEL.exists() and 'CHECK connect' not in output
        print('GRAPH_TAMPER_REPORT ' + json.dumps({**identity, 'case': kind, 'status': 'PASS',
              'refusal': expected, 'planted_code_executed': False, 'database_checks_reached': False,
              'arbitrary_import_allowance_added': False}, sort_keys=True))
    finally:
        if original is not None:
            path.write_bytes(original)
        else:
            path.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('baseline', 'fixture', 'positive', 'tamper'))
    parser.add_argument('--case', choices=DEPENDENCIES + ('unreviewed_module',))
    args = parser.parse_args()
    if args.mode == 'baseline': baseline()
    elif args.mode == 'fixture': fixture()
    elif args.mode == 'positive': positive()
    else: tamper(args.case)
