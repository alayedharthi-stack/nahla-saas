"""
Platform lifecycle dispatch — ledger gate, dispatcher, Moyasar replay.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.dispatch import (  # noqa: E402
    commerce_lifecycle_dispatch_enabled,
    commerce_lifecycle_dispatch_recipient_permitted,
    commerce_lifecycle_dispatch_tenant_permitted,
    dispatch_external_lifecycle_notification,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.ledger import (  # noqa: E402
    SendLedgerOutcome,
    finalize_send_outcome,
    mark_send_sending,
    reserve_send_decision,
    try_conditional_reclaim_send_row,
)
from models import (  # noqa: E402
    CommerceLifecycleNotificationLedger,
    Integration,
    Order,
    TenantSettings,
    WaConversationWindow,
    WebhookEvent,
)


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved: list = []
    for model in (CommerceLifecycleNotificationLedger, WaConversationWindow, TenantSettings):
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _generic_order(**kwargs):
    defaults = dict(
        id=501,
        external_id="salla-ord-8801",
        external_order_number="ORD-8801",
        status="under_review",
        checkout_url="https://shop.generic.example/checkout/8801",
        customer_name="أحمد سالم",
        customer_info={"phone": "+966500111222"},
        extra_metadata={"payment_method": "cod"},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _merchant_caps():
    return SimpleNamespace(
        to_dict=lambda: {
            "has_external_store": True,
            "supports_external_checkout": True,
            "supports_external_coupons": False,
            "supports_whatsapp_orders": True,
            "supports_nahla_orders": False,
            "supports_bank_transfer": False,
            "supports_cod": True,
            "has_whatsapp_catalog": False,
            "has_external_tracking": True,
            "has_nahla_tracking": False,
            "has_payment_link": True,
        }
    )


def _approved_template():
    return SimpleNamespace(
        id=11,
        name="order_confirmed_generic_ar",
        language="ar",
        components=[
            {"type": "BODY", "text": "مرحبا {{1}} طلب {{2}}"},
        ],
    )


def _run_async(coro):
    return asyncio.run(coro)


def _configure_lifecycle_dispatch_pilot_allowlists(monkeypatch, *, tenants: str, recipients: str) -> None:
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", tenants)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", recipients)


def _ensure_webhook_dedupe_index(db) -> None:
    """Mirror migration 0023 partial unique index for SQLite test idempotency."""
    from sqlalchemy import text  # noqa: PLC0415

    db.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event
            ON webhook_events (provider, external_event_id)
            WHERE external_event_id IS NOT NULL
            """
        )
    )
    db.commit()


def _seed_salla_integration(db, tenant_id: int, store_id: str) -> Integration:
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=store_id,
        config={
            "api_key": "test-key-generic",
            "store_id": store_id,
            "app_type": "easy",
            "api_sync_enabled": False,
        },
        enabled=True,
    )
    db.add(intg)
    db.commit()
    db.refresh(intg)
    return intg


def _generic_salla_order_created_payload(*, store_id: str) -> tuple[dict, dict]:
    order_data = {
        "id": 8801001,
        "reference_id": "ORD-GEN-8801",
        "status": {"slug": "under_review", "name": "بانتظار المراجعة"},
        "total": {"amount": 249, "currency": "SAR"},
        "customer": {
            "id": 99101,
            "name": "نورة عبدالله",
            "mobile": "+966500222333",
        },
        "items": [
            {
                "name": "حذاء رياضي أبيض",
                "quantity": 1,
                "price": {"amount": 249, "currency": "SAR"},
            }
        ],
        "updated_at": "2026-07-30T10:00:00Z",
        "event_id": "salla-trans-8801",
    }
    parsed_payload = {
        "event": "order.created",
        "merchant": store_id,
        "data": order_data,
    }
    return order_data, parsed_payload


@pytest.fixture(autouse=True)
def _enable_dispatch_flag(monkeypatch):
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "0")
    _configure_lifecycle_dispatch_pilot_allowlists(
        monkeypatch,
        tenants="1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50",
        recipients="+966500111222,+966500222333",
    )


class TestDispatchFlag:
    def test_dispatch_flag_defaults_false(self, monkeypatch):
        monkeypatch.delenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", raising=False)
        assert commerce_lifecycle_dispatch_enabled() is False

    def test_dispatch_flag_on(self):
        assert commerce_lifecycle_dispatch_enabled() is True


class TestReserveSendLedger:
    def test_duplicate_reservation_blocks_resend_states(self):
        db, _ = _make_db()
        first = reserve_send_decision(
            db,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        finalize_send_outcome(
            db,
            ledger_id=first.ledger_id,
            tenant_id=10,
            outcome=SendLedgerOutcome.AMBIGUOUS,
            send_error_code="provider_empty_response",
            commit=True,
        )
        second = reserve_send_decision(
            db,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        assert second.duplicate is True

    def test_independent_session_reservation_is_duplicate(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "300")
        db1, engine = _make_db()
        Session = sessionmaker(bind=engine)
        db2 = Session()

        first = reserve_send_decision(
            db1,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-session",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        db1.close()

        second = reserve_send_decision(
            db2,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-session",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        db2.close()

        assert first.duplicate is False
        assert second.duplicate is True
        verify_db = Session()
        assert verify_db.query(CommerceLifecycleNotificationLedger).count() == 1
        verify_db.close()

    def test_stale_reserved_row_is_reclaimed(self):
        db, _ = _make_db()
        reserve = reserve_send_decision(
            db,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-stale",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        assert try_conditional_reclaim_send_row(
            db,
            tenant_id=10,
            ledger_id=reserve.ledger_id,
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.reclaim_count == 1
        assert row.send_state == "reserved"

    def test_reclaim_blocked_when_send_attempts_exhausted(self):
        db, _ = _make_db()
        reserve = reserve_send_decision(
            db,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-attempts",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        row.send_state = "sending"
        row.send_attempt_count = 2
        db.commit()
        assert not try_conditional_reclaim_send_row(
            db,
            tenant_id=10,
            ledger_id=reserve.ledger_id,
        )

    def test_reclaim_blocked_when_provider_message_id_present(self):
        db, _ = _make_db()
        reserve = reserve_send_decision(
            db,
            tenant_id=10,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-wamid",
            transition_version="v1",
            template_service_key="order_confirmation",
            commit=True,
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        row.send_state = "sending"
        row.provider_message_id = "wamid.lifecycle.existing"
        db.commit()
        assert not try_conditional_reclaim_send_row(
            db,
            tenant_id=10,
            ledger_id=reserve.ledger_id,
        )




class TestLifecycleDispatchPilotAllowlists:
    def test_master_flag_false_skips_dispatch(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "false")
        _configure_lifecycle_dispatch_pilot_allowlists(
            monkeypatch,
            tenants="20",
            recipients="+966500111222",
        )
        db, _ = _make_db()
        order = _generic_order()
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-disabled", "updated_at": "2026-07-30T10:00:00Z"},
            )
        )
        assert result.outcome == "disabled"
        assert result.reason_code == "dispatch_disabled"

    def test_master_flag_true_empty_tenant_allowlist_blocks(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        _configure_lifecycle_dispatch_pilot_allowlists(monkeypatch, tenants="", recipients="+966500111222")
        assert commerce_lifecycle_dispatch_tenant_permitted(20) is False

    def test_tenant_not_on_allowlist_blocks_before_reserve(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        _configure_lifecycle_dispatch_pilot_allowlists(
            monkeypatch,
            tenants="99",
            recipients="+966500111222",
        )
        db, _ = _make_db()
        order = _generic_order()
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-tenant", "updated_at": "2026-07-30T10:00:00Z"},
            )
        )
        assert result.reason_code == "tenant_not_allowlisted"
        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_tenant_and_recipient_allowlisted_dispatches(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        monkeypatch,
    ):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        _configure_lifecycle_dispatch_pilot_allowlists(
            monkeypatch,
            tenants="20",
            recipients="+966500111222",
        )
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.allowlist.001"})

        db, _ = _make_db()
        order = _generic_order()
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-allow", "updated_at": "2026-07-30T10:00:00Z"},
            )
        )
        assert result.dispatched is True
        mock_send.assert_awaited_once()

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_recipient_not_allowlisted_blocks_without_provider(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        monkeypatch,
    ):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        _configure_lifecycle_dispatch_pilot_allowlists(
            monkeypatch,
            tenants="20",
            recipients="+966500999888",
        )
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()

        db, _ = _make_db()
        order = _generic_order(customer_info={"phone": "+966500111222"})
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-recipient", "updated_at": "2026-07-30T10:00:00Z"},
            )
        )
        assert result.reason_code == "recipient_not_allowlisted"
        mock_send.assert_not_awaited()
        assert commerce_lifecycle_dispatch_recipient_permitted("+966500111222") is False


class TestLifecycleDispatcher:
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_confirmation_dispatches_once_on_replay(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.test.001"})

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        kwargs = dict(
            db=db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status=None,
            raw_current_status="under_review",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "under_review",
                "external_order_number": "ORD-8801",
            },
            raw_payload={"event_id": "evt-confirm", "updated_at": "2026-07-30T10:00:00Z"},
        )

        first = _run_async(dispatch_external_lifecycle_notification(**kwargs))
        second = _run_async(dispatch_external_lifecycle_notification(**kwargs))

        assert first.dispatched is True
        assert first.provider_message_id == "wamid.test.001"
        assert second.duplicate is True
        assert mock_send.await_count == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "sent"
        assert row.template_service_key == "order_confirmation"

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_shipping_requires_tracking_evidence(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.test.002"})

        db, _ = _make_db()
        order = _generic_order(status="shipped")
        result = _run_async(
            dispatch_external_lifecycle_notification(
            db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "shipped",
                "external_order_number": "ORD-8801",
            },
            raw_payload={"event_id": "evt-ship", "updated_at": "2026-07-30T11:00:00Z"},
            )
        )
        assert result.dispatched is False
        assert result.reason_code == "missing_tracking_evidence"
        assert mock_send.await_count == 0

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_shipping_with_tracking_sends(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.test.003"})

        db, _ = _make_db()
        order = _generic_order(
            status="shipped",
            extra_metadata={
                "tracking_number": "RRRD1234",
                "payment_method": "cod",
            },
        )
        result = _run_async(
            dispatch_external_lifecycle_notification(
            db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "shipped",
                "external_order_number": "ORD-8801",
            },
            raw_payload={
                "event_id": "evt-ship-ok",
                "updated_at": "2026-07-30T11:05:00Z",
                "shipping": {
                    "tracking_link": "https://tracking.shipco.io/track/RRRD1234",
                },
            },
            )
        )
        assert result.dispatched is True
        assert mock_send.await_count == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.template_service_key == "shipping_tracking"

    @patch("core.commerce_lifecycle.order_updates.evaluate_order_update_delivery", return_value=(True, None))
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_out_for_delivery_dispatches_dedicated_template(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_flags,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.ofd"})

        db, _ = _make_db()
        order = _generic_order(
            status="out_for_delivery",
            extra_metadata={"tracking_number": "RRRD5678", "payment_method": "cod"},
        )
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status="shipped",
                raw_current_status="out_for_delivery",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "out_for_delivery",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={
                    "event_id": "evt-ofd",
                    "updated_at": "2026-07-30T12:00:00Z",
                    "shipping": {
                        "tracking_link": "https://tracking.shipco.io/track/RRRD5678",
                    },
                },
            )
        )
        assert result.dispatched is True
        assert mock_send.await_count == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.template_service_key == "out_for_delivery"

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_no_approved_template_blocks_without_provider_call(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = None

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-no-tpl", "updated_at": "2026-07-30T12:10:00Z"},
            )
        )
        assert result.reason_code == "no_approved_template"
        assert mock_send.await_count == 0
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "blocked"
        assert row.send_error_code == "no_approved_template"
        assert row.outcome == SendLedgerOutcome.SEND_BLOCKED.value

    @patch("core.commerce_lifecycle.dispatch.mark_send_sending")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_mark_sending_before_provider_call(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        mock_mark_sending,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.test.order"})
        call_order: list[str] = []

        def _mark_side_effect(*args, **kwargs):
            call_order.append("mark_sending")
            return mark_send_sending(*args, **kwargs)

        async def _send_side_effect(*args, **kwargs):
            call_order.append("provider_send")
            return ("sent", {"wa_message_id": "wamid.test.order"})

        mock_mark_sending.side_effect = _mark_side_effect
        mock_send.side_effect = _send_side_effect

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-send-order", "updated_at": "2026-07-30T12:15:00Z"},
            )
        )
        assert result.dispatched is True
        assert call_order == ["mark_sending", "provider_send"]

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_stale_reserved_dispatch_recovers_and_sends(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.side_effect = [
            RuntimeError("template resolver exploded"),
            _approved_template(),
        ]
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.test.recover"})

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        kwargs = dict(
            db=db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status=None,
            raw_current_status="under_review",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "under_review",
                "external_order_number": "ORD-8801",
            },
            raw_payload={"event_id": "evt-recover", "updated_at": "2026-07-30T12:20:00Z"},
        )
        first = _run_async(dispatch_external_lifecycle_notification(**kwargs))
        second = _run_async(dispatch_external_lifecycle_notification(**kwargs))

        assert first.outcome == "error"
        assert first.ledger_id is not None
        assert second.dispatched is True
        assert second.recovered is True
        assert mock_send.await_count == 1
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "sent"
        assert row.reclaim_count >= 1

    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_exception_after_reserve_finalizes_recoverable_ledger(
        self,
        mock_caps,
        mock_resolve_tpl,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.side_effect = RuntimeError("template resolver exploded")

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "under_review",
                    "external_order_number": "ORD-8801",
                },
                raw_payload={"event_id": "evt-crash", "updated_at": "2026-07-30T12:25:00Z"},
            )
        )
        assert result.ledger_id is not None
        assert result.outcome == "error"
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state in {"reserved", "sending"}
        assert row.send_error_code == "dispatch_error"

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_tenant_isolation_independent_ledger_and_send(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.tenant"})

        db, _ = _make_db()
        shared_payload = {
            "event_id": "evt-tenant-shared",
            "updated_at": "2026-07-30T12:30:00Z",
        }
        shared_normalized = {
            "external_id": "shared-ext-900",
            "status": "under_review",
            "external_order_number": "ORD-SHARED",
        }
        order_a = _generic_order(id=601, external_id="shared-ext-900")
        order_b = _generic_order(id=602, external_id="shared-ext-900")

        result_a = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order_a,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order=shared_normalized,
                raw_payload=shared_payload,
            )
        )
        result_b = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=21,
                order=order_b,
                provider="salla",
                raw_previous_status=None,
                raw_current_status="under_review",
                normalized_order=shared_normalized,
                raw_payload=shared_payload,
            )
        )

        assert result_a.dispatched is True
        assert result_b.dispatched is True
        assert mock_send.await_count == 2
        rows = db.query(CommerceLifecycleNotificationLedger).all()
        assert len(rows) == 2
        assert {row.tenant_id for row in rows} == {20, 21}
        assert len({row.idempotency_key for row in rows}) == 2

    @patch("core.commerce_lifecycle.order_updates.evaluate_order_update_delivery", return_value=(True, None))
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_payment_needed_dispatches_payment_pending(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_flags,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.pay"})

        db, _ = _make_db()
        order = _generic_order(status="payment_pending", extra_metadata={"payment_method": "bank"})
        result = _run_async(
            dispatch_external_lifecycle_notification(
            db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="payment_pending",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "payment_pending",
                "external_order_number": "ORD-8801",
                "payment_method": "bank_transfer",
            },
            raw_payload={"event_id": "evt-pay", "updated_at": "2026-07-30T12:30:00Z"},
            )
        )
        assert result.dispatched is True
        assert mock_send.await_count == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.template_service_key == "payment_pending"

    @patch("core.commerce_lifecycle.order_updates.evaluate_order_update_delivery", return_value=(True, None))
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_missing_payment_confirmed_template_fails_closed(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_flags,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = None
        db, _ = _make_db()
        order = _generic_order(status="paid", extra_metadata={"payment_method": "bank"})
        result = _run_async(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status="payment_pending",
                raw_current_status="paid",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "paid",
                    "external_order_number": "ORD-8801",
                    "payment_method": "bank_transfer",
                },
                raw_payload={"event_id": "evt-paid", "updated_at": "2026-07-30T12:40:00Z"},
            )
        )
        assert result.dispatched is False
        assert result.reason_code == "no_approved_template"
        assert mock_send.await_count == 0

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_ambiguous_send_not_retried(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("ambiguous", {"error_code": "provider_empty_response"})

        db, _ = _make_db()
        order = _generic_order(status="under_review")
        kwargs = dict(
            db=db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status=None,
            raw_current_status="under_review",
            normalized_order={
                "external_id": "salla-ord-8801",
                "status": "under_review",
                "external_order_number": "ORD-8801",
            },
            raw_payload={"event_id": "evt-ambig", "updated_at": "2026-07-30T13:00:00Z"},
        )
        first = _run_async(dispatch_external_lifecycle_notification(**kwargs))
        second = _run_async(dispatch_external_lifecycle_notification(**kwargs))

        assert first.outcome == SendLedgerOutcome.AMBIGUOUS.value
        assert second.duplicate is True
        assert mock_send.await_count == 1
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "ambiguous"


class TestMoyasarReplay:
    def _run_moyasar(self, *, order_status: str, ps_status: str):
        from routers.webhooks import moyasar_webhook  # noqa: PLC0415

        order = SimpleNamespace(
            id=901,
            tenant_id=30,
            status=order_status,
            customer_info={"phone": "+966500333444"},
        )
        ps = SimpleNamespace(status=ps_status, callback_data=None, updated_at=None)

        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [ps, order, None]

        emit_mock = MagicMock()
        request = MagicMock()
        request.body = AsyncMock(return_value=b"{}")
        request.json = AsyncMock(
            return_value={
                "id": "pay-replay-1",
                "status": "paid",
                "amount": 15000,
                "metadata": {"tenant_id": 30, "order_id": "901"},
            }
        )
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="")

        with patch("routers.webhooks.get_moyasar_settings", return_value={"webhook_secret": ""}):
            with patch("core.automation_engine.emit_automation_event", emit_mock):
                with patch("services.offer_attribution_service.attribute_order_to_decision"):
                    with patch("observability.event_logger.log_event"):
                        _run_async(moyasar_webhook(request, db=db))
        return emit_mock

    def test_first_paid_callback_emits_order_paid_once(self):
        emit_mock = self._run_moyasar(order_status="pending", ps_status="pending")
        emit_mock.assert_called_once()
        assert emit_mock.call_args.args[2] == "order_paid"

    def test_repeated_paid_callback_skips_order_paid_emit(self):
        emit_mock = self._run_moyasar(order_status="paid", ps_status="paid")
        emit_mock.assert_not_called()


class TestSallaLifecycleIngressIntegration:
    """
    Integrated non-live route:

    persist_event → claim_next_batch → _process_event/_dispatch_salla
    → StoreSync.handle_order_webhook → lifecycle dispatch.

    HTTP ingress is not mounted here; ``persist_event`` plus the real
    dispatcher handler is the one unavoidable test boundary.
    """

    @patch("services.outcome_tracker.record_order_outcome")
    @patch("services.offer_attribution_service.attribute_order_to_decision")
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_salla_queue_dispatch_store_sync_replay_single_provider_call(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        _mock_attr,
        _mock_outcome,
        monkeypatch,
    ):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "300")
        _configure_lifecycle_dispatch_pilot_allowlists(
            monkeypatch,
            tenants="1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50",
            recipients="+966500222333",
        )

        from commerce_scenario_fixtures import make_scenario_db, seed_tenant  # noqa: PLC0415
        from core.webhook_dispatcher import _process_event  # noqa: PLC0415
        from core.webhook_events import claim_next_batch, persist_event  # noqa: PLC0415

        db, _engine = make_scenario_db()
        _ensure_webhook_dedupe_index(db)

        tenant = seed_tenant(db, name="متجر تجريبي عام")
        store_id = "STORE-GENERIC-8801"
        _seed_salla_integration(db, tenant.id, store_id)

        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.integrated.001"})

        _order_data, parsed_payload = _generic_salla_order_created_payload(store_id=store_id)
        external_event_id = "salla-wh-evt-8801"

        first_persist = persist_event(
            db,
            provider="salla",
            raw_body='{"event":"order.created"}',
            parsed_payload=parsed_payload,
            event_type="order.created",
            external_event_id=external_event_id,
            store_id=store_id,
        )
        assert first_persist.status == "received"

        batch = claim_next_batch(db, limit=5)
        assert len(batch) == 1
        assert batch[0].id == first_persist.id

        _run_async(_process_event(db, batch[0]))

        db.refresh(first_persist)
        assert first_persist.status == "processed"
        assert mock_send.await_count == 1

        order = db.query(Order).filter_by(tenant_id=tenant.id).one()
        assert order.external_id == "8801001"
        assert order.external_order_number == "ORD-GEN-8801"
        assert order.status == "under_review"
        assert order.customer_info.get("phone") == "+966500222333"

        ledger = db.query(CommerceLifecycleNotificationLedger).filter_by(
            tenant_id=tenant.id,
            order_id=order.id,
        ).one()
        assert ledger.send_state == "sent"
        assert ledger.provider_message_id == "wamid.integrated.001"
        assert ledger.template_service_key == "order_confirmation"

        replay_persist = persist_event(
            db,
            provider="salla",
            raw_body='{"event":"order.created"}',
            parsed_payload=parsed_payload,
            event_type="order.created",
            external_event_id=external_event_id,
            store_id=store_id,
        )
        assert replay_persist.id == first_persist.id
        assert db.query(WebhookEvent).count() == 1

        db.refresh(replay_persist)
        _run_async(_process_event(db, replay_persist))

        assert mock_send.await_count == 1
        ledger_after = db.query(CommerceLifecycleNotificationLedger).filter_by(
            tenant_id=tenant.id,
            order_id=order.id,
        ).one()
        assert ledger_after.send_state == "sent"
        assert ledger_after.provider_message_id == "wamid.integrated.001"
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1
