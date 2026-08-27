# -*- coding: utf-8 -*-
"""PR #888 remaining Sol remediation regression tests."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

from database.models import AutomationEvent, AutomationExecution, Base, Customer, Integration, Order, Product, Promotion, StoreKnowledgeSnapshot, Tenant, WebhookEvent
from services.store_sync import StoreSyncService, _extract_abandoned_at_iso


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _run(coro):
    return asyncio.run(coro)




def _official_abandoned_cart_payload(cart_id: str = "1097962121", age: int = 83):
    return {
        "id": int(cart_id) if str(cart_id).isdigit() else cart_id,
        "total": {"amount": 100, "currency": "SAR"},
        "checkout_url": f"https://salla.sa/example/checkout/{cart_id}",
        "age_in_minutes": age,
        "created_at": {"date": "2025-01-21 17:09:39.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
        "updated_at": {"date": "2025-01-21 17:09:39.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"},
        "customer": {"name": "User", "mobile": "+966500111222"},
        "items": [{"id": 1, "product_id": 99, "quantity": 1}],
    }

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Generic Store", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id, engine


class TestH1OrderStatusSlug:
    def test_status_updated_prefers_order_slug_over_outer_label(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            db.add(Order(
                tenant_id=tenant_id,
                external_id="629263027",
                status="pending",
                total="200.00",
                line_items=[{"name": "Sports Shoe", "qty": 1}],
                customer_info={"mobile": "+966500111222"},
            ))
            db.commit()
            payload = {
                "id": 198290473,
                "status": "completed-label-ar",
                "order": {
                    "id": 629263027,
                    "status": {"slug": "completed", "name": "done"},
                    "total": {"amount": 200},
                },
            }
            _run(svc.handle_order_webhook(payload, webhook_event_type="order.status.updated"))
            row = db.query(Order).filter_by(tenant_id=tenant_id, external_id="629263027").one()
            assert row.status == "completed"
            assert row.total == "200"
            assert row.line_items
            assert db.query(Order).filter_by(external_id="198290473").first() is None
        finally:
            db.close(); engine.dispose()



class TestH2AdapterPaginationFailure:
    def test_get_customers_propagates_partial_pagination(self):
        from store_adapters.salla_adapter import SallaAdapter
        from store_adapters.salla_pagination import SallaPaginatedFetchIncomplete

        adapter = SallaAdapter(api_key="test-key", store_id="store-1")
        adapter._get_all_pages_strict = AsyncMock(side_effect=SallaPaginatedFetchIncomplete(
            partial=True, items=[{"id": "c1"}], pages_fetched=1,
        ))
        with pytest.raises(SallaPaginatedFetchIncomplete):
            _run(adapter.get_customers())

    def test_sync_customers_strict_blocks_watermark(self):
        from store_adapters.salla_pagination import SallaPaginatedFetchIncomplete

        db, tenant_id, engine = _make_db()
        try:
            db.add(StoreKnowledgeSnapshot(tenant_id=tenant_id, policy_summary={}))
            db.commit()
            svc = StoreSyncService(db, tenant_id)
            adapter = MagicMock()
            adapter.platform = "salla"
            adapter.get_customers = AsyncMock(side_effect=SallaPaginatedFetchIncomplete(
                partial=True, items=[{"id": "c1"}], pages_fetched=1,
            ))
            svc._adapter = adapter
            with pytest.raises(RuntimeError, match="customer_sync_failed"):
                _run(svc.sync_customers(incremental=True, strict=True))
            assert svc._commerce_reconcile_watermarks()["customers"] == ""
        finally:
            db.close(); engine.dispose()


class TestH4OfficialTimestampContract:
    def _assert_delay(self, db, tenant_id, ev, now, expected):
        from core.automation_engine import _try_execute
        automation = type("A", (), {})()
        automation.id = 9
        automation.automation_type = "abandoned_cart"
        automation.config = {"steps": [{"delay_minutes": 30}]}
        automation.enabled = True
        assert _run(_try_execute(db, tenant_id, ev, automation, now)) == expected

    def test_js_envelope_converts_to_utc_without_host_tz(self):
        from services.salla_datetime import parse_salla_js_envelope_datetime
        dt = parse_salla_js_envelope_datetime("Tue Jan 21 2025 18:00:32 GMT+0300")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.hour == 15 and dt.minute == 0 and dt.second == 32

    def test_created_plus_age_is_not_used_as_anchor(self):
        from services.salla_datetime import parse_salla_datetime_to_utc
        first = _official_abandoned_cart_payload(age=83)
        later = _official_abandoned_cart_payload(age=200)
        created = parse_salla_datetime_to_utc(first["created_at"])
        assert created + timedelta(minutes=83) != created + timedelta(minutes=200)
        assert _extract_abandoned_at_iso(first) == ""
        assert _extract_abandoned_at_iso(later) == ""

    def test_official_webhook_schedules_from_envelope_then_replay_keeps_anchor(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            envelope = "Tue Jan 21 2025 18:00:32 GMT+0300"
            _run(svc.handle_abandoned_cart_webhook(
                _official_abandoned_cart_payload("501", age=83),
                event_kind="created",
                webhook_event_type="abandoned.cart",
                envelope_created_at=envelope,
                original_received_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
            ))
            cart = db.query(Order).filter_by(external_id="cart-501").one()
            assert cart.extra_metadata.get("abandonment_anchor_source") == "provider_webhook_event"
            assert cart.extra_metadata.get("first_provider_abandoned_observed_at").startswith("2025-01-21T15:00:32")
            ev = db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").one()
            assert ev.created_at.hour == 15 and ev.created_at.minute == 0
            self._assert_delay(db, tenant_id, ev, datetime(2025, 1, 21, 15, 15, 0), "delay")
            with patch("core.automation_engine._evaluate_conditions", return_value=(False, "test_skip")):
                self._assert_delay(db, tenant_id, ev, datetime(2025, 1, 21, 15, 31, 0), "skipped")
            first_anchor = cart.extra_metadata.get("first_provider_abandoned_observed_at")
            _run(svc.handle_abandoned_cart_webhook(
                _official_abandoned_cart_payload("501", age=200),
                event_kind="created",
                webhook_event_type="abandoned.cart",
                envelope_created_at="Wed Jan 22 2025 19:00:00 GMT+0300",
                original_received_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
            ))
            db.refresh(cart)
            db.refresh(ev)
            assert cart.extra_metadata.get("first_provider_abandoned_observed_at") == first_anchor
            assert cart.extra_metadata.get("abandonment_anchor_source") == "provider_webhook_event"
            assert db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").count() == 1
            assert ev.created_at.hour == 15
        finally:
            db.close(); engine.dispose()

    def test_poller_two_ages_keeps_first_observation(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            first = _official_abandoned_cart_payload("pol-1", age=15)
            later = _official_abandoned_cart_payload("pol-1", age=200)
            t0 = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
            t1 = datetime(2026, 4, 19, 11, 0, tzinfo=timezone.utc)
            adapter = MagicMock()
            adapter.get_abandoned_carts = AsyncMock(return_value=[first])
            svc._adapter = adapter
            with patch("services.store_sync._utc_now", return_value=t0):
                _run(svc.sync_abandoned_carts())
            cart = db.query(Order).filter_by(external_id="cart-pol-1").one()
            anchor = cart.extra_metadata.get("first_provider_abandoned_observed_at")
            source = cart.extra_metadata.get("abandonment_anchor_source")
            assert source == "first_poller_observation"
            ev = db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").one()
            adapter.get_abandoned_carts = AsyncMock(return_value=[later])
            with patch("services.store_sync._utc_now", return_value=t1):
                _run(svc.sync_abandoned_carts())
            db.refresh(cart)
            assert cart.extra_metadata.get("first_provider_abandoned_observed_at") == anchor
            assert db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").count() == 1
            db.refresh(ev)
            assert ev.created_at == ev.created_at
        finally:
            db.close(); engine.dispose()

    def test_retry_uses_original_received_at_not_now(self):
        from core.webhook_dispatcher import _process_event
        from core.webhook_events import claim_next_batch, persist_event
        from commerce_scenario_fixtures import make_scenario_db, seed_tenant

        db, engine = make_scenario_db()
        try:
            tenant = seed_tenant(db, name="Generic Store")
            store_id = "STORE-H4-RETRY"
            db.add(Integration(
                tenant_id=tenant.id, provider="salla", external_store_id=store_id,
                config={"api_key": "k", "store_id": store_id}, enabled=True,
            ))
            db.commit()
            received = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
            parsed = {
                "event": "abandoned.cart",
                "merchant": store_id,
                "data": _official_abandoned_cart_payload("77", age=40),
            }
            persist_event(
                db, provider="salla", raw_body="{}", parsed_payload=parsed,
                event_type="abandoned.cart", external_event_id="evt-h4-retry", store_id=store_id,
            )
            row = db.query(WebhookEvent).filter_by(external_event_id="evt-h4-retry").one()
            row.received_at = received.replace(tzinfo=None)
            db.commit()
            batch = claim_next_batch(db, limit=1)
            _run(_process_event(db, batch[0]))
            cart = db.query(Order).filter_by(external_id="cart-77").one()
            assert cart.extra_metadata.get("abandonment_anchor_source") == "first_webhook_observation"
            assert cart.extra_metadata.get("first_provider_abandoned_observed_at").startswith("2026-04-19T09:00:00")
            ev = db.query(AutomationEvent).filter_by(tenant_id=tenant.id, event_type="cart_abandoned").one()
            assert ev.created_at.hour == 9
        finally:
            db.close(); engine.dispose()

    def test_explicit_compat_timestamp_first_claim_only(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            payload = _official_abandoned_cart_payload("88", age=10)
            payload["abandoned_at"] = "2026-04-19T08:00:00+00:00"
            _run(svc.handle_abandoned_cart_webhook(
                payload, event_kind="created", webhook_event_type="abandoned.cart",
                envelope_created_at="Tue Apr 19 2026 12:00:00 GMT+0300",
            ))
            cart = db.query(Order).filter_by(external_id="cart-88").one()
            assert cart.extra_metadata.get("abandonment_anchor_source") == "provider_explicit"
            first = cart.extra_metadata.get("first_provider_abandoned_observed_at")
            replay = _official_abandoned_cart_payload("88", age=90)
            replay["abandoned_at"] = "2026-04-19T10:00:00+00:00"
            _run(svc.handle_abandoned_cart_webhook(
                replay, event_kind="created", webhook_event_type="abandoned.cart",
                envelope_created_at="Tue Apr 19 2026 15:00:00 GMT+0300",
            ))
            db.refresh(cart)
            assert cart.extra_metadata.get("first_provider_abandoned_observed_at") == first
            assert db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").count() == 1
        finally:
            db.close(); engine.dispose()

    def test_cart_created_and_updated_alone_are_not_abandonment(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            payload = {
                "id": "88",
                "total": {"amount": 10, "currency": "SAR"},
                "customer": {"mobile": "+966500900900"},
                "items": [{"name": "Shirt"}],
                "created_at": "2026-04-19 10:00:00",
                "updated_at": "2026-04-19 10:35:00",
            }
            _run(svc.handle_abandoned_cart_webhook(payload, event_kind="created", webhook_event_type="abandoned.cart"))
            cart = db.query(Order).filter_by(external_id="cart-88").one()
            assert not cart.extra_metadata.get("first_provider_abandoned_observed_at")
            assert db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").count() == 0
        finally:
            db.close(); engine.dispose()

    def test_terminal_cart_is_not_scheduled(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            base = _official_abandoned_cart_payload("44", age=30)
            _run(svc.handle_abandoned_cart_webhook(
                {**base, "status": "purchased"}, event_kind="purchased",
                webhook_event_type="abandoned.cart.purchased",
                envelope_created_at="Tue Apr 19 2026 12:00:00 GMT+0300",
            ))
            _run(svc.handle_abandoned_cart_webhook(
                base, event_kind="created", webhook_event_type="abandoned.cart",
                envelope_created_at="Tue Apr 19 2026 12:00:00 GMT+0300",
            ))
            assert db.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").count() == 0
        finally:
            db.close(); engine.dispose()


class TestH6UnrelatedCartSnapshot:
    def test_cancel_preserves_unrelated_cart_state(self):
        from services.cart_recovery_cancel import cancel_recovery_for_customer

        db, tenant_id, engine = _make_db()
        try:
            customer = Customer(tenant_id=tenant_id, phone="+966500111000", normalized_phone="966500111000", name="Buyer")
            db.add(customer)
            db.flush()
            ev_b = AutomationEvent(
                tenant_id=tenant_id,
                customer_id=customer.id,
                event_type="cart_abandoned",
                payload={"cart_external_id": "cart-B", "cart_id": "B", "step_idx": 0, "recovery_followups": []},
                processed=False,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(ev_b)
            db.flush()
            db.commit()
            before = {
                "processed": ev_b.processed,
                "payload": dict(ev_b.payload or {}),
            }
            cancel_recovery_for_customer(
                db, tenant_id=tenant_id, customer_id=customer.id,
                matched_cart_external_id="cart-A", reason="customer_purchased",
            )
            db.refresh(ev_b)
            assert ev_b.processed == before["processed"]
            assert dict(ev_b.payload or {}) == before["payload"]
        finally:
            db.close(); engine.dispose()


class TestH7ProductFailureMatrix:
    def test_partial_new_product_without_adapter_fails(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            svc._adapter = None
            with pytest.raises(RuntimeError, match="product_hydration_failed"):
                _run(svc.handle_product_webhook({"id": "new-1"}, webhook_event_type="product.price.updated"))
            assert db.query(Product).filter_by(external_id="new-1").count() == 0
        finally:
            db.close(); engine.dispose()


class TestH7ProcessEventHydrationMatrix:
    @pytest.mark.parametrize("existing,adapter_mode", [
        (True, "exception"),
        (True, "none"),
        (True, "missing"),
        (False, "exception"),
        (False, "none"),
        (False, "missing"),
    ])
    def test_partial_product_via_process_event(self, existing, adapter_mode):
        from core.webhook_dispatcher import _process_event
        from core.webhook_events import claim_next_batch, persist_event
        from commerce_scenario_fixtures import make_scenario_db, seed_tenant

        db, engine = make_scenario_db()
        try:
            tenant = seed_tenant(db, name="Generic Store")
            store_id = f"STORE-H7-{adapter_mode}-{'ex' if existing else 'nw'}"
            db.add(Integration(
                tenant_id=tenant.id,
                provider="salla",
                external_store_id=store_id,
                config={"api_key": "k", "store_id": store_id},
                enabled=True,
            ))
            db.commit()
            product_id = f"p-{adapter_mode}-{'ex' if existing else 'nw'}"
            if existing:
                db.add(Product(
                    tenant_id=tenant.id,
                    external_id=product_id,
                    title="Rich Product",
                    price="99",
                    extra_metadata={"kept": True},
                ))
                db.commit()
            if adapter_mode == "exception":
                adapter = MagicMock()
                adapter.get_product = AsyncMock(side_effect=RuntimeError("http down"))
            elif adapter_mode == "none":
                adapter = MagicMock()
                adapter.get_product = AsyncMock(return_value=None)
            else:
                adapter = None
            parsed = {
                "event": "product.price.updated",
                "merchant": store_id,
                "data": {"id": product_id, "price": 50},
            }
            persist_event(
                db,
                provider="salla",
                raw_body="{}",
                parsed_payload=parsed,
                event_type="product.price.updated",
                external_event_id=f"evt-{product_id}",
                store_id=store_id,
            )
            batch = claim_next_batch(db, limit=1)
            assert batch
            with patch.object(StoreSyncService, "_get_adapter", return_value=adapter):
                _run(_process_event(db, batch[0]))
            ev = db.query(WebhookEvent).filter_by(external_event_id=f"evt-{product_id}").one()
            assert ev.status == "failed"
            assert ev.next_retry_at is not None
            assert ev.status != "processed"
            if existing:
                row = db.query(Product).filter_by(tenant_id=tenant.id, external_id=product_id).one()
                assert row.title == "Rich Product"
                assert row.price == "99"
            else:
                assert db.query(Product).filter_by(tenant_id=tenant.id, external_id=product_id).count() == 0
        finally:
            db.close(); engine.dispose()


class TestH9UnsupportedOfferTransition:
    def test_percentage_to_unsupported_clears_discount(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            _run(svc.handle_special_offer_webhook({
                "id": "offer-9", "offer_type": "special", "message": "10pct",
                "expiry_date": "2026-12-31", "get": {"discount_type": "percentage", "discount_amount": 10},
            }, webhook_event_type="specialoffer.created"))
            row = db.query(Promotion).filter_by(tenant_id=tenant_id).one()
            assert str(row.discount_value) in ("10", "10.00", "10.0")
            _run(svc.handle_special_offer_webhook({
                "id": "offer-9", "offer_type": "free_shipping", "message": "ship",
                "expiry_date": "2026-12-31", "get": {},
            }, webhook_event_type="specialoffer.updated"))
            db.refresh(row)
            assert row.discount_value is None
        finally:
            db.close(); engine.dispose()


class TestH11LoggingSafety:
    def test_mark_failed_log_is_safe(self, caplog):
        import logging
        from core.webhook_events import mark_failed

        db, tenant_id, engine = _make_db()
        try:
            ev = WebhookEvent(
                tenant_id=tenant_id, provider="salla", event_type="order.created",
                status="processing", raw_body="{}", parsed_payload={},
            )
            db.add(ev); db.commit()
            caplog.set_level(logging.ERROR)
            mark_failed(db, ev, RuntimeError("secret-token +966500111222"))
            db.refresh(ev)
            assert ev.last_error == "RuntimeError"
            logged = " ".join(r.message for r in caplog.records)
            assert "secret-token" not in logged
            assert "966500111222" not in logged
        finally:
            db.close(); engine.dispose()


class TestM2ExplicitCurrencyUpdate:
    def test_explicit_currency_update_via_webhook(self):
        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            base = _official_abandoned_cart_payload("cur-1")
            _run(svc.handle_abandoned_cart_webhook(base, event_kind="created", webhook_event_type="abandoned.cart"))
            cart = db.query(Order).filter_by(external_id="cart-cur-1").one()
            assert cart.extra_metadata.get("currency") == "SAR"
            partial = dict(base); partial["total"] = {"amount": 55}
            _run(svc.handle_abandoned_cart_webhook(partial, event_kind="updated", webhook_event_type="abandoned.cart"))
            db.refresh(cart)
            assert cart.extra_metadata.get("currency") == "SAR"
            explicit = dict(base); explicit["total"] = {"amount": 60, "currency": "USD"}
            _run(svc.handle_abandoned_cart_webhook(explicit, event_kind="updated", webhook_event_type="abandoned.cart"))
            db.refresh(cart)
            assert cart.extra_metadata.get("currency") == "USD"
            assert cart.line_items
        finally:
            db.close(); engine.dispose()


class TestH3PollerCommit:
    def test_sync_abandoned_carts_emit_survives_session_close(self, tmp_path):
        import tempfile
        from sqlalchemy.orm import sessionmaker

        db_path = tmp_path / "cart_emit.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        tenant = Tenant(name="Poller Store", is_active=True)
        db.add(tenant)
        db.commit()
        tenant_id = tenant.id
        svc = StoreSyncService(db, tenant_id)
        adapter = MagicMock()
        payload = _official_abandoned_cart_payload("pol-1")
        adapter.get_abandoned_carts = AsyncMock(return_value=[payload])
        svc._adapter = adapter
        _run(svc.sync_abandoned_carts())
        db.close()
        engine.dispose()

        engine2 = create_engine(f"sqlite:///{db_path}")
        Session2 = sessionmaker(bind=engine2)
        db2 = Session2()
        try:
            cart = db2.query(Order).filter_by(tenant_id=tenant_id, external_id="cart-pol-1").one()
            assert cart.extra_metadata.get("recovery_event_id")
            events = db2.query(AutomationEvent).filter_by(tenant_id=tenant_id, event_type="cart_abandoned").all()
            assert len(events) == 1
            assert events[0].id == cart.extra_metadata.get("recovery_event_id")
        finally:
            db2.close(); engine2.dispose()
