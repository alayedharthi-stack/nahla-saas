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

from core.merchant_payment_methods import MerchantPaymentMethods  # noqa: E402
from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: E402
    resolve_active_order_quantity_reply,
)
from modules.ai.brain.commerce.conversation_state_isolation import (  # noqa: E402
    inbound_breaks_fulfillment_ownership,
    should_replay_pending_question,
)
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.order_flow_v2.flags import (  # noqa: E402
    is_order_flow_v2_enabled,
    should_skip_legacy_order_flow_reply,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.payment_evidence import (  # noqa: E402
    RECEIPT_RECEIVED_NEEDS_REVIEW,
    RECEIPT_REJECTED_MISMATCH,
    RECEIPT_VERIFIED_BY_MERCHANT,
    evaluate_receipt_status,
    payment_confirmation_allowed,
)
from modules.ai.order_flow_v2.replies import build_next_field_reply  # noqa: E402
from modules.ai.order_flow_v2.shipping import (  # noqa: E402
    can_claim_shipping_started,
    evaluate_v2_shipping_readiness,
)
from modules.ai.order_flow_v2.triggers import (  # noqa: E402
    is_catalog_order_inbound,
    is_checkout_escape_inquiry,
    is_inquiry_message,
    should_not_start_checkout,
)

_STALE_CHECKOUT_PROMPT = "ممتاز، ما اسمك الأول لإكمال الطلب؟"
_LEGACY_FORBIDDEN = (
    "أرسل أي رسالة وسأتابع معك الطلب",
    "ما ظهر عندي سعر مؤكد من الكتالوج الآن",
    "تمام، أكمل معك الطلب —",
    "أحس أني كرّرت نفس الإجابة",
)
_PHONE_REQUEST_MARKERS = ("رقم جوالك", "رقم الجوال", "رقم هاتفك", "الجوال للتواصل")


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

    def test_live_transcript_inquiry_is_checkout_escape(self):
        assert is_checkout_escape_inquiry("ابي استفسر عن العسل")
        assert is_checkout_escape_inquiry("وش الأنواع المتوفرة؟")
        assert is_checkout_escape_inquiry("وش الأحجام المتوفرة؟")
        assert should_not_start_checkout("وش الأنواع المتوفرة؟")

    def test_catalog_sent_not_order(self):
        assert not is_catalog_order_inbound({"native_catalog_sent": True})
        assert should_not_start_checkout("مرحبا", {"catalog_sent": True})

    def test_catalog_order_submitted(self):
        meta = {
            "source_type": "catalog_order",
            "product_items": [{"product_retailer_id": "p1", "quantity": 1}],
        }
        assert is_catalog_order_inbound(meta)

    def test_catalog_order_alternate_order_metadata(self):
        meta = {
            "source_type": "order",
            "order": {
                "product_items": [{
                    "product_retailer_id": "86bqzca62a",
                    "quantity": 2,
                    "item_price": 182.75,
                    "currency": "SAR",
                }],
            },
        }
        assert is_catalog_order_inbound(meta)


class TestLiveTranscriptStateIsolation:
    def test_inquiry_does_not_replay_stale_checkout_prompt(self):
        assert inbound_breaks_fulfillment_ownership("ابي استفسر عن العسل")
        assert not should_replay_pending_question(
            inbound_text="ابي استفسر عن العسل",
            last_question=_STALE_CHECKOUT_PROMPT,
        )

        state = SimpleNamespace(
            last_question_asked=_STALE_CHECKOUT_PROMPT,
            last_question_answered=False,
        )
        result = try_guard_recovery_reply(
            inbound_text="ابي استفسر عن العسل",
            state=state,
            history=[],
        )

        assert result.source != "last_question_clarify"
        assert _STALE_CHECKOUT_PROMPT not in result.reply

    def test_browse_does_not_replay_stale_checkout_prompt(self):
        assert inbound_breaks_fulfillment_ownership("وش الأنواع المتوفرة؟")
        assert not should_replay_pending_question(
            inbound_text="وش الأنواع المتوفرة؟",
            last_question=_STALE_CHECKOUT_PROMPT,
        )


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

    def _active_prep(self, **extra):
        prep = {
            "order_flow_v2_active": True,
            "line_items": [{"product_id": "p1", "product_name": "عسل", "quantity": 1, "catalog_price": 285}],
            "order_flow_v2_trusted_price": True,
            "order_flow_v2_catalog_total": 285,
        }
        prep.update(extra)
        return prep

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
        assert "payment_method" not in result.state_patch
        assert not any(marker in result.reply for marker in _PHONE_REQUEST_MARKERS)

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
    def test_live_transcript_inquiry_escapes_even_with_active_checkout(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "order_flow_v2_active": True,
            "line_items": [{"product_id": "p1", "product_name": "عسل", "quantity": 1}],
        }
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="ابي استفسر عن العسل",
        )

        assert not result.handled
        assert result.reason == "inquiry_escape"
        assert _STALE_CHECKOUT_PROMPT not in result.reply

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_product_knowledge_escapes_active_checkout_to_brain(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "order_flow_v2_active": True,
            "line_items": [{"product_id": "p1", "product_name": "عسل", "quantity": 1}],
        }
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="ما هي مميزات عسل السدر القيضي؟",
        )

        assert not result.handled
        assert result.reason == "inquiry_escape"
        assert "تم إنشاء طلبك" not in result.reply

    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    @patch("modules.ai.order_flow_v2.owner.apply_state_patch")
    def test_catalog_order_event_priority_over_browse_shapes(self, _patch, _items, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        _items.return_value = SimpleNamespace(
            line_items=[{
                "product_id": "86",
                "product_name": "86bqzca62a",
                "quantity": 2,
                "catalog_price": 182.75,
                "price_source": "whatsapp_catalog",
            }],
        )
        db = self._db_with_prep({})

        result = try_handle_order_flow_v2(
            db,
            tenant_id=1,
            customer_phone="966501234567",
            message="[طلب كتالوج من العميل]\nعدد المنتجات: 2\nالإجمالي: 365.5 SAR\nرمز المنتج (SKU): 86bqzca62a",
            inbound_metadata={
                "source_type": "order",
                "order": {
                    "product_items": [{
                        "product_retailer_id": "86bqzca62a",
                        "quantity": 2,
                        "item_price": 182.75,
                        "currency": "SAR",
                    }],
                },
            },
        )

        assert result.handled
        assert result.reason == "catalog_order_start"
        assert result.state_patch.get("order_flow_v2_active") is True
        assert not any(phrase in result.reply for phrase in _LEGACY_FORBIDDEN)
        assert "تفضّل، اختر من الكتالوج" not in result.reply

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_confirmation_after_catalog_order_continues_only_with_catalog_evidence(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "order_flow_v2_active": True,
            "order_flow_v2_trusted_price": True,
            "line_items": [{
                "product_id": "86",
                "product_name": "86bqzca62a",
                "quantity": 2,
                "catalog_price": 182.75,
                "price_source": "whatsapp_catalog",
            }],
        }
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="ابغى هذا",
        )

        assert result.handled
        assert result.reason != "inquiry_escape"
        assert not any(phrase in result.reply for phrase in _LEGACY_FORBIDDEN)

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_name_correction_owns_turn_before_city(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = self._active_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثب",
            order_flow_v2_last_field="customer_name",
        )
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="الحارثي",
        )

        assert result.handled
        assert result.state_patch["customer_last_name"] == "الحارثي"
        assert "city" not in result.state_patch
        assert result.state_patch["order_flow_v2_last_field"] == "city"

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_address_refusal_keeps_address_missing_and_blocks_payment(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = self._active_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            city="مكة",
            order_flow_v2_last_field="delivery_address",
        )
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="لا",
        )

        assert result.handled
        assert result.state_patch["order_flow_v2_address_refused"] is True
        assert result.state_patch["order_flow_v2_contract"]["field"] == "delivery_address"
        assert result.state_patch["order_flow_v2_contract"]["reason"] == "address_required_before_payment"
        assert "payment_method" not in result.state_patch
        assert "طريقة الدفع" not in result.reply

    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_payment_before_address_is_blocked(self, _load, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = self._active_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            city="مكة",
        )
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="تحويل",
        )

        assert result.handled
        assert "payment_method" not in result.state_patch
        assert result.state_patch["order_flow_v2_contract"]["reason"] == "payment_blocked_until_address"
        assert "تحويل بنكي" not in result.reply

    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_merchant_payment_bank_mismatch_rejected(self, _load, _methods, _accounts, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = self._active_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            city="مكة",
            short_address_code="MDQA5061",
        )
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})
        _methods.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=True,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=["bank_transfer"],
        )
        _accounts.return_value = SimpleNamespace(bank_brands=("rajhi",))

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="تحويل الاهلي",
        )

        assert result.handled
        assert "payment_method" not in result.state_patch
        assert result.state_patch["order_flow_v2_payment_rejected"] is True
        assert result.state_patch["order_flow_v2_payment_rejection_reason"] == "requested_bank_not_enabled"
        assert result.state_patch["order_flow_v2_available_payment_methods"] == ["bank_transfer"]

    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_single_available_payment_method_can_default_after_address(self, _load, _methods, monkeypatch):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = self._active_prep(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            city="مكة",
            short_address_code="MDQA5061",
        )
        _load.return_value = (None, {"order_prep": prep, "cart_items": prep["line_items"]})
        _methods.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=True,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=["bank_transfer"],
        )

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966501234567",
            message="تمام",
        )

        assert result.handled
        assert result.state_patch["payment_method"] == "bank_transfer"
        assert not any(marker in result.reply for marker in _PHONE_REQUEST_MARKERS)

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
        prep = {
            "payment_method": "bank_transfer",
            "payment_receipt_received": True,
        }
        assert not can_claim_shipping_started(prep)

    def test_bank_transfer_not_shipped_without_merchant_receipt_verification(self):
        prep = {
            "payment_method": "bank_transfer",
            "payment_confirmed": True,
            "payment_receipt_received": True,
        }
        assert not can_claim_shipping_started(prep)

    def test_bank_transfer_shipping_allowed_after_merchant_verification(self):
        prep = {
            "payment_method": "bank_transfer",
            "payment_confirmed": True,
            "receipt_verified_by_merchant": True,
        }
        assert can_claim_shipping_started(prep)


class TestPaymentEvidenceGuard:
    def test_receipt_amount_mismatch_needs_rejection_not_confirmation(self):
        verdict = evaluate_receipt_status(
            order_prep={
                "order_total": 285,
                "payment_receipt_received": True,
            },
            receipt_metadata={
                "amount": 2850,
                "payment_evidence_status": "confirmed",
            },
        )
        assert verdict["receipt_status"] == RECEIPT_REJECTED_MISMATCH
        assert verdict["payment_confirmed_allowed"] is False
        assert payment_confirmation_allowed({
            "order_total": 285,
            "payment_receipt_received": True,
            "payment_receipt_metadata": {"amount": 2850},
        }) is False

    def test_receipt_bank_mismatch_requires_review(self):
        verdict = evaluate_receipt_status(
            order_prep={
                "order_total": 285,
                "requested_bank": "alahli",
                "payment_receipt_received": True,
            },
            receipt_metadata={
                "amount": 285,
                "bank": "Al Rajhi Bank",
                "payment_evidence_status": "confirmed",
            },
        )
        assert verdict["receipt_status"] == RECEIPT_REJECTED_MISMATCH
        assert verdict["reason"] == "bank_mismatch"
        assert verdict["payment_confirmed_allowed"] is False

    def test_confirmed_ocr_still_needs_merchant_review(self):
        verdict = evaluate_receipt_status(
            order_prep={
                "order_total": 285,
                "payment_receipt_received": True,
            },
            receipt_metadata={
                "amount": 285,
                "bank": "Al Rajhi Bank",
                "payment_evidence_status": "confirmed",
            },
        )
        assert verdict["receipt_status"] == RECEIPT_RECEIVED_NEEDS_REVIEW
        assert verdict["payment_confirmed_allowed"] is False

    def test_verified_only_by_merchant_allows_confirmation(self):
        verdict = evaluate_receipt_status(
            order_prep={
                "order_total": 285,
                "payment_receipt_received": True,
                "receipt_verified_by_merchant": True,
            },
            receipt_metadata={"amount": 285},
        )
        assert verdict["receipt_status"] == RECEIPT_VERIFIED_BY_MERCHANT
        assert verdict["payment_confirmed_allowed"] is True
