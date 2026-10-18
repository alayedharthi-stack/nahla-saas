"""Real PostgreSQL import serialization; disposable schema, provider fake only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "database")]
from database.models import Coupon
from services.store_sync import StoreSyncService


def test_concurrent_case_variant_imports_keep_one_provider_identity():
    url = os.getenv("A1_PG_TEST_DATABASE_URL")
    if not url:
        if os.getenv("A1_PG_INTEGRATION_REQUIRED") == "1":
            pytest.fail("PostgreSQL proof required")
        pytest.skip("Set A1_PG_TEST_DATABASE_URL for actual PostgreSQL proof")
    schema = "coupon_identity_" + uuid4().hex
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
        conn.execute(text(f'INSERT INTO "{schema}".tenants VALUES (1)'))
        conn.execute(text(f'''INSERT INTO "{schema}".integrations
            VALUES (22,1,'salla',true,'202','{{"store_id":"202"}}')'''))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    class Provider:
        _tenant_id, _integration_id, store_id = 1, 22, "202"
    def apply(db, code):
        svc = StoreSyncService(db, 1, integration_connection_id=22, adapter=Provider())
        return asyncio.run(svc.sync_coupons(raw_list=[dict(id=101,code=code,status="inactive",amount=2)], record_sync_meta=False))
    entered = Event()
    try:
        with Session() as first, ThreadPoolExecutor(max_workers=1) as executor:
            assert apply(first, "SHOES123") == 1  # tenant lock held until commit
            def second_import():
                with Session() as second:
                    entered.set()
                    result = apply(second, "shoes123")
                    second.commit()
                    return result
            future = executor.submit(second_import)
            assert entered.wait(5)
            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)  # second importer must await lock
            finally:
                first.commit()
            assert future.result(timeout=5) == 1
        with Session() as db:
            row = db.query(Coupon).one()
            assert row.code == "SHOES123" and row.extra_metadata["salla_coupon_id"] == "101"
            assert row.extra_metadata["salla_identity"]["store_id"] == "202"
            assert row.extra_metadata["active"] is False
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
