"""Closed0124 bridge/0126 quarantine profiles on disposable PostgreSQL."""
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text
from backend.tests import test_shopify_release_gate_pg as h
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import coupon_retirement_schema as schema
from backend.tests import _shopify_release_gate_harness as harness

pytestmark = h.pytestmark
ROOT = Path(__file__).resolve().parents[2]


def check(db,phase):
    definition=k.phase(phase)
    ec,expected=gate.check_expectations(ROOT,phase)
    assert ec.status==k.PASS,ec
    gc,known=gate.check_graph(ROOT,definition)
    assert gc.status==k.PASS,gc
    return gate.run_database_checks(h._loopback_url(db),definition,known,expected,db)


def test_explicit_pre_post_profiles_and_all_seventeen_shopify_definitions():
    db=h._fresh_db('salla_profiles')
    try:
        h._build(db,'0124')
        assert all(c.status==k.PASS for c in check(db,'S4_COUPON_PRE_DORMANT'))
        assert any(c.status==k.FAIL for c in check(db,'S4_COUPON_POST_DORMANT'))
        before=h._sql(db,"SELECT count(*) FROM shopify_connections")
        proc=h._alembic(db,'upgrade','0126')
        assert proc.returncode==0,proc.stderr[-2000:]
        assert all(c.status==k.PASS for c in check(db,'S4_COUPON_POST_DORMANT'))
        assert any(c.status==k.FAIL for c in check(db,'S4_COUPON_PRE_DORMANT'))
        assert h._sql(db,"SELECT count(*) FROM shopify_connections")==before
        from scripts.operators.bootstrap_migration_contract import INTEGRATION_BOOTSTRAP_TARGET
        assert INTEGRATION_BOOTSTRAP_TARGET=='0093'
    finally:
        h._drop(db)


def test_rendered_pre_post_dormant_pilot_profiles_and_rollback_refusal(tmp_path):
    """Unix trusted bootstrap, not just the direct schema observer."""
    db=h._fresh_db('salla_rendered')
    try:
        h._build(db,'0124')
        root=harness.build_root(tmp_path/'root')
        deadline=(datetime.now(timezone.utc)+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        pilot={'NAHLA_SHOPIFY_CONNECTION_ENABLED':'true','NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED':'true',
            'SHOPIFY_INSTALL_PILOT_ADMISSION':json.dumps({k.PILOT_SHOP:{'sha256':'a'*64,'expires_at':deadline}})}
        for mode,env in [('DORMANT',{}),('PILOT',pilot)]:
            h._assert_pass(root,'S4_COUPON_PRE_'+mode,db,'closed PRE profile',extra_env=env)
        proc=h._alembic(db,'upgrade','0126')
        assert proc.returncode==0,proc.stderr[-2000:]
        for mode,env in [('DORMANT',{}),('PILOT',pilot)]:
            h._assert_pass(root,'S4_COUPON_POST_'+mode,db,'closed POST profile',extra_env=env)
        h._assert_fail(root,'S4_COUPON_PRE_DORMANT',db,'post DB cannot use pre contract',
            check='version_rows',reason=k.R_VERSION_ROWS_MISMATCH)
        path=harness.approval_for(root,'S4_COUPON_POST_DORMANT',h._target(db))
        record=json.loads(path.read_text())
        for old in ['a4e96891bda691b1ee0f7cb105c5810e727c39bd','efb8f7a7d7c2fabe876f71cfff17bdaa8a87ccd6']:
            changed=json.loads(json.dumps(record))
            changed['schema_rollback']['source_sha']=old
            changed['deployment_rollback_snapshot']['source_sha']=old
            invalid=harness.approval_file(root.parent/'invalid',changed,old+'.json')
            result=h._assert_fail(root,'S4_COUPON_POST_DORMANT',db,'old source is not functional rollback',
                approval=invalid,check='approval',reason=k.R_SCHEMA_ROLLBACK_INVALID)
            assert 'connect' not in harness.checks(result)
    finally:
        h._drop(db)


@pytest.fixture(scope='module')
def built():
    db=h._fresh_db('salla_fence_closure')
    h._build(db,'0126')
    yield db
    h._drop(db)


@pytest.mark.parametrize('statement',[
    'ALTER TABLE coupon_identity_retirements DROP CONSTRAINT ck_coupon_retirement_distinct',
    'DROP INDEX ix_coupon_retirement_identity',
    'DROP TABLE coupon_identity_retirements CASCADE',
    'ALTER TABLE coupon_identity_retirements DROP CONSTRAINT coupon_identity_retirements_retired_id_fkey',
    'ALTER TABLE coupon_identity_retirements ADD COLUMN unexpected text',
    'ALTER TABLE coupons DISABLE TRIGGER coupon_retirement_guard',
    'ALTER TABLE coupons ENABLE ALWAYS TRIGGER coupon_retirement_guard',
    'ALTER TABLE coupon_identity_retirements DISABLE TRIGGER coupon_retirement_audit_guard',
    'ALTER FUNCTION guard_coupon_retirement() SECURITY DEFINER',
    'ALTER FUNCTION guard_coupon_retirement() IMMUTABLE',
    'ALTER FUNCTION guard_coupon_retirement() SET search_path=public',
    'ALTER TABLE coupon_identity_retirements ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE coupon_identity_retirements FORCE ROW LEVEL SECURITY',
    "CREATE POLICY unexpected ON coupon_identity_retirements USING (true)",
    'ALTER TABLE coupons ENABLE ROW LEVEL SECURITY',
    "SET LOCAL session_replication_role='replica'",
    'SET LOCAL row_security=off',
    "CREATE FUNCTION unexpected_handler() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'; CREATE TRIGGER zzz_after_guard BEFORE INSERT ON coupons FOR EACH ROW EXECUTE FUNCTION unexpected_handler()",
    "CREATE FUNCTION guard_coupon_retirement_unknown() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    'CREATE TABLE coupon_identity_unknown (id int)',
    "CREATE TYPE coupon_identity_unknown_type AS ENUM ('x')",
    'CREATE SCHEMA extra_coupon_schema; CREATE TABLE extra_coupon_schema.coupon_identity_retirements(id int)',
    "CREATE OR REPLACE FUNCTION guard_coupon_retirement() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
    'DROP TRIGGER coupon_retirement_guard ON coupons',
])
def test_missing_disabled_drifted_or_unknown_owned_objects_are_rejected(built,statement):
    expected=json.loads((ROOT/k.COUPON_EXPECTATIONS_RELATIVE_PATH).read_text())['post']
    engine=create_engine(h._loopback_url(built))
    try:
        with engine.connect() as conn:
            assert schema.matches(schema.observe(conn,gate.table_definition),expected)
            conn.execute(text(statement))
            assert not schema.matches(schema.observe(conn,gate.table_definition),expected)
            conn.rollback()
            assert schema.matches(schema.observe(conn,gate.table_definition),expected)
    finally:
        engine.dispose()
