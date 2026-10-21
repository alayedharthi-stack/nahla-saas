"""Real PostgreSQL row-lock proof; uses a disposable schema, fake Salla only."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "database")]
from database.models import Coupon
from services.coupon_active_sync import dispatch_active_change, queue_active_change


def test_two_workers_dispatch_only_once_under_real_row_lock():
    url = os.getenv("A1_PG_TEST_DATABASE_URL")
    if not url:
        if os.getenv("A1_PG_INTEGRATION_REQUIRED") == "1":
            pytest.fail("PostgreSQL test URL required")
        pytest.skip("Set A1_PG_TEST_DATABASE_URL for real PostgreSQL proof")
    schema = "coupon_status_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'''CREATE TABLE "{schema}".coupons (
          id SERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, code TEXT NOT NULL,
          description TEXT, discount_type TEXT, discount_value TEXT, metadata JSONB,
          expires_at TIMESTAMP, source_type TEXT NOT NULL DEFAULT 'manual',
          coupon_level TEXT, allocation_channel TEXT, UNIQUE(tenant_id, code))'''))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    integration = SimpleNamespace(id=22, tenant_id=1, external_store_id="202", provider="salla", enabled=True,
                                  config={"store_id": "202", "api_sync_enabled": True, "refresh_token": "fake"})
    try:
        with Session() as db:
            row = Coupon(tenant_id=1, code="GENERIC-STATUS", extra_metadata={"active": True, "salla_coupon_id": "101",
                         "salla_identity": {"tenant_id": 1, "integration_id": 22, "store_id": "202"}})
            db.add(row)
            db.flush()
            queue_active_change(row, integration, False)
            db.commit()
            coupon_id = row.id

        async def race():
            entered, release = asyncio.Event(), asyncio.Event()
            class Adapter:
                _tenant_id, _integration_id, store_id = 1, 22, "202"
                active, puts, reads = True, 0, 0
                async def get_coupon_by_id(self, _):
                    self.reads += 1
                    if self.reads == 1:
                        entered.set()
                        await release.wait()
                    return {"id": 101, "code": "GENERIC-STATUS", "status": "active" if self.active else "inactive"}
                async def set_coupon_active(self, _, active):
                    self.puts += 1
                    self.active = active
            adapter = Adapter()
            with Session() as first, Session() as second:
                task = asyncio.create_task(dispatch_active_change(first, 1, coupon_id, integration, adapter))
                await asyncio.wait_for(entered.wait(), timeout=5)
                await asyncio.wait_for(dispatch_active_change(second, 1, coupon_id, integration, adapter), timeout=5)
                assert adapter.reads == 1
                release.set()
                await asyncio.wait_for(task, timeout=5)
            assert adapter.puts == 1
            with Session() as db:
                row = db.query(Coupon).filter_by(id=coupon_id).one()
                assert row.extra_metadata["active"] is False
                assert row.extra_metadata["sync_status"] == "synced"
                assert "active_sync_pending" not in row.extra_metadata
        asyncio.run(race())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
