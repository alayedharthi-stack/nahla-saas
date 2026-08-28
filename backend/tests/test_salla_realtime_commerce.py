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
        "abandoned_at": {
            "date": "2026-04-19 12:00:00.000000",
            "timezone_type": 3,
            "timezone": "Asia/Riyadh",
        },
    }


class TestOrderWebhookExpandedEvents:
  def test_order_status_updated_routes_and_is_idempotent(self):
      db, tenant_id, _, engine = _make_db()
      try:
          svc = StoreSyncService(db, tenant_id)
          svc.db.add(Order(
              tenant_id=tenant_id,
              external_id="9001",
              status="pending",
              total="150.00",
              line_items=[{"name": "Sports Shoe White", "qty": 1}],
              customer_info={"mobile": "+966500111222"},
          ))
          svc.db.commit()
          status_payload = {
              "id": 555001,
              "status": "processing-label",
              "order": {
                  "id": 9001,
                  "status": {"slug": "processing", "name": "Processing"},
                  "reference_id": "ORD-9001",
                  "total": {"amount": 150},
              },
          }
          _run(svc.handle_order_webhook(status_payload, webhook_event_type="order.status.updated"))
          _run(svc.handle_order_webhook({
              "id": 555002,
              "status": "shipped-label",
              "order": {"id": 9001, "status": {"slug": "shipped", "name": "Shipped"}},
          }, webhook_event_type="order.status.updated"))
          rows = db.query(Order).filter_by(tenant_id=tenant_id, external_id="9001").all()
          assert len(rows) == 1
          assert rows[0].status == "shipped"
          assert rows[0].total == "150"
          assert rows[0].line_items
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
          payload = {
              "id": "offer-1",
              "name": "Spring Sale",
              "message": "Spring Sale",
              "offer_type": "discount",
              "status": "active",
              "get": {"discount_type": "percentage", "discount_amount": "15"},
          }
          _run(svc.handle_special_offer_webhook(payload, webhook_event_type="specialoffer.created"))
          updated = dict(payload)
          updated["get"] = {"discount_type": "percentage", "discount_amount": "20"}
          _run(svc.handle_special_offer_webhook(updated, webhook_event_type="specialoffer.updated"))
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

class TestSallaDatetimeParsing:
    def test_riyadh_nested_timestamp_converts_to_utc_anchor(self):
        from services.salla_datetime import parse_salla_datetime_to_utc, salla_datetime_to_naive_utc

        nested = {
            "date": "2026-04-19 12:00:00.000000",
            "timezone_type": 3,
            "timezone": "Asia/Riyadh",
        }
        aware = parse_salla_datetime_to_utc(nested)
        assert aware is not None
        assert aware.hour == 9 and aware.minute == 0
        naive = salla_datetime_to_naive_utc(nested)
        assert naive == aware.replace(tzinfo=None)

    def test_explicit_offset_honored(self):
        from services.salla_datetime import parse_salla_datetime_to_utc

        dt = parse_salla_datetime_to_utc("2026-04-19T12:00:00+02:00")
        assert dt is not None
        assert dt.hour == 10

    def test_invalid_input_returns_none(self):
        from services.salla_datetime import salla_datetime_to_naive_utc

        assert salla_datetime_to_naive_utc({"date": "not-a-date", "timezone": "Asia/Riyadh"}) is None


class TestAbandonedCartDelayContract:
    @patch("core.automation_engine.emit_automation_event")
    def test_emitter_stores_anchor_and_engine_defers_until_plus_30(self, emit_mock):
        from core.automation_engine import _try_execute
        from services.cart_recovery_emitter import emit_cart_abandoned_if_new

        db, tenant_id, _, engine = _make_db()
        try:
            customer = Customer(
                tenant_id=tenant_id,
                phone="+966500333444",
                normalized_phone="966500333444",
                name="Shopper",
            )
            db.add(customer)
            db.commit()
            payload = {
                "id": "88",
                "total": {"amount": 120, "currency": "SAR"},
                "customer": {"name": "Generic Shopper", "mobile": "+966500333444"},
                "items": [{"name": "Sports Shoe White", "quantity": 1}],
                "checkout_url": "https://shop.example/cart/88",
                "created_at": {
                    "date": "2026-04-19 12:00:00.000000",
                    "timezone_type": 3,
                    "timezone": "Asia/Riyadh",
                },
                "abandoned_at": {
                    "date": "2026-04-19 12:00:00.000000",
                    "timezone_type": 3,
                    "timezone": "Asia/Riyadh",
                },
            }
            svc = StoreSyncService(db, tenant_id)
            emit_mock.return_value = SimpleNamespace(id=501)
            _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
            cart = db.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-88").one()
            assert cart.extra_metadata.get("abandoned_at", "").startswith("2026-04-19T09:00:00")
            emit_mock.assert_called_once()
            created_at = emit_mock.call_args.kwargs.get("created_at")
            assert created_at.hour == 9 and created_at.minute == 0
            ev = AutomationEvent(
                tenant_id=tenant_id,
                event_type="cart_abandoned",
                customer_id=customer.id,
                payload={"cart_external_id": "cart-88"},
                processed=False,
                created_at=created_at,
            )
            db.add(ev)
            automation = SimpleNamespace(id=9, automation_type="abandoned_cart", config={"steps": [{"delay_minutes": 30}]}, enabled=True)
            now_before = datetime(2026, 4, 19, 9, 15, 0)
            assert _run(_try_execute(db, tenant_id, ev, automation, now_before)) == "delay"
            now_due = datetime(2026, 4, 19, 9, 31, 0)
            with patch("core.automation_engine._evaluate_conditions", return_value=(False, "test_skip")):
                assert _run(_try_execute(db, tenant_id, ev, automation, now_due)) == "skipped"
        finally:
            db.close()
            engine.dispose()


class TestPartialCartPreservation:
    def test_partial_status_payload_preserves_line_items_and_customer(self):
        db, tenant_id, _, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            full = _cart_payload("21")
            _run(svc.handle_abandoned_cart_webhook(full, event_kind="created", webhook_event_type="abandoned.cart"))
            partial = {"id": "21", "status": "active"}
            _run(svc.handle_abandoned_cart_webhook(partial, event_kind="status_changed", webhook_event_type="abandoned.cart.status.changed"))
            cart = db.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-21").one()
            assert cart.line_items
            assert cart.customer_info.get("mobile")
            assert cart.total == "120"
        finally:
            db.close()
            engine.dispose()

    def test_non_terminal_status_does_not_cancel_recovery(self):
        db, tenant_id, _, engine = _make_db()
        try:
            customer = Customer(tenant_id=tenant_id, phone="+966500333444", normalized_phone="966500333444", name="Shopper")
            db.add(customer)
            db.commit()
            svc = StoreSyncService(db, tenant_id)
            _run(svc.handle_abandoned_cart_webhook(_cart_payload("22"), event_kind="created", webhook_event_type="abandoned.cart"))
            ev = AutomationEvent(
                tenant_id=tenant_id,
                event_type="cart_abandoned",
                customer_id=customer.id,
                payload={"cart_external_id": "cart-22", "cart_id": "22"},
                processed=False,
                created_at=datetime.utcnow(),
            )
            db.add(ev)
            db.commit()
            _run(svc.handle_abandoned_cart_webhook({"id": "22", "status": "active"}, event_kind="status_changed", webhook_event_type="abandoned.cart.status.changed"))
            db.refresh(ev)
            assert ev.processed is False
        finally:
            db.close()
            engine.dispose()

    def test_replayed_purchase_cancels_once(self):
        db, tenant_id, _, engine = _make_db()
        try:
            customer = Customer(tenant_id=tenant_id, phone="+966500333444", normalized_phone="966500333444", name="Shopper")
            db.add(customer)
            db.commit()
            svc = StoreSyncService(db, tenant_id)
            payload = _cart_payload("23")
            _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
            ev = AutomationEvent(
                tenant_id=tenant_id,
                event_type="cart_abandoned",
                customer_id=customer.id,
                payload={"cart_external_id": "cart-23", "cart_id": "23"},
                processed=False,
                created_at=datetime.utcnow(),
            )
            db.add(ev)
            db.commit()
            purchased = {"id": "23", "status": "purchased"}
            _run(svc.handle_abandoned_cart_webhook(purchased, event_kind="purchased", webhook_event_type="abandoned.cart.purchased"))
            _run(svc.handle_abandoned_cart_webhook(purchased, event_kind="purchased", webhook_event_type="abandoned.cart.purchased"))
            db.refresh(ev)
            assert ev.processed is True
            cart = db.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-23").one()
            assert cart.line_items
        finally:
            db.close()
            engine.dispose()


class TestEventRegistryContract:
    def test_activation_checklist_excludes_compat_and_app_functions_only(self):
        from services.salla_realtime_events import (
            SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST,
            SALLA_WEBHOOK_COMPATIBILITY_ALIASES,
            event_registry_contract,
        )

        contract = event_registry_contract()
        checklist = set(SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST)
        assert "cart.abandoned" not in checklist
        assert "abandoned_cart" not in checklist
        assert "abandoned.cart.updated" not in checklist
        assert "customer.login" in checklist
        assert set(contract["compatibility_aliases"]) == set(SALLA_WEBHOOK_COMPATIBILITY_ALIASES)
        active = set(contract["merchant_active"])
        deprecated = set(contract["merchant_deprecated"])
        app_only = set(contract["app_functions_only"])
        aliases = set(contract["compatibility_aliases"])
        assert active.isdisjoint(deprecated)
        assert active.isdisjoint(app_only)
        assert active.isdisjoint(aliases)
        assert "product.updated" in deprecated
        assert "abandoned.cart.updated" in app_only


class TestRealtimeCommerceDiagnostics:
    def test_diag_hashes_store_and_strips_raw_errors(self):
        from services.salla_commerce_reconciler import _state as reconciler_state
        from services.salla_realtime_observability import build_realtime_commerce_diag

        db, tenant_id, _, engine = _make_db()
        try:
            _seed_integration(db, tenant_id, "STORE-DIAG-1")
            reconciler_state["tenants"][tenant_id] = {
                "tenant_hash": "abc",
                "integration_id": 1,
                "store_hash": "def",
                "result": "error",
                "error_code": "RuntimeError",
                "error": "secret token=leak",
            }
            diag = build_realtime_commerce_diag(db, tenant_id)
            assert diag["integration"]["store_hash"]
            assert "store_id" not in diag["integration"]
            tenant = diag["reconciler"]["tenant"]
            assert tenant["error_code"] == "RuntimeError"
            assert "error" not in tenant
            assert "secret" not in str(diag)
        finally:
            reconciler_state["tenants"].pop(tenant_id, None)
            db.close()
            engine.dispose()

    def test_admin_diag_route_requires_admin_dependency(self):
        from routers.admin import router as admin_router

        def _dep_callable_names(route):
            names = set()
            deps = list(getattr(route, "dependant", None).dependencies) if getattr(route, "dependant", None) else []
            for dep in deps:
                call = getattr(dep, "call", None)
                if call is not None:
                    names.add(getattr(call, "__name__", repr(call)))
            return names

        route = next(
            r for r in admin_router.routes
            if getattr(r, "path", "") == "/admin/salla/realtime-commerce/diag"
        )
        assert "require_admin" in _dep_callable_names(route)
