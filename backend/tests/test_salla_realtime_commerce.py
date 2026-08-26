"""
backend/tests/test_salla_realtime_commerce.py
Regression tests for Salla near-real-time commerce sync.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine, event, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    AutomationEvent,
    Base,
    Customer,
    Integration,
    Order,
    Product,
    Promotion,
    Tenant,
    WebhookEvent,
)
from services.store_sync import StoreSyncService  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_tenant_external_id "
                "ON orders (tenant_id, external_id) "
                "WHERE external_id IS NOT NULL AND external_id != ''"
            )
        )
        conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event "
                "ON webhook_events (provider, external_event_id) "
                "WHERE external_event_id IS NOT NULL"
            )
        )
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant_a = Tenant(name="Tenant A", is_active=True)
    tenant_b = Tenant(name="Tenant B", is_active=True)
    session.add_all([tenant_a, tenant_b])
    session.commit()
    return session, tenant_a.id, tenant_b.id, engine


def _seed_integration(db, tenant_id: int, store_id: str) -> Integration:
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=store_id,
        config={"api_key": "test-key", "store_id": store_id},
        enabled=True,
    )
    db.add(intg)
    db.commit()
    return intg


def _cart_payload(cart_id: str = "77", phone: str = "+966500333444"):
    return {
        "id": cart_id,
        "total": {"amount": 120, "currency": "SAR"},
        "customer": {"name": "Generic Shopper", "mobile": phone},
        "items": [{"name": "Sports Shoe White", "quantity": 1}],
        "checkout_url": f"https://shop.example/cart/{cart_id}",
        "created_at": "2026-04-19 10:00:00",
        "abandoned_at": "2026-04-19 10:00:00",
    }


class TestOrderWebhookExpandedEvents:
  def test_order_status_updated_routes_and_is_idempotent(self):
      db, tenant_id, _, engine = _make_db()
      try:
          svc = StoreSyncService(db, tenant_id)
          payload = {"id": "ord-9001", "status": "processing", "total": "99.00", "items": []}
          _run(svc.handle_order_webhook(payload, webhook_event_type="order.status.updated"))
          _run(svc.handle_order_webhook({"id": "ord-9001", "status": "shipped", "total": "99.00", "items": []}, webhook_event_type="order.status.updated"))
          rows = db.query(Order).filter_by(tenant_id=tenant_id, external_id="ord-9001").all()
          assert len(rows) == 1
          assert rows[0].status == "shipped"
      finally:
          db.close(); engine.dispose()


class TestCustomerWebhookDedup:
  def test_customer_webhook_reuses_same_customer_row(self):
      db, tenant_id, _, engine = _make_db()
      try:
          svc = StoreSyncService(db, tenant_id)
          payload = {"id": "cust-1", "first_name": "Ahmed", "last_name": "Salem", "mobile": "+966500111222"}
          _run(svc.handle_customer_webhook(payload))
          _run(svc.handle_customer_webhook({**payload, "first_name": "Ahmed", "last_name": "Updated"}))
          customers = db.query(Customer).filter_by(tenant_id=tenant_id).all()
          assert len(customers) == 1
          assert customers[0].name
      finally:
          db.close(); engine.dispose()


class TestAbandonedCartLifecycle:
  @patch("core.automation_engine.emit_automation_event")
  def test_created_emits_once_updated_does_not_reemit(self, emit_mock):
      db, tenant_id, _, engine = _make_db()
      try:
          svc = StoreSyncService(db, tenant_id)
          payload = _cart_payload()
          emit_mock.return_value = SimpleNamespace(id=101)
          _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
          _run(svc.handle_abandoned_cart_webhook(payload, event_kind="updated", webhook_event_type="abandoned.cart.updated"))
          assert emit_mock.call_count == 1
          cart = db.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-77").one()
          assert cart.is_abandoned is True
      finally:
          db.close(); engine.dispose()

  def test_purchased_cancels_recovery(self):
      db, tenant_id, _, engine = _make_db()
      try:
          customer = Customer(tenant_id=tenant_id, phone="+966500333444", normalized_phone="966500333444", name="Shopper")
          db.add(customer); db.commit()
          svc = StoreSyncService(db, tenant_id)
          payload = _cart_payload()
          _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
          cart = db.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-77").one()
          cart.extra_metadata = {**(cart.extra_metadata or {}), "recovery_event_id": 999}
          db.commit()
          ev = AutomationEvent(
              tenant_id=tenant_id,
              event_type="cart_abandoned",
              customer_id=customer.id,
              payload={"cart_external_id": "cart-77", "cart_id": "77"},
              processed=False,
              created_at=datetime.utcnow(),
          )
          db.add(ev); db.commit()
          _run(svc.handle_abandoned_cart_webhook(payload, event_kind="purchased", webhook_event_type="abandoned.cart.purchased"))
          db.refresh(cart)
          assert cart.is_abandoned is False
          db.refresh(ev)
          assert ev.processed is True
      finally:
          db.close(); engine.dispose()


class TestOrderCancelsCartRecovery:
  @patch("services.customer_intelligence.CustomerIntelligenceService.find_customer_by_phone")
  @patch("services.customer_intelligence.CustomerIntelligenceService.upsert_customer_from_order")
  def test_order_with_cart_reference_cancels_recovery(self, upsert_mock, find_mock):
      db, tenant_id, _, engine = _make_db()
      try:
          customer = Customer(tenant_id=tenant_id, phone="+966500333444", normalized_phone="966500333444", name="Buyer")
          db.add(customer); db.commit()
          cart = Order(
              tenant_id=tenant_id,
              external_id="cart-55",
              status="abandoned",
              total="50",
              is_abandoned=True,
              customer_info={"mobile": "+966500333444"},
              extra_metadata={"recovery_event_id": 42},
          )
          db.add(cart)
          ev = AutomationEvent(
              tenant_id=tenant_id,
              event_type="cart_abandoned",
              customer_id=customer.id,
              payload={"cart_external_id": "cart-55", "cart_id": "55"},
              processed=False,
              created_at=datetime.utcnow(),
          )
          db.add(ev); db.commit()
          upsert_mock.return_value = customer
          find_mock.return_value = customer
          svc = StoreSyncService(db, tenant_id)
          order_payload = {
              "id": "ord-55",
              "status": "completed",
              "total": "50",
              "cart_reference_id": "55",
              "customer": {"mobile": "+966500333444", "name": "Buyer"},
              "items": [],
          }
          _run(svc.handle_order_webhook(order_payload, webhook_event_type="order.created"))
          db.refresh(cart); db.refresh(ev)
          assert cart.is_abandoned is False
          assert ev.processed is True
      finally:
          db.close(); engine.dispose()


class TestProductPartialWebhook:
  def test_partial_product_event_fetches_full_product(self):
      db, tenant_id, _, engine = _make_db()
      try:
          existing = Product(
              tenant_id=tenant_id,
              external_id="prod-9",
              title="Old Title",
              price="10",
              extra_metadata={"title": "Old Title"},
          )
          db.add(existing); db.commit()
          svc = StoreSyncService(db, tenant_id)
          adapter = MagicMock()
          adapter.platform = "salla"
          adapter.get_product = AsyncMock(return_value={
              "id": "prod-9",
              "title": "Updated Athletic Shoe",
              "price": "149",
              "in_stock": True,
          })
          svc._adapter = adapter
          _run(svc.handle_product_webhook({"id": "prod-9", "price": "149"}, webhook_event_type="product.price.updated"))
          db.refresh(existing)
          assert existing.title == "Updated Athletic Shoe"
          adapter.get_product.assert_awaited_once_with("prod-9")
      finally:
          db.close(); engine.dispose()


class TestSpecialOfferUpsert:
  def test_special_offer_webhook_is_idempotent(self):
      db, tenant_id, _, engine = _make_db()
      try:
          svc = StoreSyncService(db, tenant_id)
          payload = {"id": "offer-1", "name": "Spring Sale", "type": "percentage", "percent": 15, "status": "active"}
          _run(svc.handle_special_offer_webhook(payload, webhook_event_type="specialoffer.created"))
          _run(svc.handle_special_offer_webhook({**payload, "percent": 20}, webhook_event_type="specialoffer.updated"))
          promos = db.query(Promotion).filter_by(tenant_id=tenant_id).all()
          assert len(promos) == 1
          assert promos[0].extra_metadata.get("salla_offer_id") == "offer-1"
          assert float(promos[0].discount_value) == 20.0
          assert promos[0].extra_metadata.get("nahla_to_salla_supported") is False
      finally:
          db.close(); engine.dispose()


class TestTenantIsolation:
  def test_abandoned_cart_rows_are_tenant_scoped(self):
      db, tenant_a, tenant_b, engine = _make_db()
      try:
          svc_a = StoreSyncService(db, tenant_a)
          svc_b = StoreSyncService(db, tenant_b)
          _run(svc_a.handle_abandoned_cart_webhook(_cart_payload("10"), event_kind="created", webhook_event_type="abandoned.cart"))
          _run(svc_b.handle_abandoned_cart_webhook(_cart_payload("10"), event_kind="created", webhook_event_type="abandoned.cart"))
          assert db.query(Order).filter_by(tenant_id=tenant_a, external_id="cart-10").count() == 1
          assert db.query(Order).filter_by(tenant_id=tenant_b, external_id="cart-10").count() == 1
      finally:
          db.close(); engine.dispose()


class TestWebhookFastAckPattern:
  @patch("services.outcome_tracker.record_order_outcome")
  def test_dispatcher_marks_processed_without_duplicate_order(self, _mock_outcome):
      from core.webhook_dispatcher import _process_event
      from core.webhook_events import claim_next_batch, persist_event
      from commerce_scenario_fixtures import make_scenario_db, seed_tenant

      db, _engine = make_scenario_db()
      tenant = seed_tenant(db, name="Generic Commerce Store")
      intg = Integration(
          tenant_id=tenant.id,
          provider="salla",
          external_store_id="STORE-RT-1",
          config={"api_key": "k", "store_id": "STORE-RT-1"},
          enabled=True,
      )
      db.add(intg); db.commit()
      order_data = {
          "id": 7001,
          "reference_id": "ORD-7001",
          "status": "under_review",
          "total": {"amount": 100, "currency": "SAR"},
          "items": [],
      }
      parsed = {"event": "order.payment.updated", "merchant": "STORE-RT-1", "data": order_data}
      ev = persist_event(
          db,
          provider="salla",
          raw_body='{"event":"order.payment.updated"}',
          parsed_payload=parsed,
          event_type="order.payment.updated",
          external_event_id="evt-7001",
          store_id="STORE-RT-1",
      )
      assert ev.status == "received"
      batch = claim_next_batch(db, limit=1)
      _run(_process_event(db, batch[0]))
      db.refresh(ev)
      assert ev.status == "processed"
      assert db.query(Order).filter_by(tenant_id=tenant.id, external_id="7001").count() == 1
