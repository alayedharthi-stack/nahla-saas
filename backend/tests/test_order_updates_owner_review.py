"""Owner-review blockers: COD single owner, settings fail-closed, master vs flags, no payment_reminder."""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.dispatch import (  # noqa: E402
    dispatch_external_lifecycle_notification,
)
from core.commerce_lifecycle.order_updates import (  # noqa: E402
    LEGACY_DEFAULT_ON_KEYS,
    REASON_ORDER_UPDATE_DISABLED,
    REASON_SETTINGS_UNAVAILABLE,
    evaluate_order_update_delivery,
    get_order_update_flags,
    get_order_updates_master_enabled,
    is_order_update_enabled,
    load_order_update_settings_truth,
    resolve_lifecycle_template_for_send,
    set_order_update_flags,
)
from models import (  # noqa: E402
    CommerceLifecycleNotificationLedger,
    TenantSettings,
    WaConversationWindow,
    WhatsAppTemplate,
)
from routers.order_updates import (  # noqa: E402
    OrderUpdateFlagsPayload,
    get_settings,
    patch_settings,
)
from services.cod_confirmation import (  # noqa: E402
    CANONICAL_SERVICE_KEY,
    handle_cod_reply,
    send_cod_confirmation_template,
)
from store_adapters.salla_lifecycle import (  # noqa: E402
    normalize_salla_lifecycle_business_intent,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in models:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _cod_template(**kwargs):
    defaults = dict(
        id=44,
        name="nahla_cod_confirmation_live",
        language="ar",
        status="APPROVED",
        is_active=True,
        is_hidden=False,
        revision=3,
        service_key="cod_confirmation",
        category="UTILITY",
        components=[
            {"type": "BODY", "text": "مرحبا {{1}} طلب #{{2}}"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
                    {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
                ],
            },
        ],
    )
    defaults.update(kwargs)
    return WhatsAppTemplate(**defaults)


def _user(tenant_id: int = 9):
    return SimpleNamespace(tenant_id=tenant_id)


class TestSettingsFailClosed:
    def test_missing_row_uses_compatibility_defaults(self):
        db, _ = _make_db(TenantSettings)
        truth = load_order_update_settings_truth(db, 9)
        assert truth.available is True
        assert truth.reason is None
        assert truth.master_enabled is True
        for key in ("order_confirmation", "shipping_tracking", "cod_confirmation"):
            assert truth.flags[key] is True, key
        assert truth.flags["payment_pending"] is False
        assert is_order_update_enabled(db, 9, "order_confirmation") is True

    def test_query_exception_is_not_merchant_consent(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        allowed, reason = evaluate_order_update_delivery(db, 9, "order_confirmation")
        assert allowed is False
        assert reason == REASON_SETTINGS_UNAVAILABLE
        assert get_order_updates_master_enabled(db, 9) is False

    def test_merchant_disabled_blocks_send(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {"cod_confirmation": False}, commit=True)
        allowed, reason = evaluate_order_update_delivery(db, 9, "cod_confirmation")
        assert allowed is False
        assert reason == REASON_ORDER_UPDATE_DISABLED

    def test_master_off_preserves_individual_flags(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(
            db,
            9,
            {"order_confirmation": True, "shipping_tracking": True},
            master_enabled=True,
            commit=True,
        )
        set_order_update_flags(db, 9, {}, master_enabled=False, commit=True)
        flags = get_order_update_flags(db, 9)
        assert flags["order_confirmation"] is True
        assert flags["shipping_tracking"] is True
        assert is_order_update_enabled(db, 9, "order_confirmation") is False
        assert is_order_update_enabled(db, 9, "shipping_tracking") is False
        set_order_update_flags(db, 9, {}, master_enabled=True, commit=True)
        assert is_order_update_enabled(db, 9, "order_confirmation") is True
        assert is_order_update_enabled(db, 9, "shipping_tracking") is True
        assert get_order_update_flags(db, 9)["order_confirmation"] is True


class TestSettingsApiSnapshot:
    def test_patch_master_does_not_write_effective_false_into_flags(self):
        db, _ = _make_db(TenantSettings, WhatsAppTemplate)
        set_order_update_flags(
            db,
            9,
            {"order_confirmation": True, "shipping_tracking": True},
            master_enabled=True,
            commit=True,
        )
        payload = patch_settings(
            OrderUpdateFlagsPayload(enabled=False),
            db=db,
            user=_user(),
        )
        assert payload["enabled"] is False
        assert payload["flags"]["order_confirmation"] is True
        assert payload["flags"]["shipping_tracking"] is True
        assert payload["effective"]["order_confirmation"] is False
        assert payload["effective"]["shipping_tracking"] is False
        assert payload["services"]["order_confirmation"]["enabled"] is True
        assert payload["services"]["order_confirmation"]["effective_enabled"] is False

        restored = patch_settings(
            OrderUpdateFlagsPayload(enabled=True),
            db=db,
            user=_user(),
        )
        assert restored["enabled"] is True
        assert restored["flags"]["order_confirmation"] is True
        assert restored["effective"]["order_confirmation"] is True

    def test_patch_only_changed_key(self):
        db, _ = _make_db(TenantSettings, WhatsAppTemplate)
        set_order_update_flags(
            db, 9, {"order_confirmation": True, "shipping_tracking": True}, commit=True
        )
        payload = patch_settings(
            OrderUpdateFlagsPayload(services={"shipping_tracking": {"enabled": False}}),
            db=db,
            user=_user(),
        )
        assert payload["flags"]["order_confirmation"] is True
        assert payload["flags"]["shipping_tracking"] is False

    def test_get_settings_503_when_truth_unavailable(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        try:
            get_settings(db=db, user=_user())
        except HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail == REASON_SETTINGS_UNAVAILABLE
        else:
            raise AssertionError("expected 503")


class TestPaymentPendingNoFallback:
    def test_payment_reminder_row_is_not_selected(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        db.add(
            WhatsAppTemplate(
                tenant_id=4,
                name="nahla_payment_reminder_hist",
                language="ar",
                category="UTILITY",
                status="APPROVED",
                components=[{"type": "BODY", "text": "ادفع {{1}}"}],
                service_key="payment_reminder",
                is_active=True,
                is_hidden=False,
                revision=1,
            )
        )
        db.commit()
        assert resolve_lifecycle_template_for_send(db, 4, "payment_pending") is None

    def test_resolver_source_has_no_payment_reminder_alias(self):
        from core.commerce_lifecycle import order_updates as mod  # noqa: PLC0415

        src = inspect.getsource(mod.resolve_lifecycle_template_for_send)
        assert "payment_reminder" not in src
        assert "_SERVICE_KEY_ALIASES" not in Path(mod.__file__).read_text(encoding="utf-8")


class TestCodSingleOwner:
    def test_salla_first_seen_cod_is_not_customer_confirm_prompt(self):
        assert normalize_salla_lifecycle_business_intent(
            None, "under_review", {"payment_method": "cod"}
        ) == BusinessIntent.ORDER_CONFIRMED
        assert normalize_salla_lifecycle_business_intent(
            "payment_pending", "under_review", {"payment_method": "cod"}
        ) is None

    def test_legacy_hard_named_template_is_not_send_owner(self):
        src = inspect.getsource(send_cod_confirmation_template)
        assert "cod_order_confirmation_ar" not in src
        assert "resolve_lifecycle_template_for_send" in src

    def test_one_checkout_one_confirmation_then_salla_push_does_not_resend(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", "9")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", "+966500111222"
        )
        db, _ = _make_db(
            TenantSettings,
            WhatsAppTemplate,
            CommerceLifecycleNotificationLedger,
            WaConversationWindow,
        )
        db.add(_cod_template(tenant_id=9))
        db.commit()
        order = SimpleNamespace(
            id=8801,
            external_id=None,
            extra_metadata={"payment_method": "cod"},
        )
        session_send = AsyncMock(return_value=("sent", {"wa_message_id": "wamid.cod.1"}))
        template_send = AsyncMock(return_value=("sent", {"wa_message_id": "wamid.cod.1"}))
        with patch(
            "core.automation_engine.send_lifecycle_whatsapp_session_body",
            session_send,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            template_send,
        ), patch(
            "core.commerce_lifecycle.canary_guard.evaluate_and_audit",
            return_value=SimpleNamespace(allowed=True, reason="permitted"),
        ):
            first = asyncio.run(
                send_cod_confirmation_template(
                    db,
                    tenant_id=9,
                    order=order,
                    customer_phone="+966500111222",
                    customer_name="أحمد سالم",
                    product_name="قميص قطني أزرق",
                    total_amount="120",
                )
            )
        assert first["sent"] is True
        assert first["service_key"] == CANONICAL_SERVICE_KEY
        assert first["buttons"] == ["تأكيد الطلب ✅", "إلغاء الطلب ❌"]
        assert session_send.await_count + template_send.await_count == 1
        assert order.extra_metadata["nahla_cod_confirmation_sent"] is True

        order.extra_metadata["cod_confirmed_at"] = "2026-09-06T05:00:00+00:00"
        order.extra_metadata["cod_pushed_external_id"] = "salla-ord-8801"
        order.external_id = "salla-ord-8801"
        dispatch_send = AsyncMock(return_value=("sent", {"wa_message_id": "wamid.dup"}))
        with patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            dispatch_send,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_session_body",
            dispatch_send,
        ), patch(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            return_value=SimpleNamespace(to_dict=lambda: {}),
        ):
            result = asyncio.run(
                dispatch_external_lifecycle_notification(
                    db,
                    tenant_id=9,
                    order=order,
                    provider="salla",
                    raw_previous_status=None,
                    raw_current_status="under_review",
                    normalized_order={
                        "external_id": "salla-ord-8801",
                        "status": "under_review",
                        "external_order_number": "8801",
                        "payment_method": "cod",
                    },
                    raw_payload={"event_id": "evt-salla-cod", "updated_at": "2026-09-06T05:01:00Z"},
                )
            )
        assert result.dispatched is False
        assert result.reason_code == "nahla_cod_already_confirmed"
        dispatch_send.assert_not_awaited()
        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

    def test_cancel_does_not_push_and_does_not_duplicate(self):
        db, _ = _make_db(TenantSettings)
        order = SimpleNamespace(
            id=77,
            tenant_id=9,
            status="pending_confirmation",
            customer_info={"phone": "+966500111222"},
            extra_metadata={"payment_method": "cod", "nahla_cod_confirmation_sent": True},
            line_items=[{"product_id": "1", "quantity": 1}],
        )

        class _Query:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return [order]

        db.query = lambda *_a, **_k: _Query()
        db.commit = lambda: None
        push = AsyncMock(return_value="should-not-push")
        with patch("services.cod_confirmation._push_cod_to_store", push), patch(
            "observability.event_logger.log_event", lambda *a, **k: None
        ), patch(
            "services.cod_confirmation.flag_modified", lambda *a, **k: None
        ):
            decision, affected = asyncio.run(
                handle_cod_reply(
                    db,
                    tenant_id=9,
                    customer_phone="+966500111222",
                    text="إلغاء الطلب ❌",
                )
            )
        assert decision == "cancel"
        assert affected is order
        assert order.status == "cancelled"
        assert order.extra_metadata.get("cod_cancelled_at")
        push.assert_not_awaited()

    def test_open_and_closed_use_same_active_revision_and_buttons(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings, WaConversationWindow)
        tpl = _cod_template(tenant_id=4)
        db.add(tpl)
        db.commit()
        resolved = resolve_lifecycle_template_for_send(db, 4, "cod_confirmation")
        assert resolved is not None
        assert int(resolved.id) == int(tpl.id)
        assert int(resolved.revision) == 3
        buttons = [
            b.get("text")
            for c in (resolved.components or [])
            if str(c.get("type", "")).upper() == "BUTTONS"
            for b in (c.get("buttons") or [])
        ]
        assert buttons == ["تأكيد الطلب ✅", "إلغاء الطلب ❌"]

        captured = []

        async def _session(*args, **kwargs):
            captured.append(("session", kwargs.get("service_key"), args[3]))
            return ("sent", {"wa_message_id": "wamid.s"})

        async def _template(*args, **kwargs):
            captured.append(("template", kwargs.get("service_key"), args[3]))
            return ("sent", {"wa_message_id": "wamid.t"})

        order = SimpleNamespace(id=12, extra_metadata={})
        with patch(
            "core.automation_engine.send_lifecycle_whatsapp_session_body",
            _session,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            _template,
        ), patch(
            "core.commerce_lifecycle.canary_guard.evaluate_and_audit",
            return_value=SimpleNamespace(allowed=True, reason="permitted"),
        ), patch(
            "core.commerce_lifecycle.window.lifecycle_service_window_is_open",
            return_value=(True, "service_window"),
        ):
            open_result = asyncio.run(
                send_cod_confirmation_template(
                    db,
                    tenant_id=4,
                    order=order,
                    customer_phone="+966500111222",
                    customer_name="نورة عبدالله",
                    product_name="عطر ورد 100ml",
                    total_amount="90",
                )
            )
        with patch(
            "core.automation_engine.send_lifecycle_whatsapp_session_body",
            _session,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            _template,
        ), patch(
            "core.commerce_lifecycle.canary_guard.evaluate_and_audit",
            return_value=SimpleNamespace(allowed=True, reason="permitted"),
        ), patch(
            "core.commerce_lifecycle.window.lifecycle_service_window_is_open",
            return_value=(False, "closed"),
        ):
            closed_result = asyncio.run(
                send_cod_confirmation_template(
                    db,
                    tenant_id=4,
                    order=SimpleNamespace(id=13, extra_metadata={}),
                    customer_phone="+966500111222",
                    customer_name="نورة عبدالله",
                    product_name="عطر ورد 100ml",
                    total_amount="90",
                )
            )
        assert open_result["send_method"] == "session_message"
        assert closed_result["send_method"] == "approved_template"
        assert open_result["template_id"] == closed_result["template_id"] == tpl.id
        assert captured[0][1] == captured[1][1] == "cod_confirmation"
        assert captured[0][2].id == captured[1][2].id == tpl.id


class TestDispatchSettingsUnavailable:
    def test_settings_exception_blocks_lifecycle_send(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", "20")
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", "+966500111222"
        )
        db, _ = _make_db(CommerceLifecycleNotificationLedger, WaConversationWindow)
        order = SimpleNamespace(
            id=501,
            external_id="salla-ord-8801",
            extra_metadata={},
            customer_info={"phone": "+966500111222"},
            customer_name="أحمد سالم",
            checkout_url="https://shop.generic.example/checkout/8801",
            status="under_review",
            external_order_number="ORD-8801",
        )
        send = AsyncMock()
        caps = SimpleNamespace(
            to_dict=lambda: {
                "has_external_store": True,
                "supports_external_checkout": True,
                "supports_whatsapp_orders": True,
                "supports_nahla_orders": False,
                "supports_cod": True,
                "has_external_tracking": True,
                "has_payment_link": True,
            }
        )
        with patch(
            "core.commerce_lifecycle.order_updates.evaluate_order_update_delivery",
            return_value=(False, REASON_SETTINGS_UNAVAILABLE),
        ), patch(
            "core.commerce_lifecycle.order_updates.resolve_lifecycle_template_for_send",
            return_value=SimpleNamespace(name="order_confirmed_generic_ar", id=1),
        ), patch(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            return_value=caps,
        ), patch(
            "core.automation_engine.send_lifecycle_whatsapp_template", send
        ):
            result = asyncio.run(
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
                    raw_payload={"event_id": "evt-settings", "updated_at": "2026-09-06T05:00:00Z"},
                )
            )
        assert result.dispatched is False
        assert result.reason_code == REASON_SETTINGS_UNAVAILABLE
        send.assert_not_awaited()
