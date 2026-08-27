"""PostgreSQL concurrency tests for cart recovery emission (PR #888 H3/H5)."""
from __future__ import annotations

import sys
import threading
import uuid
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

pytestmark = [
    pytest.mark.usefixtures("postgres_engine"),
    pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning"),
]


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


def test_poller_emit_visible_from_fresh_session(postgres_engine):
  import asyncio
  from services.store_sync import StoreSyncService

  cart_token = f"pgpol{uuid.uuid4().hex[:8]}"
  external_id = f"cart-{cart_token}"
  Session = sessionmaker(bind=postgres_engine)
  session = Session()
  try:
    if session.get(Tenant, TEST_TENANT) is None:
      session.add(Tenant(id=TEST_TENANT, name="Cart Recovery PG"))
      session.commit()
    svc = StoreSyncService(session, TEST_TENANT)
    raw = {
      "id": cart_token,
      "total": {"amount": 90, "currency": "SAR"},
      "checkout_url": "https://example/cart",
      "age_in_minutes": 15,
      "abandoned_at": "2026-04-19T09:00:00",
      "created_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
      "updated_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
      "customer": {"mobile": "+966500700700", "name": "PG Shopper"},
      "items": [{"name": "Shoe", "qty": 1}],
    }
    adapter = MagicMock()
    adapter.get_abandoned_carts = AsyncMock(return_value=[raw])
    svc._adapter = adapter
    asyncio.run(svc.sync_abandoned_carts())
  finally:
    session.close()

  verify_session = Session()
  try:
    cart = verify_session.query(Order).filter_by(tenant_id=TEST_TENANT, external_id=external_id).one()
    assert cart.extra_metadata.get("recovery_event_id")
    events = verify_session.query(AutomationEvent).filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned").all()
    assert any(e.id == cart.extra_metadata.get("recovery_event_id") for e in events)
  finally:
    verify_session.close()


def test_purchase_blocks_emit(postgres_engine):
    import asyncio
    from services.cart_recovery_emitter import emit_cart_abandoned_if_new
    from services.store_sync import StoreSyncService

    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        if session.get(Tenant, TEST_TENANT) is None:
            session.add(Tenant(id=TEST_TENANT, name="Cart Recovery PG"))
            session.commit()
        cart = Order(
            tenant_id=TEST_TENANT,
            external_id="cart-pg-purchased",
            status="purchased",
            total="50",
            is_abandoned=False,
            customer_info={"mobile": "+966500700700"},
            extra_metadata={"abandoned_at": "2026-04-19T09:00:00", "cart_status": "purchased"},
        )
        session.add(cart)
        session.commit()
        normalised = {
            "external_id": "cart-pg-purchased",
            "raw_cart_id": "pg-purchased",
            "customer_info": {"mobile": "+966500700700"},
            "abandoned_at": "2026-04-19T09:00:00",
        }
        event_id = emit_cart_abandoned_if_new(
            session, tenant_id=TEST_TENANT, cart_row=cart, normalised=normalised, commit=True,
        )
        assert event_id is None
        events = session.query(AutomationEvent).filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned").all()
        assert not any((e.payload or {}).get("cart_external_id") == "cart-pg-purchased" for e in events)
    finally:
        session.close()


def test_webhook_and_poller_emit_single_event(postgres_engine):
    import asyncio
    import threading
    from services.store_sync import StoreSyncService

    cart_token = f"pgboth{uuid.uuid4().hex[:8]}"
    external_id = f"cart-{cart_token}"
    raw = {
        "id": cart_token,
        "total": {"amount": 90, "currency": "SAR"},
        "checkout_url": "https://example/cart",
        "age_in_minutes": 20,
        "abandoned_at": "2026-04-19T09:00:00",
        "created_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
        "updated_at": {"date": "2026-04-19 09:00:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
        "customer": {"mobile": "+966500711711", "name": "PG Shopper"},
        "items": [{"name": "Shoe", "qty": 1}],
    }
    Session = sessionmaker(bind=postgres_engine)
    if Session().get(Tenant, TEST_TENANT) is None:
        s = Session(); s.add(Tenant(id=TEST_TENANT, name="Cart Recovery PG")); s.commit(); s.close()
    barrier = threading.Barrier(2)

    errors = {}

    def _webhook():
        session = Session()
        try:
            svc = StoreSyncService(session, TEST_TENANT)
            barrier.wait(timeout=5)
            asyncio.run(svc.handle_abandoned_cart_webhook(raw, event_kind="created", webhook_event_type="abandoned.cart"))
            errors["webhook"] = {"ok": True}
        except Exception as exc:
            errors["webhook"] = {"ok": False, "exc": exc}
        finally:
            session.close()

    def _poller():
        session = Session()
        try:
            svc = StoreSyncService(session, TEST_TENANT)
            adapter = MagicMock()
            adapter.get_abandoned_carts = AsyncMock(return_value=[raw])
            svc._adapter = adapter
            barrier.wait(timeout=5)
            asyncio.run(svc.sync_abandoned_carts())
            errors["poller"] = {"ok": True}
        except Exception as exc:
            errors["poller"] = {"ok": False, "exc": exc}
        finally:
            session.close()

    t1 = threading.Thread(target=_webhook)
    t2 = threading.Thread(target=_poller)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert errors.get("webhook", {}).get("ok") is True, errors.get("webhook")
    assert errors.get("poller", {}).get("ok") is True, errors.get("poller")

    verify = Session()
    try:
        cart = verify.query(Order).filter_by(tenant_id=TEST_TENANT, external_id=external_id).one()
        marker = cart.extra_metadata.get("recovery_event_id")
        assert marker
        events = verify.query(AutomationEvent).filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned").all()
        matched = [e for e in events if (e.payload or {}).get("cart_external_id") == external_id]
        assert len(matched) == 1
        assert matched[0].id == marker
    finally:
        verify.close()

def test_marker_failure_rolls_back_event_then_retry_succeeds(postgres_engine):
    """H3: event+marker atomicity — commit failure rolls back event; retry succeeds."""
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        if session.get(Tenant, TEST_TENANT) is None:
            session.add(Tenant(id=TEST_TENANT, name="Cart Recovery PG"))
            session.commit()
        customer = Customer(
            tenant_id=TEST_TENANT,
            phone="+966500700722",
            normalized_phone="966500700722",
            name="PG Shopper Rollback",
        )
        session.add(customer)
        session.flush()
        customer_id = customer.id
        cart_token = f"pgroll{uuid.uuid4().hex[:8]}"
        external_id = f"cart-{cart_token}"
        cart = Order(
            tenant_id=TEST_TENANT,
            external_id=external_id,
            status="abandoned",
            total="90",
            is_abandoned=True,
            customer_info={"mobile": "+966500700722"},
            extra_metadata={"abandoned_at": "2026-04-19T09:00:00"},
        )
        session.add(cart)
        session.commit()
        cart_id = cart.id
    finally:
        session.close()

    normalised = {
        "external_id": external_id,
        "raw_cart_id": cart_token,
        "customer_info": {"mobile": "+966500700722"},
        "customer_name": "PG Shopper Rollback",
        "abandoned_at": "2026-04-19T09:00:00",
    }

    fail_session = Session()
    commit_calls = {"n": 0}
    real_commit = fail_session.commit

    def _flaky_commit():
        commit_calls["n"] += 1
        if commit_calls["n"] == 1:
            raise RuntimeError("simulated marker persist failure")
        return real_commit()

    fail_session.commit = _flaky_commit  # type: ignore[method-assign]
    try:
        cart = fail_session.get(Order, cart_id)
        first = emit_cart_abandoned_if_new(
            fail_session,
            tenant_id=TEST_TENANT,
            cart_row=cart,
            normalised=normalised,
            source="postgres_test",
            commit=True,
        )
        assert first is None
    finally:
        fail_session.close()

    verify = Session()
    try:
        cart = verify.get(Order, cart_id)
        assert not cart.extra_metadata.get("recovery_event_id")
        events = (
            verify.query(AutomationEvent)
            .filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned", customer_id=customer_id)
            .all()
        )
        assert events == []
    finally:
        verify.close()

    retry = Session()
    try:
        cart = retry.get(Order, cart_id)
        second = emit_cart_abandoned_if_new(
            retry,
            tenant_id=TEST_TENANT,
            cart_row=cart,
            normalised=normalised,
            source="postgres_test",
            commit=True,
        )
        assert second is not None
    finally:
        retry.close()

    final = Session()
    try:
        cart = final.get(Order, cart_id)
        marker = cart.extra_metadata.get("recovery_event_id")
        assert marker == second
        events = (
            final.query(AutomationEvent)
            .filter_by(tenant_id=TEST_TENANT, event_type="cart_abandoned", customer_id=customer_id)
            .all()
        )
        assert len(events) == 1
        assert events[0].id == marker
    finally:
        final.close()

