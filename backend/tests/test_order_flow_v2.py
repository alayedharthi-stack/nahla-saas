"""OrderFlowV2 — deterministic checkout owner tests."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: E402
    resolve_active_order_quantity_reply,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.order_flow_v2.flags import (  # noqa: E402
    is_order_flow_v2_enabled,
    should_skip_legacy_order_flow_reply,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.replies import build_next_field_reply  # noqa: E402
from modules.ai.order_flow_v2.shipping import evaluate_v2_shipping_readiness  # noqa: E402
from modules.ai.order_flow_v2.triggers import (  # noqa: E402
    is_catalog_order_inbound,
    is_inquiry_message,
    should_not_start_checkout,
)


@pytest.fixture(autouse=True)
def _reset_v2_flags(monkeypatch):
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("LEGACY_ORDER_FLOW_DISABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")


class TestFlags:
    def test_defaults_off(self, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
        monkeypatch.setattr("core.config.LEGACY_ORDER_FLOW_DISABLED", False, raising=False)
        assert is_order_flow_v2_enabled() is False
        assert should_skip_legacy_order_flow_reply() is False

    def test_v2_enabled_skips_legacy(self, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        assert should_skip_legacy_order_flow_reply() is True


class TestTriggers:
    def test_price_inquiry_not_checkout(self):
        assert is_inquiry_message("كم سعر العكبر؟")
        assert should_not_start_checkout("كم سعر العكبر؟")

    def test_availability_inquiry_not_checkout(self):
        assert is_inquiry_message("متوفر 40 جرام؟")
        assert should_not_start_checkout("متوفر 40 جرام؟")

    def test_types_inquiry_not_checkout(self):
        assert is_inquiry_message("وش الأنواع؟")

    def test_catalog_sent_not_order(self):
        assert not is_catalog_order_inbound({"native_catalog_sent": True})
        assert should_not_start_checkout("مرحبا", {"catalog_sent": True})

    def test_catalog_order_submitted(self):
        meta = {
            "source_type": "catalog_order",
            "product_items": [{"product_retailer_id": "p1", "quantity": 1}],
        }
        assert is_catalog_order_inbound(meta)


class TestLegacyIsolation:
    def test_active_order_quantity_disabled_when_v2(self, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.LEGACY_ORDER_FLOW_DISABLED", False, raising=False)
        state = SimpleNamespace(
            cart_items=[{"product_name": "عسل", "quantity": 1}],
            order_prep={"line_items": [{"product_id": "p1", "quantity": 1}]},
        )
        reply = resolve_active_order_quantity_reply("نص كيلo", state=state, active_commerce=True)
        assert reply is None

    def test_commerce_guard_skips_checkout_fallback_when_v2(self, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.LEGACY_ORDER_FLOW_DISABLED", False, raising=False)
        state = SimpleNamespace(
            cart_items=[{"product_name": "عسل", "quantity": 1}],
            order_prep={
                "line_items": [{"product_id": "p1", "quantity": 1}],
                "missing_fields": ["city"],
            },
        )
        fallback, kind = select_arabic_commerce_fallback(
            inbound_text="مكة",
            state=state,
        )
        assert kind != "checkout_slot_prompt"
        assert kind != "active_order_quantity"


class TestOrderFlowV2Owner:
    def _db_with_prep(self, prep: dict):
        db = MagicMock()
        conv = SimpleNamespace(
            id=1,
            extra_metadata={"brain_state": {"order_prep": prep, "cart_items": prep.get("line_items", [])}},
        )
        db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = conv
        return db

    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    @patch("modules.ai.order_flow_v2.owner.apply_state_patch")
    def test_catalog_order_starts_v2(self, _patch, _items, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        _items.return_value = SimpleNamespace(
            line_items=[{
                "product_id": "11",
                "product_name": "عسل",
                "quantity": 1,
                "catalog_price": 265.0,
                "price_source": "whatsapp_catalog",
            }],
        )
        db = self._db_with_prep({})
        result = try_handle_order_flow_v2(
            db,
            tenant_id=1,
            customer_phone="966501234567",
            message="",
            inbound_metadata={
                "source_type": "catalog_order",
                "product_items": [{
                    "product_retailer_id": "p1",
                    "quantity": 1,
                    "item_price": 265,
                    "currency": "SAR",
                }],
            },
        )
        assert result.handled
        assert "اسمك الكامل" in result.reply
        assert "الحجم" not in result.reply
        assert result.state_patch.get("order_flow_v2_active") is True

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_greeting_with_pending_no_slot_question(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "line_items": [{"product_id": "p1", "quantity": 1}],
            "order_flow_v2_pending": True,
        }
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="السلام عليكم",
        )
        assert result.handled
        assert "وعليكم السلام" in result.reply
        assert "كمل الطلب" in result.reply
        assert "المدينة" not in result.reply

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_inquiry_not_handled(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        _load.return_value = (None, {})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="كم سعر العكبر؟",
        )
        assert not result.handled

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_resume_pending_checkout(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "line_items": [{"product_id": "p1", "product_name": "عسل", "quantity": 1, "catalog_price": 265}],
            "order_flow_v2_pending": True,
            "customer_first_name": "محمد",
            "customer_last_name": "الكناني",
        }
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="كمل الطلب",
        )
        assert result.handled
        assert "نكمل" in result.reply or "المدينة" in result.reply


class TestDeterministicReplies:
    def test_no_generic_templates(self):
        prep = {
            "line_items": [{"product_name": "عسل", "quantity": 1, "catalog_price": 265}],
            "order_total": 265,
        }
        reply = build_next_field_reply(
            order_prep=prep,
            brain_state={},
            missing_fields=["customer_name"],
        )
        assert "أرسل أي رسالة" not in reply
        assert "ما ظهر عندي سعر" not in reply
        assert "اسمك الكامل" in reply

    def test_trusted_catalog_price_in_summary(self):
        prep = {
            "line_items": [{"product_name": "عسل", "quantity": 1, "catalog_price": 265}],
            "order_flow_v2_catalog_total": 265,
        }
        reply = build_next_field_reply(
            order_prep=prep,
            brain_state={},
            missing_fields=["city"],
        )
        assert "265" in reply
        assert "ما ظهر عندي سعر" not in reply


class TestShippingReadiness:
    def test_not_ready_without_address(self):
        prep = {
            "customer_first_name": "محمد",
            "customer_last_name": "الكناني",
            "city": "مكة",
            "line_items": [{"product_id": "p1", "quantity": 1}],
        }
        verdict = evaluate_v2_shipping_readiness(order_prep=prep, brain_state={})
        assert not verdict["allowed"]

    def test_ready_with_short_code(self):
        prep = {
            "customer_first_name": "محمد",
            "customer_last_name": "الكناني",
            "city": "مكة",
            "short_address_code": "MDQA5061",
            "payment_method": "bank_transfer",
            "line_items": [{"product_id": "p1", "quantity": 1}],
        }
        verdict = evaluate_v2_shipping_readiness(
            order_prep=prep,
            brain_state={"cart_items": prep["line_items"]},
            customer_phone="966501234567",
        )
        assert verdict["allowed"]

    def test_bank_transfer_not_shipped_without_verification(self):
        from modules.ai.order_flow_v2.shipping import can_claim_shipping_started  # noqa: E402

        prep = {
            "payment_method": "bank_transfer",
            "payment_receipt_received": True,
        }
        assert not can_claim_shipping_started(prep)
