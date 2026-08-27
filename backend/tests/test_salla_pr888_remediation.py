"""PR #888 consolidated Sol remediation regression tests."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
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
    StoreKnowledgeSnapshot,
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
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Generic Store", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id, engine


def _riyadh_abandoned():
    return {
        "date": "2026-04-19 12:00:00.000000",
        "timezone_type": 3,
        "timezone": "Asia/Riyadh",
    }


class TestH2PaginationWatermark:
    def test_products_partial_page_does_not_advance_watermark(self):
        db, tenant_id, engine = _make_db()
        try:
            snap = StoreKnowledgeSnapshot(tenant_id=tenant_id, policy_summary={})
            db.add(snap)
            db.commit()
            svc = StoreSyncService(db, tenant_id)
            adapter = MagicMock()
            adapter.platform = "salla"
            from store_adapters.salla_pagination import SallaPaginatedFetchIncomplete

            adapter.get_products = AsyncMock(side_effect=SallaPaginatedFetchIncomplete(
                partial=True, items=[{"id": "p1", "name": "Shirt", "price": {"amount": 10}}], pages_fetched=1,
            ))
            svc._adapter = adapter
            result = _run(svc.sync_products(incremental=True, strict=False))
            assert result == 0
            wm = svc._commerce_reconcile_watermarks()["products"]
            assert wm == ""
        finally:
            db.close(); engine.dispose()

    def test_customers_row_failure_blocks_watermark(self):
        db, tenant_id, engine = _make_db()
        try:
            snap = StoreKnowledgeSnapshot(tenant_id=tenant_id, policy_summary={})
            db.add(snap)
            db.commit()
            svc = StoreSyncService(db, tenant_id)
            adapter = MagicMock()
            adapter.platform = "salla"
            adapter.get_customers = AsyncMock(return_value=[{"id": "c1"}, {"id": "bad"}])
            svc._adapter = adapter

            def _upsert(raw, **kwargs):
                if raw.get("id") == "bad":
                    raise ValueError("row failed")
                return "created"

            with patch.object(svc, "_upsert_legacy_customer_from_salla_sync_payload", side_effect=_upsert):
                with patch.object(svc, "_upsert_a1_external_profile_side_effect", return_value=None):
                    _run(svc.sync_customers(incremental=True, strict=False))
            assert svc._commerce_reconcile_watermarks()["customers"] == ""
        finally:
            db.close(); engine.dispose()


class TestH4H5CartLifecycle:
    def test_missing_abandonment_anchor_skips_emit(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            payload = {
                "id": "99",
                "total": {"amount": 50, "currency": "SAR"},
                "customer": {"mobile": "+966500900900"},
                "items": [],
                "created_at": "2026-04-19 10:00:00",
            }
            with patch("core.automation_engine.emit_automation_event") as emit_mock:
                _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
                emit_mock.assert_not_called()
            cart = db.query(Order).filter_by(external_id="cart-99").one()
            assert "abandoned_at" not in (cart.extra_metadata or {})
        finally:
            db.close(); engine.dispose()

    def test_purchased_then_created_does_not_reopen(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            base = {
                "id": "44",
                "total": {"amount": 80, "currency": "SAR"},
                "customer": {"mobile": "+966500800800"},
                "items": [{"name": "Perfume", "qty": 1}],
                "abandoned_at": _riyadh_abandoned(),
            }
            _run(svc.handle_abandoned_cart_webhook(base, event_kind="created", webhook_event_type="abandoned.cart"))
            _run(svc.handle_abandoned_cart_webhook({**base, "status": "purchased"}, event_kind="purchased", webhook_event_type="abandoned.cart.purchased"))
            _run(svc.handle_abandoned_cart_webhook(base, event_kind="created", webhook_event_type="abandoned.cart"))
            cart = db.query(Order).filter_by(external_id="cart-44").one()
            assert cart.is_abandoned is False
            assert cart.line_items
        finally:
            db.close(); engine.dispose()


class TestH6CartScopedCancel:
    def test_cancel_scoped_to_matching_cart_only(self):
        from services.cart_recovery_cancel import cancel_recovery_for_customer

        db, tenant_id, engine = _make_db()
        try:
            customer = Customer(tenant_id=tenant_id, phone="+966500111000", normalized_phone="966500111000", name="Buyer")
            db.add(customer)
            ev_a = AutomationEvent(
                tenant_id=tenant_id,
                customer_id=1,
                event_type="cart_abandoned",
                payload={"cart_external_id": "cart-A", "cart_id": "A"},
                processed=False,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            ev_b = AutomationEvent(
                tenant_id=tenant_id,
                customer_id=1,
                event_type="cart_abandoned",
                payload={"cart_external_id": "cart-B", "cart_id": "B"},
                processed=False,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add_all([customer, ev_a, ev_b])
            db.commit()
            db.refresh(customer)
            ev_a.customer_id = customer.id
            ev_b.customer_id = customer.id
            db.commit()
            cancel_recovery_for_customer(
                db,
                tenant_id=tenant_id,
                customer_id=customer.id,
                matched_cart_external_id="cart-A",
                reason="customer_purchased",
            )
            db.refresh(ev_a); db.refresh(ev_b)
            assert ev_a.processed is True
            assert ev_b.processed is False
        finally:
            db.close(); engine.dispose()

    def test_unknown_order_status_is_not_purchase(self):
        from services.cart_recovery_cancel import order_is_a_purchase

        assert order_is_a_purchase("mystery_status") is False
        assert order_is_a_purchase("completed") is True


class TestH7H8ProductWebhook:
    def test_partial_fetch_failure_preserves_product(self):
        db, tenant_id, engine = _make_db()
        try:
            product = Product(tenant_id=tenant_id, external_id="p1", title="Rich Product", price="99", extra_metadata={})
            db.add(product); db.commit()
            svc = StoreSyncService(db, tenant_id)
            adapter = MagicMock(platform="salla")
            adapter.get_product = AsyncMock(side_effect=RuntimeError("http down"))
            svc._adapter = adapter
            with pytest.raises(RuntimeError, match="product_hydration_failed"):
                _run(svc.handle_product_webhook({"id": "p1", "price": 50}, webhook_event_type="product.price.updated"))
            db.refresh(product)
            assert product.title == "Rich Product"
        finally:
            db.close(); engine.dispose()

    def test_product_updated_routes_via_dispatcher(self):
        from core.webhook_dispatcher import _process_event
        from core.webhook_events import claim_next_batch, persist_event
        from commerce_scenario_fixtures import make_scenario_db, seed_tenant

        db, _engine = make_scenario_db()
        tenant = seed_tenant(db, name="Generic Store")
        intg = Integration(
            tenant_id=tenant.id,
            provider="salla",
            external_store_id="STORE-P8",
            config={"api_key": "k", "store_id": "STORE-P8"},
            enabled=True,
        )
        db.add(intg); db.commit()
        existing = Product(tenant_id=tenant.id, external_id="p8", title="Old", price="10", extra_metadata={})
        db.add(existing); db.commit()
        parsed = {"event": "product.updated", "merchant": "STORE-P8", "data": {"id": "p8", "title": "New Athletic Shoe", "price": {"amount": 20}}}
        ev = persist_event(
            db,
            provider="salla",
            raw_body="{}",
            parsed_payload=parsed,
            event_type="product.updated",
            external_event_id="evt-p8",
            store_id="STORE-P8",
        )
        batch = claim_next_batch(db, limit=1)
        _run(_process_event(db, batch[0]))
        db.refresh(existing)
        assert existing.title == "New Athletic Shoe"


class TestH9SpecialOfferContract:
    def test_official_fixture_fields(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            payload = {
                "id": 421,
                "name": "Osama Test Offer",
                "message": "Buy one get one",
                "expiry_date": "2025-11-29 09:00:00",
                "offer_type": "buy_x_get_y",
                "status": "active",
                "get": {"discount_type": "percentage", "discount_amount": "10", "quantity": "1"},
            }
            _run(svc.handle_special_offer_webhook(payload, webhook_event_type="specialoffer.created"))
            promo = db.query(Promotion).filter_by(tenant_id=tenant_id).one()
            assert promo.promotion_type == "percentage"
            assert float(promo.discount_value) == 10.0
            assert promo.extra_metadata.get("offer_type") == "buy_x_get_y"
            assert promo.extra_metadata.get("message") == "Buy one get one"
            assert "raw" not in promo.extra_metadata
        finally:
            db.close(); engine.dispose()


class TestH11SafeErrors:
    def test_mark_failed_stores_safe_code(self):
        from core.webhook_events import mark_failed

        db, tenant_id, engine = _make_db()
        try:
            ev = WebhookEvent(
                tenant_id=tenant_id,
                provider="salla",
                event_type="product.price.updated",
                status="received",
                raw_body="{}",
                parsed_payload={},
            )
            db.add(ev); db.commit()
            mark_failed(db, ev, RuntimeError("secret token=abc provider body leak"))
            db.refresh(ev)
            assert "secret" not in (ev.last_error or "")
            assert "token" not in (ev.last_error or "")
            assert ev.last_error == "RuntimeError"
        finally:
            db.close(); engine.dispose()

    def test_orders_poller_diag_strips_sensitive_fields(self):
        from services.salla_realtime_observability import build_realtime_commerce_diag
        from services.salla_orders_poller import _state

        db, tenant_id, engine = _make_db()
        try:
            _state["tenants"][tenant_id] = {
                "tenant_id": tenant_id,
                "store_id": "SECRET-STORE",
                "result": "error",
                "error": "ValueError('api_key=leak')",
                "stats": {"api_error": "raw provider body", "new_orders": 1},
            }
            diag = build_realtime_commerce_diag(db, tenant_id)
            tenant = diag["orders_poller"]["tenant"]
            assert tenant is not None
            assert "store_id" not in tenant
            assert "api_error" not in str(tenant)
            assert "leak" not in str(diag)
        finally:
            db.close(); engine.dispose()


class TestM2CartCurrency:
    def test_currency_preserved_on_partial_update(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            full = {
                "id": "31",
                "total": {"amount": 120, "currency": "SAR"},
                "customer": {"mobile": "+966500333111"},
                "items": [{"name": "Shoe"}],
                "abandoned_at": _riyadh_abandoned(),
            }
            _run(svc.handle_abandoned_cart_webhook(full, event_kind="created", webhook_event_type="abandoned.cart"))
            _run(svc.handle_abandoned_cart_webhook({"id": "31", "status": "active"}, event_kind="status_changed", webhook_event_type="abandoned.cart.status.changed"))
            cart = db.query(Order).filter_by(external_id="cart-31").one()
            assert cart.extra_metadata.get("currency") == "SAR"
        finally:
            db.close(); engine.dispose()


class TestH10AdminDiagAuth:
    def test_support_impersonation_cannot_query_other_tenant(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.auth import require_admin
        from routers.admin import salla_realtime_commerce_diag

        app = FastAPI()
        app.get("/diag")(salla_realtime_commerce_diag)

        async def _support_user():
            return {
                "role": "support_impersonation",
                "impersonation": True,
                "tenant_id": 10,
                "sub": "support@example.com",
            }

        app.dependency_overrides[require_admin] = _support_user
        client = TestClient(app)
        resp = client.get("/diag", params={"tenant_id": 99})
        assert resp.status_code == 403
        assert "99" not in resp.text

    def test_platform_admin_can_query_any_tenant(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.auth import require_admin
        from routers.admin import salla_realtime_commerce_diag

        app = FastAPI()
        app.get("/diag")(salla_realtime_commerce_diag)

        async def _admin_user():
            return {"role": "admin", "sub": "admin@example.com"}

        app.dependency_overrides[require_admin] = _admin_user
        client = TestClient(app)
        with patch("services.salla_realtime_observability.build_realtime_commerce_diag", return_value={"tenant_id": 99}):
            resp = client.get("/diag", params={"tenant_id": 99})
        assert resp.status_code == 200
        assert resp.json()["diag"]["tenant_id"] == 99
