"""PR #955 owner-review remediations: shipping owner, settings retry, COD bind, identity."""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

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

from core.automation_engine import _execute_action  # noqa: E402
from core.commerce_lifecycle.canary_guard import (  # noqa: E402
    REASON_LIFECYCLE_OWNS_EVENT,
    lifecycle_dispatch_owns_legacy_send,
)
from core.commerce_lifecycle.dispatch import (  # noqa: E402
    dispatch_external_lifecycle_notification,
)
from core.commerce_lifecycle.order_updates import (  # noqa: E402
    REASON_ORDER_UPDATE_DISABLED,
    REASON_SETTINGS_UNAVAILABLE,
    evaluate_order_update_delivery,
    set_order_update_flags,
)
from models import (  # noqa: E402
    CommerceLifecycleNotificationLedger,
    TenantSettings,
    WaConversationWindow,
)
from services.cod_confirmation import (  # noqa: E402
    classify_cod_reply,
    handle_cod_reply,
    parse_cod_button_payload,
)
from store_integration.lifecycle_normalization import (  # noqa: E402
    build_transition_identity,
)


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    use = models or (
        CommerceLifecycleNotificationLedger,
        WaConversationWindow,
        TenantSettings,
    )
    for model in use:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _run(coro):
    return asyncio.run(coro)


def _enable_dispatch(monkeypatch, *, tenants="20", recipients="+966500111222"):
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", tenants)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", recipients)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "300")


def _caps():
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


def _tpl():
    return SimpleNamespace(
        id=11,
        name="shipping_tracking_generic_ar",
        language="ar",
        components=[{"type": "BODY", "text": "مرحبا {{1}} طلب {{2}}"}],
    )


def _ship_order(**kwargs):
    defaults = dict(
        id=8801,
        external_id="salla-ord-8801",
        external_order_number="ORD-8801",
        status="shipped",
        checkout_url="https://shop.generic.example/checkout/8801",
        customer_name="أحمد سالم",
        customer_info={"phone": "+966500111222"},
        extra_metadata={
            "payment_method": "cod",
            "tracking_number": "RRRD1234",
        },
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestShippingSingleOwner:
    def test_lifecycle_owns_legacy_order_shipped(self, monkeypatch):
        _enable_dispatch(monkeypatch)
        assert lifecycle_dispatch_owns_legacy_send(
            20, event_type="order_shipped"
        ) is True
        assert lifecycle_dispatch_owns_legacy_send(
            20, automation_type="shipping_update"
        ) is True
        assert lifecycle_dispatch_owns_legacy_send(
            20, automation_type="customer_winback"
        ) is False
        assert lifecycle_dispatch_owns_legacy_send(
            33, event_type="order_shipped"
        ) is False

    def test_engine_skips_legacy_order_shipped_when_dispatch_owns(self, monkeypatch):
        _enable_dispatch(monkeypatch, tenants="1")
        monkeypatch.setattr("core.billing.has_billing_access", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "core.wa_usage.check_limit",
            lambda *_a, **_k: SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=99
            ),
        )
        customer = SimpleNamespace(
            id=9, phone="+966500111222", tenant_id=1, name="أحمد سالم"
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = customer
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message", send
        )
        ok, info = _run(
            _execute_action(
                db,
                1,
                SimpleNamespace(
                    id=1,
                    customer_id=9,
                    event_type="order_shipped",
                    payload={"order_id": 88},
                ),
                SimpleNamespace(id=4, automation_type="shipping_update"),
                {},
            )
        )
        assert ok is False
        assert info.get("error_code") == REASON_LIFECYCLE_OWNS_EVENT
        send.assert_not_awaited()

    def test_storesync_and_poller_do_not_provider_send(self):
        store_sync = (BACKEND_DIR / "services" / "store_sync.py").read_text(
            encoding="utf-8"
        )
        poller = (BACKEND_DIR / "services" / "salla_orders_poller.py").read_text(
            encoding="utf-8"
        )
        assert "await provider_send_message" not in store_sync
        assert "_lifecycle_dispatch_owns_tenant" in store_sync
        assert "await provider_send_message" not in poller

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_webhook_and_poller_prev_disagreement_one_send(
        self, mock_caps, mock_resolve, mock_send, monkeypatch
    ):
        _enable_dispatch(monkeypatch)
        mock_caps.return_value = _caps()
        mock_resolve.return_value = _tpl()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.ship.1"})
        db, _ = _make_db()
        set_order_update_flags(db, 20, {"shipping_tracking": True}, commit=True)
        order = _ship_order()
        payload = {
            "shipping": {"tracking_link": "https://tracking.shipco.io/track/RRRD1234"},
        }
        first = _run(
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
                raw_payload={**payload, "event_id": "evt-wh"},
            )
        )
        second = _run(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status="processing",
                raw_current_status="shipped",
                normalized_order={
                    "external_id": "salla-ord-8801",
                    "status": "shipped",
                    "external_order_number": "ORD-8801",
                },
                raw_payload=payload,
            )
        )
        assert first.dispatched is True
        assert second.dispatched is False
        assert mock_send.await_count == 1

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_later_out_for_delivery_is_separate_send(
        self, mock_caps, mock_resolve, mock_send, monkeypatch
    ):
        _enable_dispatch(monkeypatch)
        mock_caps.return_value = _caps()
        mock_resolve.return_value = _tpl()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.ship.2"})
        db, _ = _make_db()
        set_order_update_flags(
            db, 20, {"shipping_tracking": True, "out_for_delivery": True}, commit=True
        )
        order = _ship_order()
        tracking = {
            "shipping": {"tracking_link": "https://tracking.shipco.io/track/RRRD1234"},
        }
        first = _run(
            dispatch_external_lifecycle_notification(
                db,
                tenant_id=20,
                order=order,
                provider="salla",
                raw_previous_status="under_review",
                raw_current_status="shipped",
                normalized_order={"external_id": "salla-ord-8801", "status": "shipped"},
                raw_payload=tracking,
            )
        )
        second = _run(
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
                },
                raw_payload=tracking,
            )
        )
        assert first.dispatched is True
        assert second.dispatched is True
        assert mock_send.await_count == 2

    def test_shipping_disabled_blocks_legacy_engine(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "false")
        monkeypatch.setattr("core.billing.has_billing_access", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "core.wa_usage.check_limit",
            lambda *_a, **_k: SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=99
            ),
        )
        customer = SimpleNamespace(
            id=9, phone="+966500111222", tenant_id=20, name="نورة عبدالله"
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = customer
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message", send
        )
        with patch(
            "core.commerce_lifecycle.order_updates.evaluate_order_update_delivery",
            return_value=(False, REASON_ORDER_UPDATE_DISABLED),
        ):
            ok, info = _run(
                _execute_action(
                    db,
                    20,
                    SimpleNamespace(
                        id=1,
                        customer_id=9,
                        event_type="order_shipped",
                        payload={},
                    ),
                    SimpleNamespace(id=4, automation_type="shipping_update"),
                    {},
                )
            )
        assert ok is False
        assert info.get("error_code") == REASON_ORDER_UPDATE_DISABLED
        send.assert_not_awaited()

    def test_non_allowlisted_recipient_no_legacy_or_lifecycle_send(self, monkeypatch):
        _enable_dispatch(monkeypatch, tenants="1", recipients="+966500111222")
        monkeypatch.setattr("core.billing.has_billing_access", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "core.wa_usage.check_limit",
            lambda *_a, **_k: SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=99
            ),
        )
        customer = SimpleNamespace(
            id=9, phone="+966500999888", tenant_id=1, name="أحمد سالم"
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = customer
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message", send
        )
        ok, info = _run(
            _execute_action(
                db,
                1,
                SimpleNamespace(
                    id=1, customer_id=9, event_type="order_shipped", payload={}
                ),
                SimpleNamespace(id=4, automation_type="shipping_update"),
                {},
            )
        )
        assert ok is False
        send.assert_not_awaited()


class TestSettingsRetryable:
    def test_merchant_off_is_terminal_block(self):
        db, _ = _make_db()
        set_order_update_flags(db, 20, {"shipping_tracking": False}, commit=True)
        allowed, reason = evaluate_order_update_delivery(db, 20, "shipping_tracking")
        assert allowed is False
        assert reason == REASON_ORDER_UPDATE_DISABLED

    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_settings_read_failure_then_recovery_sends_once(
        self, mock_caps, mock_resolve, mock_send, monkeypatch
    ):
        _enable_dispatch(monkeypatch)
        mock_caps.return_value = _caps()
        mock_resolve.return_value = _tpl()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.settings.1"})
        db, _ = _make_db()
        set_order_update_flags(db, 20, {"shipping_tracking": True}, commit=True)
        order = _ship_order()
        kwargs = dict(
            db=db,
            tenant_id=20,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "salla-ord-8801", "status": "shipped"},
            raw_payload={
                "shipping": {
                    "tracking_link": "https://tracking.shipco.io/track/RRRD1234"
                }
            },
        )
        with patch(
            "core.commerce_lifecycle.order_updates.evaluate_order_update_delivery",
            return_value=(False, REASON_SETTINGS_UNAVAILABLE),
        ):
            first = _run(dispatch_external_lifecycle_notification(**kwargs))
        assert first.outcome == "retryable"
        assert first.reason_code == REASON_SETTINGS_UNAVAILABLE
        assert first.ledger_id is None
        assert mock_send.await_count == 0
        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

        second = _run(dispatch_external_lifecycle_notification(**kwargs))
        assert second.dispatched is True
        assert mock_send.await_count == 1

        third = _run(dispatch_external_lifecycle_notification(**kwargs))
        assert third.dispatched is False
        assert mock_send.await_count == 1

    def test_settings_error_does_not_default_on(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        allowed, reason = evaluate_order_update_delivery(db, 20, "shipping_tracking")
        assert allowed is False
        assert reason == REASON_SETTINGS_UNAVAILABLE


class TestSemanticIdentity:
    def test_previous_status_does_not_split_key(self):
        a = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            business_intent="shipment_available",
        )
        b = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="processing",
            raw_current_status="shipped",
            business_intent="shipment_available",
        )
        assert a == b
        src = inspect.getsource(build_transition_identity)
        assert "previous_status" not in src.split("semantic_payload")[1]


class TestCodBinding:
    def test_parse_confirm_cancel_ids(self):
        assert parse_cod_button_payload("nahla_cod_confirm") == ("confirm", None)
        assert parse_cod_button_payload("nahla_cod_confirm:77") == ("confirm", 77)
        assert parse_cod_button_payload("nahla_cod_cancel:77") == ("cancel", 77)
        assert parse_cod_button_payload("nahla_cod_confirmation") == (None, None)
        assert classify_cod_reply("nahla_cod_confirm:77") == "confirm"
        assert classify_cod_reply("متى يصل الطلب؟") is None

    def _pending_query(self, orders):
        class _Query:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return list(orders)

            def first(self):
                return orders[0] if orders else None

        db = MagicMock()
        db.query = lambda *_a, **_k: _Query()
        db.commit = lambda: None
        return db

    def test_interactive_confirm_id_binds(self):
        order = SimpleNamespace(
            id=77,
            tenant_id=9,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={"payment_method": "cod"},
            line_items=[],
            external_id=None,
        )
        db = self._pending_query([order])
        push = AsyncMock(return_value="salla-77")
        with patch("services.cod_confirmation._push_cod_to_store", push), patch(
            "observability.event_logger.log_event", lambda *a, **k: None
        ), patch("services.cod_confirmation.flag_modified", lambda *a, **k: None):
            decision, affected = _run(
                handle_cod_reply(
                    db,
                    tenant_id=9,
                    customer_phone="+966500111222",
                    text="تأكيد الطلب ✅",
                    button_payload="nahla_cod_confirm:77",
                )
            )
        assert decision == "confirm"
        assert affected is order
        assert order.status == "under_review"
        push.assert_awaited_once()

    def test_interactive_cancel_id_does_not_push(self):
        order = SimpleNamespace(
            id=77,
            tenant_id=9,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={"payment_method": "cod"},
            line_items=[],
        )
        db = self._pending_query([order])
        push = AsyncMock(return_value="should-not")
        with patch("services.cod_confirmation._push_cod_to_store", push), patch(
            "observability.event_logger.log_event", lambda *a, **k: None
        ), patch("services.cod_confirmation.flag_modified", lambda *a, **k: None):
            decision, affected = _run(
                handle_cod_reply(
                    db,
                    tenant_id=9,
                    customer_phone="+966500111222",
                    text="إلغاء الطلب ❌",
                    button_payload="nahla_cod_cancel:77",
                )
            )
        assert decision == "cancel"
        assert affected is order
        assert order.status == "cancelled"
        push.assert_not_awaited()

    def test_unrelated_message_does_not_mutate(self):
        order = SimpleNamespace(
            id=77,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={},
        )
        db = self._pending_query([order])
        decision, affected = _run(
            handle_cod_reply(
                db,
                tenant_id=9,
                customer_phone="+966500111222",
                text="متى يصل الطلب؟",
            )
        )
        assert decision is None
        assert affected is None
        assert order.status == "pending_confirmation"

    def test_no_pending_order_no_mutation(self):
        db = self._pending_query([])
        decision, affected = _run(
            handle_cod_reply(
                db,
                tenant_id=9,
                customer_phone="+966500111222",
                text="تأكيد الطلب ✅",
                button_payload="nahla_cod_confirm",
            )
        )
        assert decision == "confirm"
        assert affected is None

    def test_ambiguous_pending_orders_do_not_guess(self):
        a = SimpleNamespace(
            id=1,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={},
        )
        b = SimpleNamespace(
            id=2,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={},
        )
        db = self._pending_query([a, b])
        decision, affected = _run(
            handle_cod_reply(
                db,
                tenant_id=9,
                customer_phone="+966500111222",
                text="نعم",
            )
        )
        assert decision == "confirm"
        assert affected is None

    def test_foreign_order_id_is_not_trusted(self):
        order = SimpleNamespace(
            id=77,
            tenant_id=9,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={},
        )
        db = self._pending_query([order])

        class _Query:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return [order]

            def first(self):
                return None

        db.query = lambda *_a, **_k: _Query()
        decision, affected = _run(
            handle_cod_reply(
                db,
                tenant_id=9,
                customer_phone="+966500111222",
                text="تأكيد الطلب ✅",
                button_payload="nahla_cod_confirm:999",
            )
        )
        assert decision == "confirm"
        assert affected is None
        assert order.status == "pending_confirmation"

    def test_duplicate_tap_does_not_push_again(self):
        order = SimpleNamespace(
            id=77,
            tenant_id=9,
            status="under_review",
            customer_info={"phone": "+966500111222"},
            extra_metadata={"cod_confirmed_at": "2026-09-06T08:00:00Z"},
        )
        db = self._pending_query([])
        push = AsyncMock(return_value="again")
        with patch("services.cod_confirmation._push_cod_to_store", push):
            decision, affected = _run(
                handle_cod_reply(
                    db,
                    tenant_id=9,
                    customer_phone="+966500111222",
                    text="تأكيد الطلب ✅",
                    button_payload="nahla_cod_confirm:77",
                )
            )
        assert affected is None
        push.assert_not_awaited()

    def test_webhook_intercepts_button_ids_before_brain(self):
        src = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
            encoding="utf-8"
        )
        interactive = src.index("normalized_type == \"interactive\"")
        brain_generic = src.index("button_reply (generic)")
        assert src.find("button_payload=btn_id", interactive, brain_generic) > 0
        button_rescue = src.index("msg_type == \"button\"")
        merchant_rescue = src.index("_handle_merchant_message", button_rescue)
        assert src.find("button_payload=_btn_payload", button_rescue, merchant_rescue) > 0


class TestMigration0104:
    def test_parent_and_unique_expression(self):
        path = (
            DATABASE_DIR
            / "migrations"
            / "versions"
            / "0104_active_lifecycle_template_null_step.py"
        )
        src = path.read_text(encoding="utf-8")
        assert 'revision = "0104"' in src
        assert 'down_revision = "0103"' in src
        assert "uq_active_lifecycle_template_null_step" in src
        upgrade_src = src.split("def upgrade", 1)[1]
        assert "previous_status" not in upgrade_src
        assert "step_number IS NULL" in src
        assert "No rows were deleted" in src


class TestZeroAi:
    def test_remediation_modules_do_not_call_model(self):
        forbidden = ("MerchantBrain", "openai", "anthropic", "generate_cart_recovery_text")
        for rel in (
            "core/commerce_lifecycle/dispatch.py",
            "services/cod_confirmation.py",
            "core/commerce_lifecycle/canary_guard.py",
        ):
            src = (BACKEND_DIR / rel).read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in src, f"{rel} contains {token}"
        model = MagicMock()
        model.assert_not_called()
        tree = ast.parse(
            (BACKEND_DIR / "core" / "commerce_lifecycle" / "dispatch.py").read_text(
                encoding="utf-8"
            )
        )
        assert tree is not None
