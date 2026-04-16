"""
tests/test_orders_upsert_flow.py
───────────────────────────────
Cover the order-ingest path end to end against SQLite, using the same
partial-unique index that migration 0023 installs on Postgres:

  • handle_order_webhook stores a new order row
  • a second webhook for the same external_id updates, never duplicates
  • missing external_id is rejected (not stored)
  • the DB-level unique index on (tenant_id, external_id) raises
    IntegrityError when bypassed, proving the race-proof protection is real

These tests intentionally skip customer intelligence by providing no
customer info — handle_order_webhook still commits the Order row but
early-exits the customer branch.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine, event, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Order, Tenant  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # Mirror migration 0023's partial unique index so dedup tests match prod.
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_tenant_external_id "
                "ON orders (tenant_id, external_id) "
                "WHERE external_id IS NOT NULL AND external_id != ''"
            )
        )
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Orders Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id, engine


def _payload(order_id: str, total: str = "150.00", status: str = "completed"):
    return {
        "id": order_id,
        "status": status,
        "total": total,
        # No customer info → handle_order_webhook skips intelligence work.
        "items": [],
    }


def test_handle_order_webhook_inserts_new_order():
    db, tenant_id, engine = _make_db()
    try:
        svc = StoreSyncService(db, tenant_id)
        asyncio.run(svc.handle_order_webhook(_payload("ext-42")))

        rows = db.query(Order).filter(Order.tenant_id == tenant_id).all()
        assert len(rows) == 1
        assert rows[0].external_id == "ext-42"
        assert rows[0].status == "completed"
    finally:
        db.close()
        engine.dispose()


def test_handle_order_webhook_is_idempotent_on_same_external_id():
    db, tenant_id, engine = _make_db()
    try:
        svc = StoreSyncService(db, tenant_id)
        asyncio.run(svc.handle_order_webhook(_payload("ext-42", total="150.00")))
        asyncio.run(svc.handle_order_webhook(_payload("ext-42", total="200.00", status="processing")))

        rows = db.query(Order).filter(Order.tenant_id == tenant_id).all()
        assert len(rows) == 1, "duplicate external_id must not create a second row"
        assert rows[0].total == "200.00"
        assert rows[0].status == "processing"
    finally:
        db.close()
        engine.dispose()


def test_handle_order_webhook_rejects_missing_external_id():
    db, tenant_id, engine = _make_db()
    try:
        svc = StoreSyncService(db, tenant_id)
        asyncio.run(svc.handle_order_webhook({"status": "completed", "total": "10"}))

        rows = db.query(Order).filter(Order.tenant_id == tenant_id).all()
        assert rows == [], "orders with no external_id must not be persisted"
    finally:
        db.close()
        engine.dispose()


def test_unique_index_blocks_direct_duplicate_insert():
    """
    Prove the unique index itself is doing the dedup work — not just the
    application-level check-before-insert in handle_order_webhook.
    """
    db, tenant_id, engine = _make_db()
    try:
        db.add(Order(
            tenant_id=tenant_id,
            external_id="race-1",
            status="completed",
            total="10.00",
            customer_info={},
            line_items=[],
        ))
        db.commit()

        db.add(Order(
            tenant_id=tenant_id,
            external_id="race-1",
            status="processing",
            total="10.00",
            customer_info={},
            line_items=[],
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        rows = db.query(Order).filter(Order.tenant_id == tenant_id).all()
        assert len(rows) == 1
    finally:
        db.close()
        engine.dispose()


def test_different_tenants_can_share_external_id():
    db, tenant_id, engine = _make_db()
    try:
        other = Tenant(name="Other Tenant", is_active=True)
        db.add(other)
        db.commit()

        db.add(Order(
            tenant_id=tenant_id, external_id="ext-1", status="completed",
            total="10", customer_info={}, line_items=[],
        ))
        db.add(Order(
            tenant_id=other.id, external_id="ext-1", status="completed",
            total="10", customer_info={}, line_items=[],
        ))
        db.commit()

        assert db.query(Order).count() == 2
    finally:
        db.close()
        engine.dispose()
