"""Quarantine contract on actual PostgreSQL, including old-runtime writes."""
import asyncio
import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from test_salla_coupon_identity import Provider
from database.models import Coupon, Integration
from services.coupon_active_sync import dispatch_active_change, queue_active_change, retry_active_changes
from services.coupon_identity import CouponIdentityConflict
from services.coupon_retirement import reconcile_duplicate, snapshot, digest
from services.coupon_salla_push import push_coupon_to_salla, is_pushable_manual_coupon
from services.native_ai_coupon_eligibility import is_native_ai_allocatable_coupon
from services.store_sync import StoreSyncService


@pytest.fixture
def pg():
    url = os.getenv("A1_PG_TEST_DATABASE_URL")
    if not url:
        if os.getenv("A1_PG_INTEGRATION_REQUIRED") == "1":
            pytest.fail("PostgreSQL proof required")
        pytest.skip("PostgreSQL proof not configured")
    schema = "coupon_retirement_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE TABLE "{schema}".tenants (id INTEGER PRIMARY KEY)'))
        conn.execute(text(f'''CREATE TABLE "{schema}".integrations (
            id INTEGER PRIMARY KEY, tenant_id INTEGER, provider TEXT, enabled BOOLEAN,
            external_store_id TEXT, config JSONB)'''))
        conn.execute(text(f'''CREATE TABLE "{schema}".coupons (
            id SERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, code TEXT NOT NULL,
            description TEXT, discount_type TEXT, discount_value TEXT, metadata JSONB,
            expires_at TIMESTAMP, source_type TEXT NOT NULL DEFAULT 'manual',
            coupon_level TEXT, allocation_channel TEXT, UNIQUE(tenant_id,code))'''))
        conn.execute(text(f'INSERT INTO "{schema}".tenants VALUES (1),(2)'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    path = Path(__file__).resolve().parents[1] / "database/migrations/versions/0126_coupon_identity_retirements.py"
    spec = importlib.util.spec_from_file_location("retirement_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    Session = sessionmaker(bind=engine)
    try:
        with Session() as db:
            integration = Integration(id=22,tenant_id=1,provider="salla",enabled=True,
                external_store_id="202",config={"store_id":"202","api_sync_enabled":True,"refresh_token":"fake"})
            db.add(integration)
            for cid, code, source, value in [(10505,"SHOES123","manual","1"),(10506,"shoes123","imported","2")]:
                db.add(Coupon(id=cid,tenant_id=1,code=code,source_type=source,discount_type="percentage",
                    discount_value=value,coupon_level="silver",allocation_channel="ai",
                    extra_metadata={"salla_coupon_id":"101","external_id":"101","active":False,
                        "usage_count":0,"usage_limit":1,"ai_allocatable":False,"merchant_note":"preserve"}))
            db.commit()
        yield Session, migration
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


BINDING = {"tenant_id":1,"integration_id":22,"store_id":"202"}
RAW = {"id":101,"code":"shoes123","status":"inactive","type":"percentage","amount":2}


def repair(db, raw=None, binding=None, mutate_inventory=None, expected=None):
    rows = db.query(Coupon).filter(Coupon.id.in_([10505,10506])).order_by(Coupon.id).all()
    expected = expected or digest({str(r.id):snapshot(r) for r in rows})
    inventory = {"snapshot_digest":expected,"unresolved_references":0,
                 "review_reference":"test-only-reviewed-inventory","canonical_id":10505,"retired_id":10506,
                 "provider_digest":digest(raw or RAW),"provider_observed_at":datetime.now(timezone.utc).isoformat(),
                 "maintenance_fence_reference":"test-isolated-schema"}
    if mutate_inventory:
        inventory.update(mutate_inventory)
    return reconcile_duplicate(db,binding or BINDING,10505,10506,raw or RAW,expected,inventory)


def test_repair_poll_dispatch_push_allocation_retry_and_history(pg, monkeypatch):
    Session, _ = pg
    with Session() as db:
        first = db.get(Coupon,10505)
        integration = db.get(Integration,22)
        queue_active_change(first,integration,False)
        db.commit()
        before = snapshot(db.get(Coupon,10506))
        repair(db)
        db.commit()
        retired = db.get(Coupon,10506)
        frozen = snapshot(retired)
        audit = db.execute(text("SELECT audit FROM coupon_identity_retirements")).scalar_one()
        assert audit["before"]["10506"] == before
        assert audit["before"]["10505"]["extra_metadata"]["active_sync_pending"]["desired"] is False
        assert retired.code == "shoes123" and retired.source_type == "imported"
        assert not is_pushable_manual_coupon(retired)
        assert not is_native_ai_allocatable_coupon(retired,tenant_id=1,resolved_level="silver")
        provider = Provider(1)
        provider.raw = deepcopy(RAW)
        ok,_ = asyncio.run(push_coupon_to_salla(db,1,retired,adapter=provider))
        assert not ok and provider.raw == RAW
        with pytest.raises(CouponIdentityConflict,match="coupon_retired"):
            queue_active_change(retired,integration,True)
        asyncio.run(dispatch_active_change(db,1,10506,integration,provider))
        monkeypatch.setattr("store_integration.registry.adapter_for_integration", lambda _: provider)
        asyncio.run(retry_active_changes(db,integration))
        assert provider.puts == [] and snapshot(retired) == frozen
        svc = StoreSyncService(db,1,integration_connection_id=22,adapter=provider)
        for code in ["shoes123","SHOES123","Shoes123"]:
            provider.raw["code"] = code
            assert asyncio.run(svc.sync_coupons(raw_list=[provider.raw],record_sync_meta=False)) == 1
            db.commit()
        first = db.get(Coupon,10505)
        assert first.code == "SHOES123" and first.discount_value == "2"
        assert first.extra_metadata["merchant_note"] == "preserve"
        for active in [True,False]:
            queue_active_change(first,integration,active)
            db.commit()
            asyncio.run(dispatch_active_change(db,1,10505,integration,provider))
            assert first.extra_metadata["active"] is active
        assert db.query(Coupon).count() == 2 and snapshot(retired) == frozen


@pytest.mark.parametrize("change", ["active","delete","old_import","mixed_insert","id_insert","null_binding","forged_binding","audit_update","audit_delete"])
def test_previous_runtime_cannot_rebind_activate_delete_or_recreate(pg,change):
    Session,_ = pg
    with Session() as db:
        repair(db)
        db.commit()
        statements = {
            "active":"UPDATE coupons SET metadata = metadata || jsonb_build_object('active',true) WHERE id=10506",
            "delete":"DELETE FROM coupons WHERE id=10506",
            # The exact efb8 importer uses tenant+code, then updates this row.
            "old_import":"UPDATE coupons SET metadata = metadata || '{\"salla_coupon_id\":\"101\"}'::jsonb WHERE tenant_id=1 AND code='shoes123'",
            "mixed_insert":"INSERT INTO coupons (tenant_id,code,metadata) VALUES (1,'Shoes123','{}')",
            "id_insert":"INSERT INTO coupons (tenant_id,code,metadata) VALUES (1,'OTHER','{\"external_id\":\"101\"}')",
            "null_binding":"INSERT INTO coupons (tenant_id,code,metadata) VALUES (1,'OTHER','{\"external_id\": \"101\", \"salla_identity\": null}')",
            "forged_binding":"INSERT INTO coupons (tenant_id,code,metadata) VALUES (1,'OTHER','{\"external_id\": \"101\", \"salla_identity\": {\"tenant_id\": 1, \"integration_id\": 999, \"store_id\": \"303\"}}')",
            "audit_update":"UPDATE coupon_identity_retirements SET code_key='other'",
            "audit_delete":"DELETE FROM coupon_identity_retirements",
        }
        with pytest.raises(IntegrityError,match="coupon_retired|coupon_retirement_audit_immutable"):
            db.execute(text(statements[change]))
        db.rollback()
        assert db.query(Coupon).count() == 2
        assert "salla_coupon_id" not in db.get(Coupon,10506).extra_metadata


@pytest.mark.parametrize("bad", ["active","provider","code","store","tenant","integration","digest","inventory","intent","aliases","stale_provider","unfenced","provider_digest"])
def test_repair_preconditions_abort_without_partial_writes(pg,bad):
    Session,_ = pg
    with Session() as db:
        raw,binding = deepcopy(RAW),dict(BINDING)
        kwargs = {}
        if bad == "active": raw["status"] = "active"
        if bad == "provider": raw["id"] = 999
        if bad == "code": raw["code"] = "OTHER"
        if bad == "store": binding["store_id"] = "303"
        if bad == "tenant": binding["tenant_id"] = 2
        if bad == "integration": binding["integration_id"] = 999
        if bad == "digest": kwargs["expected"] = "stale"
        if bad == "inventory": kwargs["mutate_inventory"] = {"unresolved_references":1}
        if bad == "stale_provider": kwargs["mutate_inventory"] = {"provider_observed_at":"2000-01-01T00:00:00+00:00"}
        if bad == "unfenced": kwargs["mutate_inventory"] = {"maintenance_fence_reference":None}
        if bad == "provider_digest": kwargs["mutate_inventory"] = {"provider_digest":"other"}
        if bad == "intent":
            queue_active_change(db.get(Coupon,10505),db.get(Integration,22),True)
            db.commit()
        if bad == "aliases":
            row = db.get(Coupon,10506)
            row.extra_metadata = dict(row.extra_metadata,external_id="999")
            db.commit()
        before = [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)]
        with pytest.raises(CouponIdentityConflict):
            repair(db,raw,binding,**kwargs)
        db.rollback()
        assert [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)] == before
        assert db.execute(text("SELECT count(*) FROM coupon_identity_retirements")).scalar_one() == 0


def test_transaction_rollback_and_application_rollback_keep_safety(pg):
    Session,migration = pg
    with Session() as db:
        before = [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)]
        repair(db)
        db.rollback()
        assert [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)] == before
        assert db.execute(text("SELECT count(*) FROM coupon_identity_retirements")).scalar_one() == 0
        repair(db)
        db.commit()
        with Operations.context(MigrationContext.configure(db.connection())):
            with pytest.raises(RuntimeError,match="persistent_rollback_fence"):
                migration.downgrade()
        db.rollback()
        # Other tenants and positively bound other stores retain their own ID.
        db.add(Coupon(tenant_id=2,code="shoes123",extra_metadata={"salla_coupon_id":"101"}))
        db.add(Integration(id=23,tenant_id=1,provider="salla",enabled=True,external_store_id="303",config={"store_id":"303"}))
        db.flush()
        db.add(Coupon(tenant_id=1,code="OTHER",extra_metadata={"salla_coupon_id":"101",
            "salla_identity":{"tenant_id":1,"integration_id":23,"store_id":"303"}}))
        db.commit()
        assert db.query(Coupon).count() == 4
        assert db.get(Coupon,10505).extra_metadata["active"] is False


def previous_importer():
    """Load the pinned efb8 method without importing another application tree."""
    import services.store_sync as module
    executable = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    source = subprocess.check_output([executable,"show",
        "efb8f7a7d7c2fabe876f71cfff17bdaa8a87ccd6:backend/services/store_sync.py"],
        cwd=Path(__file__).resolve().parents[1]).decode("utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name == "StoreSyncService")
    method = next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name == "sync_coupons")
    namespace = dict(module.__dict__)
    exec(compile(ast.Module(body=[method],type_ignores=[]),"pinned-efb8-sync_coupons","exec"),namespace)
    return namespace["sync_coupons"]


@pytest.mark.parametrize("code", ["shoes123","Shoes123"])
def test_exact_previous_importer_is_fenced_after_application_rollback(pg,code):
    """Execute the actual pinned efb8 method, not a rewritten approximation."""
    import_coupons = previous_importer()
    Session,_ = pg
    with Session() as db:
        repair(db)
        db.commit()
        before = [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)]
        svc = StoreSyncService(db,1,integration_connection_id=22,adapter=Provider(1))
        with pytest.raises(IntegrityError,match="coupon_retired"):
            asyncio.run(import_coupons(svc,raw_list=[dict(RAW,code=code)],record_sync_meta=False))
        db.rollback()
        assert [snapshot(r) for r in db.query(Coupon).order_by(Coupon.id)] == before


@pytest.mark.parametrize("code", ["shoes123", "Shoes123"])
def test_old_batch_aborts_other_coupon_but_alias_aware_bridge_remains_functional(pg, code):
    """A safe rollback must sync ordinary coupons, not merely reject old writes.

    The pinned importer successfully writes the first coupon before the second
    collides with retired history. Its entire transaction must roll back. The
    current alias-aware implementation must commit the same batch while leaving
    the retired row and immutable audit unchanged.
    """
    import_coupons = previous_importer()
    Session, _ = pg
    with Session() as db:
        repair(db)
        db.add(Coupon(id=10507, tenant_id=1, code="COAT20", source_type="imported",
                      discount_type="percentage", discount_value="1",
                      extra_metadata={"salla_coupon_id": "202", "external_id": "202",
                                      "salla_identity": dict(BINDING), "active": False}))
        db.commit()
        before = [snapshot(row) for row in db.query(Coupon).order_by(Coupon.id)]
        retired_before = snapshot(db.get(Coupon, 10506))
        audit_before = db.execute(text("SELECT audit FROM coupon_identity_retirements")).scalar_one()
        ordinary = dict(id=202, code="COAT20", status="inactive", type="percentage", amount=7)
        batch = [ordinary, dict(RAW, code=code)]
        svc = StoreSyncService(db, 1, integration_connection_id=22, adapter=Provider(1))
        successful_updates = []

        def record_update(_conn, _cursor, _statement, parameters, context, _executemany):
            compiled = context.compiled
            statement = compiled.statement if compiled is not None else None
            if (getattr(statement, "is_update", False)
                    and statement.table.name == "coupons"):
                successful_updates.append(dict(parameters))

        engine = db.get_bind()
        event.listen(engine, "after_cursor_execute", record_update)
        try:
            with pytest.raises(IntegrityError, match="coupon_retired"):
                asyncio.run(import_coupons(svc, raw_list=deepcopy(batch), record_sync_meta=False))
        finally:
            event.remove(engine, "after_cursor_execute", record_update)
            db.rollback()
        # Prove the ordinary update reached PostgreSQL before the fence failed.
        assert any(10507 in parameters.values() for parameters in successful_updates)
        assert [snapshot(row) for row in db.query(Coupon).order_by(Coupon.id)] == before
        assert db.execute(text("SELECT audit FROM coupon_identity_retirements")).scalar_one() == audit_before

        assert asyncio.run(svc.sync_coupons(raw_list=deepcopy(batch), record_sync_meta=False)) == 2
        db.commit()
        assert db.get(Coupon, 10507).discount_value == "7"
        assert db.get(Coupon, 10505).discount_value == "2"
        assert snapshot(db.get(Coupon, 10506)) == retired_before
        assert db.execute(text("SELECT audit FROM coupon_identity_retirements")).scalar_one() == audit_before
        assert db.query(Coupon).count() == 3


def test_empty_retirement_migration_downgrades_and_reupgrades_real_postgresql(pg):
    """Removing an unused fence is supported; a populated fence stays permanent."""
    Session, migration = pg
    assert migration.revision == "0126" and migration.down_revision == "0124"
    with Session() as db:
        before = [snapshot(row) for row in db.query(Coupon).order_by(Coupon.id)]
        assert db.execute(text("SELECT count(*) FROM coupon_identity_retirements")).scalar_one() == 0
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
        db.commit()
        assert db.execute(text("SELECT to_regclass('coupon_identity_retirements')")).scalar_one() is None
        for function in ("guard_coupon_retirement", "guard_coupon_retirement_audit"):
            assert db.execute(text("SELECT to_regprocedure(:name)"), {"name": function + "()"}).scalar_one() is None
        assert db.execute(text("""SELECT count(*) FROM pg_trigger
            WHERE tgrelid = 'coupons'::regclass AND tgname = 'coupon_retirement_guard'""")).scalar_one() == 0
        assert [snapshot(row) for row in db.query(Coupon).order_by(Coupon.id)] == before
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.upgrade()
        db.commit()
        assert db.execute(text("SELECT count(*) FROM coupon_identity_retirements")).scalar_one() == 0
        assert db.execute(text("""SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
            AND tgname IN ('coupon_retirement_guard', 'coupon_retirement_audit_guard')
            AND tgrelid IN ('coupons'::regclass, 'coupon_identity_retirements'::regclass)""")).scalar_one() == 2
        # The reinstalled operations must really enforce the fence again.
        repair(db)
        db.commit()
        with pytest.raises(IntegrityError, match="coupon_retired"):
            db.execute(text("UPDATE coupons SET discount_value='9' WHERE id=10506"))
        db.rollback()
        assert db.get(Coupon, 10506).discount_value == "2"
