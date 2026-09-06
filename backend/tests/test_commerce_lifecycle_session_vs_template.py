"""
Slice A — lifecycle open-window session BODY vs closed-window Meta template.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
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
    dispatch_external_lifecycle_notification,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.ledger import (  # noqa: E402
    normalize_send_method,
    mark_send_sending,
    reserve_send_decision,
    try_conditional_reclaim_send_row,
)
from core.wa_usage import has_open_service_window  # noqa: E402
from models import (  # noqa: E402
    CommerceLifecycleNotificationLedger,
    WaConversationWindow,
)


def _make_db(*tables) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved: list = []
    for model in tables:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine)
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
            {"type": "HEADER", "format": "TEXT", "text": "تأكيد"},
            {
                "type": "BUTTONS",
                "buttons": [{"type": "URL", "url": "https://example.com/{{1}}"}],
            },
        ],
    )


def _run_async(coro):
    return asyncio.run(coro)


def _configure_allowlists(monkeypatch) -> None:
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "0")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", "20")
    monkeypatch.setenv(
        "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST",
        "+966500111222",
    )


def _dispatch_kwargs(db, order):
    return dict(
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
        raw_payload={"event_id": "evt-slice-a", "updated_at": "2026-07-30T10:00:00Z"},
    )


@pytest.fixture(autouse=True)
def _pilot_env(monkeypatch):
    _configure_allowlists(monkeypatch)


class TestHasOpenServiceWindowInboundOnly:
    def test_inbound_service_window_opens_and_outbound_marketing_does_not(self):
        """
        has_open_service_window depends on WaConversationWindow.category==service
        (opened by inbound customer messages via track_conversation), not on a
        prior outbound/marketing window.
        """
        db, _ = _make_db(WaConversationWindow)
        phone = "+966500111222"
        now = datetime.utcnow()

        # Outbound / marketing window alone must NOT unlock session text.
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone=phone,
                window_start=now,
                category="marketing",
            )
        )
        db.commit()
        assert has_open_service_window(db, 20, phone, now=now) is False

        # Simulate inbound customer message opening a service window.
        row = db.query(WaConversationWindow).filter_by(tenant_id=20, customer_phone=phone).one()
        row.category = "service"
        row.window_start = now
        db.commit()
        assert has_open_service_window(db, 20, phone, now=now) is True

        # Expired service window closes free-form path.
        row.window_start = now - timedelta(hours=25)
        db.commit()
        assert has_open_service_window(db, 20, phone, now=now) is False


class TestSendMethodNormalize:
    def test_nullable_and_closed_values(self):
        assert normalize_send_method(None) is None
        assert normalize_send_method("") is None
        assert normalize_send_method("session_message") == "session_message"
        assert normalize_send_method("approved_template") == "approved_template"
        with pytest.raises(ValueError):
            normalize_send_method("both")


class TestOpenClosedWindowDispatch:
    @patch("core.automation_engine.send_lifecycle_whatsapp_session_body", new_callable=AsyncMock)
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_open_window_sends_session_body_only(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_template_send,
        mock_session_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_session_send.return_value = ("sent", {"wa_message_id": "wamid.session.1"})

        db, _ = _make_db(CommerceLifecycleNotificationLedger, WaConversationWindow)
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone="+966500111222",
                window_start=datetime.utcnow(),
                category="service",
            )
        )
        db.commit()

        result = _run_async(
            dispatch_external_lifecycle_notification(**_dispatch_kwargs(db, _generic_order()))
        )
        assert result.dispatched is True
        mock_session_send.assert_awaited_once()
        mock_template_send.assert_not_awaited()

        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_method == "session_message"
        assert (row.dispatch_decision_json or {}).get("send_method") == "session_message"
        assert row.send_state == "sent"

    @patch("core.automation_engine.send_lifecycle_whatsapp_session_body", new_callable=AsyncMock)
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_closed_window_sends_approved_template_only(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_template_send,
        mock_session_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_template_send.return_value = ("sent", {"wa_message_id": "wamid.tpl.1"})

        db, _ = _make_db(CommerceLifecycleNotificationLedger, WaConversationWindow)
        result = _run_async(
            dispatch_external_lifecycle_notification(**_dispatch_kwargs(db, _generic_order()))
        )
        assert result.dispatched is True
        mock_template_send.assert_awaited_once()
        mock_session_send.assert_not_awaited()

        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_method == "approved_template"
        assert (row.dispatch_decision_json or {}).get("send_method") == "approved_template"

    @patch("core.automation_engine.send_lifecycle_whatsapp_session_body", new_callable=AsyncMock)
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_no_approved_template_blocks_even_when_window_open(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_template_send,
        mock_session_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = None

        db, _ = _make_db(CommerceLifecycleNotificationLedger, WaConversationWindow)
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone="+966500111222",
                window_start=datetime.utcnow(),
                category="service",
            )
        )
        db.commit()

        result = _run_async(
            dispatch_external_lifecycle_notification(**_dispatch_kwargs(db, _generic_order()))
        )
        assert result.dispatched is False
        assert result.reason_code == "no_approved_template"
        mock_session_send.assert_not_awaited()
        mock_template_send.assert_not_awaited()
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "blocked"
        assert row.send_error_code == "no_approved_template"
        assert row.send_method is None

    @patch("core.automation_engine.send_lifecycle_whatsapp_session_body", new_callable=AsyncMock)
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_single_path_idempotency_blocks_second_send(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_template_send,
        mock_session_send,
    ):
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_session_send.return_value = ("sent", {"wa_message_id": "wamid.session.once"})

        db, _ = _make_db(CommerceLifecycleNotificationLedger, WaConversationWindow)
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone="+966500111222",
                window_start=datetime.utcnow(),
                category="service",
            )
        )
        db.commit()

        kwargs = _dispatch_kwargs(db, _generic_order())
        first = _run_async(dispatch_external_lifecycle_notification(**kwargs))
        # Close window so a naive second attempt would prefer template — still blocked.
        win = db.query(WaConversationWindow).one()
        win.category = "marketing"
        db.commit()
        mock_template_send.return_value = ("sent", {"wa_message_id": "wamid.tpl.dup"})

        second = _run_async(dispatch_external_lifecycle_notification(**kwargs))

        assert first.dispatched is True
        assert second.duplicate is True
        mock_session_send.assert_awaited_once()
        mock_template_send.assert_not_awaited()
        rows = db.query(CommerceLifecycleNotificationLedger).all()
        assert len(rows) == 1
        assert rows[0].send_method == "session_message"


class TestConditionalReclaimWithSendMethod:
    def test_conditional_reclaim_preserves_single_path(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "1")
        db, _ = _make_db(CommerceLifecycleNotificationLedger)
        reserved = reserve_send_decision(
            db,
            tenant_id=20,
            order_id=501,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-reclaim-a",
            transition_version="v1",
            template_service_key="order_confirmation",
            dispatch_decision={
                "send_method": "approved_template",
                "service_key": "order_confirmation",
            },
            commit=True,
        )
        mark_send_sending(
            db,
            ledger_id=reserved.ledger_id,
            tenant_id=20,
            template_name="order_confirmed_generic_ar",
            template_service_key="order_confirmation",
            send_method="approved_template",
            commit=True,
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        stale = datetime.utcnow() - timedelta(hours=2)
        row.send_reserved_at = stale
        row.send_attempted_at = stale
        db.commit()

        recovered = try_conditional_reclaim_send_row(
            db,
            tenant_id=20,
            ledger_id=reserved.ledger_id,
        )
        db.commit()
        assert recovered is True
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "reserved"
        assert row.send_method == "approved_template"
        assert row.send_method != "session_message"


class TestRenderApprovedBodyOnly:
    def test_render_uses_body_not_header_or_buttons(self):
        from core.automation_engine import render_lifecycle_approved_body  # noqa: PLC0415

        db = MagicMock()
        with patch("core.automation_engine._resolve_store_name", return_value="متجر تجريبي عام"):
            with patch(
                "core.automation_engine._build_template_vars",
                return_value={"{{1}}": "أحمد سالم", "{{2}}": "ORD-8801"},
            ):
                text = render_lifecycle_approved_body(
                    db,
                    20,
                    _approved_template(),
                    {"order_number": "ORD-8801"},
                    customer_name="أحمد سالم",
                )
        assert "مرحبا أحمد سالم طلب ORD-8801" == text
        assert "تأكيد" not in text
        assert "example.com" not in text
