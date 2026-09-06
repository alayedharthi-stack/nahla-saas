"""Order lifecycle notifications — flags, mapping, same-template, idempotency, zero AI."""
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
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.automation_engine import (  # noqa: E402
    _lifecycle_quick_reply_id,
    _lifecycle_session_quick_replies,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.order_updates import (  # noqa: E402
    LEGACY_DEFAULT_ON_KEYS,
    ORDER_UPDATE_SERVICE_KEYS,
    get_order_update_flags,
    get_order_updates_master_enabled,
    is_order_update_enabled,
    resolve_lifecycle_template_for_send,
    set_order_update_flags,
)
from core.commerce_lifecycle.window import (  # noqa: E402
    WINDOW_SOURCE_ERROR_FAIL_CLOSED,
    lifecycle_service_window_is_open,
)
from models import TenantSettings, WhatsAppTemplate  # noqa: E402
from store_adapters.salla_lifecycle import (  # noqa: E402
    normalize_salla_lifecycle_business_intent,
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


def _seed_approved(db, *, tenant_id: int, service_key: str, body: str = "مرحبا {{1}} #{{2}}") -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=f"nahla_{service_key}_live",
        language="ar",
        category="UTILITY",
        status="APPROVED",
        components=[{"type": "BODY", "text": body}],
        service_key=service_key,
        is_active=True,
        is_hidden=False,
        step_number=None,
        revision=3,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


class TestMerchantFlags:
    def test_legacy_keys_default_on_new_keys_default_off(self):
        db, _ = _make_db(TenantSettings)
        flags = get_order_update_flags(db, 9)
        for key in ORDER_UPDATE_SERVICE_KEYS:
            if key in LEGACY_DEFAULT_ON_KEYS:
                assert flags[key] is True, key
            else:
                assert flags[key] is False, key

    def test_master_off_blocks_all_including_legacy(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {}, master_enabled=False, commit=True)
        assert get_order_updates_master_enabled(db, 9) is False
        assert is_order_update_enabled(db, 9, "order_confirmation") is False
        assert is_order_update_enabled(db, 9, "shipping_tracking") is False

    def test_individual_off_blocks_only_that_key(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 9, {"order_delivered": True, "order_cancelled": False}, commit=True)
        assert is_order_update_enabled(db, 9, "order_delivered") is True
        assert is_order_update_enabled(db, 9, "order_cancelled") is False
        assert is_order_update_enabled(db, 9, "order_confirmation") is True

    def test_disable_does_not_delete_templates(self):
        db, _ = _make_db(TenantSettings, WhatsAppTemplate)
        tpl = _seed_approved(db, tenant_id=9, service_key="order_confirmation")
        set_order_update_flags(db, 9, {"order_confirmation": False}, commit=True)
        db.refresh(tpl)
        assert tpl.is_active is True
        assert tpl.status == "APPROVED"
        assert db.query(WhatsAppTemplate).count() == 1

    def test_tenant_isolation(self):
        db, _ = _make_db(TenantSettings)
        set_order_update_flags(db, 1, {"order_delivered": True}, commit=True)
        assert is_order_update_enabled(db, 1, "order_delivered") is True
        assert is_order_update_enabled(db, 2, "order_delivered") is False


class TestSameTemplateContract:
    def test_open_and_closed_resolve_same_active_revision(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        active = _seed_approved(db, tenant_id=4, service_key="order_confirmation")
        draft = WhatsAppTemplate(
            tenant_id=4,
            name="nahla_order_confirmation_r4",
            language="ar",
            category="UTILITY",
            status="PENDING",
            components=[{"type": "BODY", "text": "مسودة {{1}}"}],
            service_key="order_confirmation",
            is_active=False,
            is_hidden=False,
            revision=4,
            supersedes_template_id=active.id,
        )
        db.add(draft)
        db.commit()
        resolved = resolve_lifecycle_template_for_send(db, 4, "order_confirmation")
        assert resolved is not None
        assert int(resolved.id) == int(active.id)
        assert int(resolved.revision) == 3

    def test_rejected_revision_not_used(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        active = _seed_approved(db, tenant_id=4, service_key="order_ready")
        rejected = WhatsAppTemplate(
            tenant_id=4,
            name="nahla_order_ready_rej",
            language="ar",
            category="UTILITY",
            status="REJECTED",
            components=[{"type": "BODY", "text": "مرفوض"}],
            service_key="order_ready",
            is_active=False,
            is_hidden=False,
            revision=9,
        )
        db.add(rejected)
        db.commit()
        resolved = resolve_lifecycle_template_for_send(db, 4, "order_ready")
        assert int(resolved.id) == int(active.id)

    def test_never_substitutes_another_event_template(self):
        db, _ = _make_db(WhatsAppTemplate, TenantSettings)
        _seed_approved(db, tenant_id=4, service_key="shipping_tracking")
        assert resolve_lifecycle_template_for_send(db, 4, "out_for_delivery") is None
        assert resolve_lifecycle_template_for_send(db, 4, "order_delivered") is None


class TestSallaMapping:
    def test_preparing_and_ready_are_distinct(self):
        assert normalize_salla_lifecycle_business_intent(
            "under_review", "in_progress", {}
        ) == BusinessIntent.ORDER_PREPARING
        assert normalize_salla_lifecycle_business_intent(
            "in_progress", "in_progress", {}
        ) is None
        assert normalize_salla_lifecycle_business_intent(
            "in_progress", "ready", {}
        ) == BusinessIntent.ORDER_PACKED

    def test_out_for_delivery_and_delivered_and_cancelled_refunded(self):
        assert normalize_salla_lifecycle_business_intent(
            "shipped", "out_for_delivery", {}
        ) == BusinessIntent.OUT_FOR_DELIVERY
        assert normalize_salla_lifecycle_business_intent(
            "out_for_delivery", "delivered", {}
        ) == BusinessIntent.ORDER_DELIVERED
        assert normalize_salla_lifecycle_business_intent(
            "in_progress", "cancelled", {}
        ) == BusinessIntent.ORDER_CANCELLED
        assert normalize_salla_lifecycle_business_intent(
            "delivered", "refunded", {}
        ) == BusinessIntent.ORDER_REFUNDED

    def test_payment_confirmed_only_from_pending_non_cod(self):
        assert normalize_salla_lifecycle_business_intent(
            "payment_pending", "paid", {"payment_method": "bank"}
        ) == BusinessIntent.PAYMENT_CONFIRMED
        assert normalize_salla_lifecycle_business_intent(
            "payment_pending", "paid", {"payment_method": "cod"}
        ) is None


class TestIdempotency:
    def test_duplicate_webhook_payload_same_key(self):
        payload = {"event_id": "evt-a", "updated_at": "2026-07-13T10:00:00Z"}
        a = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="under_review",
            raw_current_status="in_progress",
            raw_payload=payload,
        )
        b = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="under_review",
            raw_current_status="in_progress",
            raw_payload=payload,
        )
        assert a == b

    def test_new_transition_different_key(self):
        a = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="under_review",
            raw_current_status="in_progress",
        )
        b = build_transition_identity(
            provider="salla",
            external_order_id="ext-1",
            raw_previous_status="in_progress",
            raw_current_status="ready",
        )
        assert a != b


class TestWindowFailClosed:
    def test_error_fails_closed_to_closed_window(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        opened, source = lifecycle_service_window_is_open(db, 1, "+966500111222")
        assert opened is False
        assert source == WINDOW_SOURCE_ERROR_FAIL_CLOSED


class TestCodButtons:
    def test_canonical_cod_buttons_bind_confirm_cancel(self):
        template = SimpleNamespace(
            components=[
                {
                    "type": "BUTTONS",
                    "buttons": [
                        {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
                        {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
                    ],
                }
            ]
        )
        titles = _lifecycle_session_quick_replies(template)
        assert titles == ["تأكيد الطلب ✅", "إلغاء الطلب ❌"]
        assert _lifecycle_quick_reply_id(titles[0], 0) == "nahla_cod_confirm"
        assert _lifecycle_quick_reply_id(titles[1], 1) == "nahla_cod_cancel"


class TestZeroAi:
    def test_lifecycle_package_has_no_model_imports(self):
        package_root = Path(__file__).resolve().parents[1] / "core" / "commerce_lifecycle"
        forbidden = ("modules.ai", "openai", "anthropic", "MerchantBrain")
        for path in package_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for bad in forbidden:
                            assert bad not in alias.name
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for bad in forbidden:
                        assert bad not in node.module

    def test_dispatch_does_not_call_model_gateway(self, monkeypatch):
        model = MagicMock()
        fake = SimpleNamespace(generate_cart_recovery_text=model)
        monkeypatch.setitem(sys.modules, "services.ai_client", fake)
        from core.commerce_lifecycle import dispatch as dispatch_mod  # noqa: PLC0415

        src = inspect.getsource(dispatch_mod)
        assert "generate_cart_recovery_text" not in src
        assert "MerchantBrain" not in src
        model.assert_not_called()


def test_session_interactive_payload_uses_canonical_buttons():
    from core.automation_engine import send_lifecycle_whatsapp_session_body  # noqa: PLC0415

    template = SimpleNamespace(
        name="nahla_cod_confirmation_live",
        language="ar",
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
    captured: dict[str, Any] = {}

    async def _send(_db, _conn, **kwargs):
        captured["payload"] = kwargs["payload"]
        return {"messages": [{"id": "wamid.cod.1"}]}, {}

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        phone_number_id="123",
        status="connected",
        tenant_id=1,
    )
    with patch("core.billing.has_billing_access", return_value=True), patch(
        "core.commerce_lifecycle.canary_guard.evaluate_and_audit",
        return_value=SimpleNamespace(allowed=True, reason="permitted"),
    ), patch(
        "services.whatsapp_platform.service.provider_send_message",
        new=AsyncMock(side_effect=_send),
    ), patch(
        "core.automation_engine._resolve_store_name",
        return_value="متجر تجريبي عام",
    ), patch(
        "core.automation_engine._build_template_vars",
        return_value={},
    ), patch(
        "core.acceptance_execution_context.deny_external_egress",
        return_value=None,
    ):
        outcome, info = asyncio.run(
            send_lifecycle_whatsapp_session_body(
                db,
                1,
                "+966500111222",
                template,
                {"order_number": "8801"},
                customer_name="أحمد سالم",
                service_key="cod_confirmation",
            )
        )
    assert outcome == "sent"
    assert captured["payload"]["type"] == "interactive"
    buttons = captured["payload"]["interactive"]["action"]["buttons"]
    assert buttons[0]["reply"]["id"] == "nahla_cod_confirm"
    assert buttons[1]["reply"]["id"] == "nahla_cod_cancel"
    assert info.get("wa_message_id") == "wamid.cod.1"
