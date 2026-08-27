"""PostgreSQL concurrency tests for cart recovery emission (PR #888 H3/H5)."""
from __future__ import annotations

import sys
import threading
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import AutomationEvent, Customer, Order, Tenant
from services.cart_recovery_emitter import emit_cart_abandoned_if_new
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT = 991_303

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


def _new_session(engine):
    connection = engine.connect()
    trans = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    return session, connection, trans


def _seed_cart(engine):
    session, connection, trans = _new_session(engine)
    try:
        if session.get(Tenant, TEST_TENANT) is None:
            session.add(Tenant(id=TEST_TENANT, name="Cart Recovery PG"))
        customer = Customer(
            tenant_id=TEST_TENANT,
            phone="+966500700700",
            normalized_phone="966500700700",
            name="PG Shopper",
        )
        session.add(customer)
        session.flush()
        cart = Order(
            tenant_id=TEST_TENANT,
            external_id="cart-pg-1",
            status="abandoned",
            total="90",
            is_abandoned=True,
            customer_info={"mobile": "+966500700700"},
            extra_metadata={"abandoned_at": "2026-04-19T09:00:00"},
        )
        session.add(cart)
        session.commit()
        return customer.id, cart.id
    finally:
        trans.commit()
        session.close()
        connection.close()


def test_concurrent_emit_creates_single_event(postgres_engine, monkeypatch):
    customer_id, cart_id = _seed_cart(postgres_engine)
    results: list = []
    barrier = threading.Barrier(2)

    def _worker():
        session, connection, trans = _new_session(postgres_engine)
        try:
            cart = session.get(Order, cart_id)
            normalised = {
                "external_id": "cart-pg-1",
                "raw_cart_id": "pg-1",
                "customer_info": {"mobile": "+966500700700"},
                "customer_name": "PG Shopper",
                "abandoned_at": "2026-04-19T09:00:00",
            }
            barrier.wait(timeout=5)
            event_id = emit_cart_abandoned_if_new(
                session,
                tenant_id=TEST_TENANT,
                cart_row=cart,
                normalised=normalised,
                source="postgres_test",
                commit=True,
            )
            results.append(event_id)
        finally:
            try:
                trans.commit()
            except Exception:
                trans.rollback()
            session.close()
            connection.close()

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    verify_session, connection, trans = _new_session(postgres_engine)
    try:
        events = (
            verify_session.query(AutomationEvent)
            .filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned", customer_id=customer_id)
            .all()
        )
        assert len(events) == 1
        cart = verify_session.get(Order, cart_id)
        assert cart.extra_metadata.get("recovery_event_id") == events[0].id
        assert results.count(None) >= 1 or len(set(results)) == 1
    finally:
        trans.commit()
        verify_session.close()
        connection.close()


def test_poller_emit_visible_from_fresh_session(postgres_engine, monkeypatch):
    customer_id, cart_id = _seed_cart(postgres_engine)
    session, connection, trans = _new_session(postgres_engine)
    try:
        tenant = session.get(Tenant, TEST_TENANT)
        svc = __import__("services.store_sync", fromlist=["StoreSyncService"]).StoreSyncService(session, TEST_TENANT)
        raw = {
            "id": "pg-pol-2",
            "total": {"amount": 90, "currency": "SAR"},
            "checkout_url": "https://example/cart",
            "age_in_minutes": 15,
            "created_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
            "updated_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
            "customer": {"mobile": "+966500700700", "name": "PG Shopper"},
            "items": [{"name": "Shoe", "qty": 1}],
        }
        adapter = MagicMock()
        adapter.get_abandoned_carts = AsyncMock(return_value=[raw])
        svc._adapter = adapter
        import asyncio
        asyncio.run(svc.sync_abandoned_carts())
    finally:
        trans.commit()
        session.close()
        connection.close()

    verify_session, connection, trans = _new_session(postgres_engine)
    try:
        cart = verify_session.query(Order).filter_by(tenant_id=TEST_TENANT, external_id="cart-pg-pol-2").one()
        assert cart.extra_metadata.get("recovery_event_id")
        events = verify_session.query(AutomationEvent).filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned").all()
        assert any(e.id == cart.extra_metadata.get("recovery_event_id") for e in events)
    finally:
        trans.commit()
        verify_session.close()
        connection.close()
