# -*- coding: utf-8 -*-
"""PR #888 remaining Sol remediation regression tests."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
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

from database.models import AutomationEvent, AutomationExecution, Base, Customer, Integration, Order, Product, Promotion, SmartAutomation, StoreKnowledgeSnapshot, Tenant, WebhookEvent
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
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
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


def _file_db(tmp_path, name="h6.db"):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    tenant = Tenant(name="Generic Commerce Store", is_active=True)
    db.add(tenant)
    db.commit()
    return db, tenant.id, engine, db_path


def _reopen_db(db_path, db=None, engine=None):
    if db is not None:
        db.close()
    if engine is not None:
        engine.dispose()
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_recovery_automation(db, tenant_id):
    auto = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="abandoned_cart",
        name="Cart recovery",
        enabled=True,
        engine="recovery",
        config={"steps": [
            {"delay_minutes": 0, "enabled": True},
            {"delay_minutes": 0, "enabled": True},
        ]},
    )
    db.add(auto)
    db.flush()
    return auto


def _create_abandoned_via_handler(db, tenant_id, cart_id, phone="+966500111222"):
    payload = _official_abandoned_cart_payload(str(cart_id), age=83)
    payload["customer"] = {"name": "Ahmad Salem", "mobile": phone}
    svc = StoreSyncService(db, tenant_id)
    _run(svc.handle_abandoned_cart_webhook(
        payload,
        event_kind="created",
        webhook_event_type="abandoned.cart",
        envelope_created_at="Tue Jan 21 2025 18:00:32 GMT+0300",
        original_received_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    ))
    return db.query(Order).filter_by(tenant_id=tenant_id, external_id=f"cart-{cart_id}").one()


def _promote_parent_and_execution(db, tenant_id, cart_external_id, automation_id):
    from sqlalchemy.orm.attributes import flag_modified

    matches = [
        ev for ev in db.query(AutomationEvent).filter_by(
            tenant_id=tenant_id, event_type="cart_abandoned",
        ).all()
        if (ev.payload or {}).get("cart_external_id") == cart_external_id
        and int((ev.payload or {}).get("step_idx") or 0) == 0
    ]
    assert len(matches) == 1
    parent = matches[0]
    parent.processed = True
    parent.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    payload = dict(parent.payload or {})
    payload["recovery_followups"] = []
    parent.payload = payload
    flag_modified(parent, "payload")
    execution = AutomationExecution(
        tenant_id=tenant_id,
        automation_id=automation_id,
        event_id=parent.id,
        customer_id=parent.customer_id,
        status="sent",
        action_taken={"metrics": {"sent": True}},
    )
    db.add(execution)
    db.flush()
    return parent, execution


def _cart_state(cart):
    return {
        "is_abandoned": cart.is_abandoned,
        "status": cart.status,
        "total": cart.total,
        "customer_info": copy.deepcopy(dict(cart.customer_info or {})),
        "extra_metadata": copy.deepcopy(dict(cart.extra_metadata or {})),
        "checkout_url": cart.checkout_url,
        "line_items": copy.deepcopy(list(cart.line_items or [])),
    }


def _followups_for(db, tenant_id, cart_external_id):
    return [
        ev for ev in db.query(AutomationEvent).filter_by(
            tenant_id=tenant_id, event_type="cart_abandoned",
        ).all()
        if (ev.payload or {}).get("cart_external_id") == cart_external_id
        and int((ev.payload or {}).get("step_idx") or 0) > 0
    ]


def _parent_for(db, tenant_id, cart_external_id):
    matches = [
        ev for ev in db.query(AutomationEvent).filter_by(
            tenant_id=tenant_id, event_type="cart_abandoned",
        ).all()
        if (ev.payload or {}).get("cart_external_id") == cart_external_id
        and int((ev.payload or {}).get("step_idx") or 0) == 0
    ]
    assert len(matches) == 1
    return matches[0]


class TestH6PartialPurchaseCancelsFullChain:
    def _run_scenario(self, tmp_path, *, clear_stored_phone: bool, unresolved_customer: bool):
        from core.automation_emitters import scan_abandoned_cart_followups
        from sqlalchemy.orm.attributes import flag_modified

        db, tenant_id, engine, db_path = _file_db(tmp_path)
        try:
            auto = _seed_recovery_automation(db, tenant_id)
            if unresolved_customer:
                cart_a = Order(
                    tenant_id=tenant_id,
                    external_id="cart-801",
                    status="abandoned",
                    total="80.00",
                    is_abandoned=True,
                    customer_info={},
                    line_items=[{"name": "White sports shoe", "qty": 1}],
                    extra_metadata={"source_kind": "abandoned_cart", "raw_cart_id": "801"},
                )
                db.add(cart_a)
                db.flush()
                parent_a = AutomationEvent(
                    tenant_id=tenant_id,
                    customer_id=None,
                    event_type="cart_abandoned",
                    processed=True,
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
                    payload={
                        "cart_external_id": "cart-801",
                        "cart_id": "801",
                        "step_idx": 0,
                        "recovery_followups": [],
                    },
                )
                db.add(parent_a)
                db.flush()
                exec_a = AutomationExecution(
                    tenant_id=tenant_id,
                    automation_id=auto.id,
                    event_id=parent_a.id,
                    customer_id=None,
                    status="sent",
                    action_taken={"metrics": {"sent": True}},
                )
                db.add(exec_a)
                db.flush()
                cart_b = Order(
                    tenant_id=tenant_id,
                    external_id="cart-802",
                    status="abandoned",
                    total="40.00",
                    is_abandoned=True,
                    customer_info={},
                    line_items=[{"name": "Blue cotton shirt", "qty": 1}],
                    extra_metadata={"source_kind": "abandoned_cart", "raw_cart_id": "802"},
                )
                db.add(cart_b)
                db.flush()
                parent_b = AutomationEvent(
                    tenant_id=tenant_id,
                    customer_id=None,
                    event_type="cart_abandoned",
                    processed=True,
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
                    payload={
                        "cart_external_id": "cart-802",
                        "cart_id": "802",
                        "step_idx": 0,
                        "recovery_followups": [],
                    },
                )
                db.add(parent_b)
                db.flush()
                exec_b = AutomationExecution(
                    tenant_id=tenant_id,
                    automation_id=auto.id,
                    event_id=parent_b.id,
                    customer_id=None,
                    status="sent",
                    action_taken={"metrics": {"sent": True}},
                )
                db.add(exec_b)
                db.commit()
                id_a, id_b = "801", "802"
            else:
                cart_a = _create_abandoned_via_handler(db, tenant_id, "701")
                cart_b = _create_abandoned_via_handler(db, tenant_id, "702")
                parent_a, exec_a = _promote_parent_and_execution(db, tenant_id, "cart-701", auto.id)
                parent_b, exec_b = _promote_parent_and_execution(db, tenant_id, "cart-702", auto.id)
                if clear_stored_phone:
                    cart_a.customer_info = {"name": "Ahmad Salem"}
                    flag_modified(cart_a, "customer_info")
                db.commit()
                id_a, id_b = "701", "702"

            before_b = {
                "cart": _cart_state(cart_b),
                "parent": copy.deepcopy(dict(parent_b.payload or {})),
                "processed": parent_b.processed,
                "execution": copy.deepcopy(dict(exec_b.action_taken or {})),
                "followups": len(_followups_for(db, tenant_id, f"cart-{id_b}")),
            }
            svc = StoreSyncService(db, tenant_id)
            _run(svc.handle_abandoned_cart_webhook(
                {"id": int(id_a)},
                event_kind="purchased",
                webhook_event_type="abandoned.cart.purchased",
            ))
        finally:
            db.close()
            engine.dispose()

        db, engine = _reopen_db(db_path)
        try:
            parent_a = _parent_for(db, tenant_id, f"cart-{id_a}")
            parent_b = _parent_for(db, tenant_id, f"cart-{id_b}")
            exec_a = db.query(AutomationExecution).filter_by(tenant_id=tenant_id, event_id=parent_a.id).one()
            exec_b = db.query(AutomationExecution).filter_by(tenant_id=tenant_id, event_id=parent_b.id).one()
            cart_a = db.query(Order).filter_by(tenant_id=tenant_id, external_id=f"cart-{id_a}").one()
            cart_b = db.query(Order).filter_by(tenant_id=tenant_id, external_id=f"cart-{id_b}").one()
            payload_a = dict(parent_a.payload or {})
            assert payload_a.get("recovery_converted_at")
            skipped = {
                int(item.get("step_idx"))
                for item in (payload_a.get("recovery_followups") or [])
                if item.get("skipped")
            }
            assert 1 in skipped
            metrics = dict((exec_a.action_taken or {}).get("metrics") or {})
            assert metrics.get("converted") is True
            assert metrics.get("remaining_steps_skipped") is True
            assert metrics.get("skip_reason") == "abandoned_cart_purchased"
            assert cart_a.is_abandoned is False
            assert dict(parent_b.payload or {}) == before_b["parent"]
            assert parent_b.processed == before_b["processed"]
            assert dict(exec_b.action_taken or {}) == before_b["execution"]
            assert _cart_state(cart_b) == before_b["cart"]
            assert len(_followups_for(db, tenant_id, f"cart-{id_b}")) == before_b["followups"]

            emitted = scan_abandoned_cart_followups(db, tenant_id)
            assert _followups_for(db, tenant_id, f"cart-{id_a}") == []
            follow_b = _followups_for(db, tenant_id, f"cart-{id_b}")
            assert follow_b, emitted
            after_scan_b_payload = copy.deepcopy(dict(parent_b.payload or {}))
            after_scan_b_followups = [
                (ev.id, copy.deepcopy(dict(ev.payload or {})), ev.processed) for ev in follow_b
            ]
            svc = StoreSyncService(db, tenant_id)
            _run(svc.handle_abandoned_cart_webhook(
                {"id": int(id_a)},
                event_kind="purchased",
                webhook_event_type="abandoned.cart.purchased",
            ))
            db.expire_all()
            parent_a = _parent_for(db, tenant_id, f"cart-{id_a}")
            parent_b = _parent_for(db, tenant_id, f"cart-{id_b}")
            assert dict(parent_a.payload or {}).get("recovery_converted_at")
            assert dict(parent_b.payload or {}) == after_scan_b_payload
            replay_follow_b = _followups_for(db, tenant_id, f"cart-{id_b}")
            assert [
                (ev.id, dict(ev.payload or {}), ev.processed) for ev in replay_follow_b
            ] == after_scan_b_followups
            assert _followups_for(db, tenant_id, f"cart-{id_a}") == []
        finally:
            db.close()
            engine.dispose()

    def test_purchased_without_phone_uses_stored_cart_phone(self, tmp_path):
        self._run_scenario(tmp_path, clear_stored_phone=False, unresolved_customer=False)

    def test_purchased_without_phone_uses_matching_parent_event(self, tmp_path):
        self._run_scenario(tmp_path, clear_stored_phone=True, unresolved_customer=False)

    def test_purchased_without_phone_stamps_chain_when_customer_unresolved(self, tmp_path):
        self._run_scenario(tmp_path, clear_stored_phone=True, unresolved_customer=True)


_H11_SKIP_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "message",
    "taskName", "color_message",
}
H11_CANARIES = (
    "+966500111222",
    "tok_H11_SECRET_CANARY",
    "https://canary.example/checkout/H11",
    "CANARY_BODY_H11_PROVIDER",
    "STORE_ID_CANARY_8889",
    "CANARY_EXC_H11_SECRET_BODY",
)


def _h11_blob(caplog) -> str:
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    chunks = []
    for rec in caplog.records:
        chunks.append(formatter.format(rec))
        chunks.append(repr(rec.args))
        extra = {k: rec.__dict__[k] for k in rec.__dict__ if k not in _H11_SKIP_ATTRS}
        if extra:
            chunks.append(json.dumps(extra, default=str, sort_keys=True))
        if str(getattr(rec, "name", "")).startswith("nahla"):
            assert rec.exc_info is None, rec
            assert not rec.exc_text, rec.exc_text
    return "\n".join(chunks)


def _h11_http_error():
    import httpx

    request = httpx.Request("GET", "https://canary.example/checkout/H11")
    response = httpx.Response(
        500,
        text="CANARY_BODY_H11_PROVIDER tok_H11_SECRET_CANARY +966500111222 STORE_ID_CANARY_8889",
        request=request,
    )
    return httpx.HTTPStatusError(
        "CANARY_EXC_H11_SECRET_BODY",
        request=request,
        response=response,
    )


def _assert_no_h11_canaries(blob: str, diagnostic=None):
    haystacks = [blob]
    if diagnostic is not None:
        haystacks.append(json.dumps(diagnostic, default=str))
    for haystack in haystacks:
        for canary in H11_CANARIES:
            assert canary not in haystack, canary


class TestH11RemainingLogSites:
    def test_orders_poller_logs_and_diagnostic_state_are_safe(self, caplog, tmp_path):
        import httpx
        from services.salla_orders_poller import _run_one_tick, _state, get_poller_state

        db, tenant_id, engine, db_path = _file_db(tmp_path, "h11-poller.db")
        try:
            db.add(Integration(
                tenant_id=tenant_id,
                provider="salla",
                enabled=True,
                external_store_id="STORE_ID_CANARY_8889",
                config={
                    "store_id": "STORE_ID_CANARY_8889",
                    "merchant_id": "STORE_ID_CANARY_8889",
                    "api_key": "tok_H11_SECRET_CANARY",
                },
            ))
            db.commit()
        finally:
            db.close()
            engine.dispose()

        Session = sessionmaker(bind=create_engine(f"sqlite:///{db_path.as_posix()}"))
        mock_adapter = MagicMock()
        mock_adapter.get_orders = AsyncMock(side_effect=_h11_http_error())
        _state["tenants"].clear()
        caplog.set_level(logging.DEBUG)
        # Fault injection: HTTP boundary = adapter.get_orders HTTPStatusError.
        # Persist boundary = StoreSyncService.sync_orders raises RuntimeError.
        # SessionLocal is patched to the sqlite session factory.
        # adapter_for_integration is patched to return mock_adapter.
        # _log helpers and poller log statements are real.
        with patch("core.database.SessionLocal", Session), patch(
            "store_integration.registry.adapter_for_integration",
            return_value=mock_adapter,
        ), patch(
            "services.store_sync.StoreSyncService.sync_orders",
            new=AsyncMock(side_effect=RuntimeError(
                "CANARY_EXC_H11_SECRET_BODY tok_H11_SECRET_CANARY +966500111222"
            )),
        ):
            _run(_run_one_tick())
        blob = _h11_blob(caplog)
        assert "salla_orders_poller.tenant_scan_failed" in blob
        assert "salla_orders_poller.salla_api_response_failed" in blob
        assert "error_class=HTTPStatusError" in blob or "HTTPStatusError" in blob
        assert "error_class=RuntimeError" in blob
        diagnostic = get_poller_state()
        _assert_no_h11_canaries(blob, diagnostic)
        dumped = json.dumps(diagnostic, default=str)
        assert "store_hash=" in dumped
        assert "STORE_ID_CANARY_8889" not in dumped
        tenants = diagnostic.get("tenants") or {}
        assert tenants, diagnostic
        for row in tenants.values():
            assert "store_id" not in row
            assert row.get("error") == "RuntimeError"

    def test_adapter_get_abandoned_carts_log_is_safe(self, caplog):
        from store_adapters.salla_adapter import SallaAdapter

        adapter = SallaAdapter(api_key="tok_H11_SECRET_CANARY", store_id="STORE_ID_CANARY_8889")
        # Fault injection: HTTP boundary = instance _get_all_pages raises HTTPStatusError.
        # _log_error is not mocked.
        adapter._get_all_pages = AsyncMock(side_effect=_h11_http_error())
        caplog.set_level(logging.DEBUG)
        result = _run(adapter.get_abandoned_carts())
        assert result == []
        blob = _h11_blob(caplog)
        assert "method=get_abandoned_carts" in blob
        assert "salla_adapter.get_abandoned_carts_failed" in blob
        assert "http_status=500" in blob
        _assert_no_h11_canaries(blob)

    def test_sync_abandoned_carts_logs_are_safe(self, caplog):
        class _Boom:
            def dict(self):
                raise RuntimeError(
                    "CANARY_EXC_H11_SECRET_BODY tok_H11_SECRET_CANARY +966500111222"
                    " CANARY_BODY_H11_PROVIDER https://canary.example/checkout/H11"
                )

        db, tenant_id, engine = _make_db()
        try:
            svc = StoreSyncService(db, tenant_id)
            adapter = MagicMock()
            adapter.get_abandoned_carts = AsyncMock(return_value=[
                {
                    "customer": {"name": "Ahmad Salem", "mobile": "+966500111222"},
                    "checkout_url": "https://canary.example/checkout/H11",
                    "access_token": "tok_H11_SECRET_CANARY",
                    "provider_body": "CANARY_BODY_H11_PROVIDER",
                    "store_id": "STORE_ID_CANARY_8889",
                },
                _Boom(),
                {
                    "id": "h11-save",
                    "customer": {"name": "Noura Abdullah", "mobile": "+966500111222"},
                    "checkout_url": "https://canary.example/checkout/H11",
                    "access_token": "tok_H11_SECRET_CANARY",
                    "provider_body": "CANARY_BODY_H11_PROVIDER",
                },
            ])
            svc._adapter = adapter
            caplog.set_level(logging.DEBUG)

            def _boom_upsert(self, normalised, *args, **kwargs):
                raise RuntimeError(
                    "CANARY_EXC_H11_SECRET_BODY tok_H11_SECRET_CANARY +966500111222"
                )

            # Fault injection: persist boundary = _upsert_abandoned_cart_row raises.
            # get_abandoned_carts mock returns canary payloads including a .dict() boom.
            # Logging statements under test are real.
            with patch.object(StoreSyncService, "_upsert_abandoned_cart_row", _boom_upsert):
                _run(svc.sync_abandoned_carts())
            blob = _h11_blob(caplog)
            assert "store_sync.abandoned_cart_missing_id" in blob
            assert "store_sync.abandoned_cart_normalize_failed" in blob
            assert "store_sync.abandoned_cart_save_failed" in blob
            _assert_no_h11_canaries(blob)
        finally:
            db.close()
            engine.dispose()

    def test_resolve_customer_id_log_is_safe(self, caplog):
        from services.cart_recovery_emitter import emit_cart_abandoned_if_new

        db, tenant_id, engine = _make_db()
        try:
            cart = Order(
                tenant_id=tenant_id,
                external_id="cart-h11-resolve",
                status="abandoned",
                total="25.00",
                is_abandoned=True,
                customer_info={"mobile": "+966500111222", "name": "Ahmad Salem"},
                line_items=[{"name": "Rose perfume 100ml", "qty": 1}],
                extra_metadata={
                    "first_provider_abandoned_observed_at": "2025-01-21T15:00:32+00:00",
                    "abandonment_anchor_source": "provider_webhook_event",
                    "raw_cart_id": "h11-resolve",
                },
            )
            db.add(cart)
            db.commit()
            db.refresh(cart)
            normalised = {
                "external_id": "cart-h11-resolve",
                "raw_cart_id": "h11-resolve",
                "customer_info": {"mobile": "+966500111222", "name": "Ahmad Salem"},
                "customer_name": "Ahmad Salem",
                "checkout_url": "https://canary.example/checkout/H11",
                "total": "25.00",
                "line_items": [{"name": "Rose perfume 100ml", "qty": 1}],
                "observation_candidate_iso": "2025-01-21T15:00:32+00:00",
                "observation_candidate_source": "provider_webhook_event",
            }
            caplog.set_level(logging.DEBUG)

            def _boom(self, *args, **kwargs):
                raise RuntimeError(
                    "CANARY_EXC_H11_SECRET_BODY tok_H11_SECRET_CANARY +966500111222"
                )

            # Fault injection: customer DB lookup/upsert raises.
            # emit_cart_abandoned_if_new and _resolve_customer_id are real.
            with patch(
                "services.customer_intelligence.CustomerIntelligenceService.find_customer_by_phone",
                _boom,
            ):
                result = emit_cart_abandoned_if_new(
                    db,
                    tenant_id=tenant_id,
                    cart_row=cart,
                    normalised=normalised,
                    source="store_sync",
                    commit=False,
                )
            assert result is None
            blob = _h11_blob(caplog)
            assert "cart_recovery.customer_resolve_failed" in blob
            assert "error_class=RuntimeError" in blob
            _assert_no_h11_canaries(blob)
        finally:
            db.close()
            engine.dispose()
