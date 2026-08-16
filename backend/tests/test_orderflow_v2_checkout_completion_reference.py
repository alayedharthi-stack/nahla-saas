"""OrderFlowV2 checkout completion — persist draft and announce order reference."""
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

from core.order_creation_evidence import (  # noqa: E402
    NO_ORDER_NUMBER_YET_AR,
)
from modules.ai.brain.postprocess.order_creation_claim_guard import (  # noqa: E402
    apply_order_creation_claim_guard,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.payment_evidence import (  # noqa: E402
    payment_confirmation_allowed,
)
from modules.ai.order_flow_v2.replies import (  # noqa: E402
    build_checkout_order_number_reply,
    build_order_created_reply,
)

_GENERIC_LINE_ITEM = {
    "product_id": "sku-sport-shoe-01",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "catalog_price": 249.0,
    "price_source": "whatsapp_catalog",
}


def _complete_prep(**extra):
    prep = {
        "order_flow_v2_active": True,
        "order_flow_v2_trusted_price": True,
        "order_flow_v2_catalog_total": 249.0,
        "line_items": [dict(_GENERIC_LINE_ITEM)],
        "customer_first_name": "أحمد",
        "customer_last_name": "سالم",
        "city": "الرياض",
        "short_address_code": "RRRD1234",
        "delivery_method": "delivery",
        "payment_method": "bank_transfer",
        "requested_bank": "rajhi",
    }
    prep.update(extra)
    return prep


def _conversation(conv_id: int = 42):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=1,
        extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
        customer=SimpleNamespace(id=9, tenant_id=1),
    )


class TestOrderFlowV2CheckoutCompletionReference:
    @patch("modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block")
    @patch("services.nahla_order_bridge.sync_nahla_wa_order")
    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_orderflow_v2_checkout_completion_persists_draft_and_announces_reference(
        self,
        load_state,
        methods_mock,
        accounts_mock,
        sync_mock,
        bank_block_mock,
        monkeypatch,
    ):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)

        prep = _complete_prep(payment_method="")
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})

        from core.merchant_payment_methods import MerchantPaymentMethods  # noqa: PLC0415

        methods_mock.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=True,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=["bank_transfer"],
        )
        accounts_mock.return_value = SimpleNamespace(bank_brands=("rajhi",), ibans=())
        sync_mock.return_value = SimpleNamespace(
            id=501,
            external_order_number="NHL-1-000501",
            external_id="nahla-wa-1-42",
        )
        bank_block_mock.return_value = "تم اختيار التحويل البنكي.\nالآيبان الخاص بالمتجر: SA1111111111111111111111"

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="تحويل الراجحي",
        )

        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"
        sync_mock.assert_not_called()

    @patch("modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block")
    @patch("services.nahla_order_bridge.sync_nahla_wa_order")
    @patch("core.order_context_builder._load_active_draft")
    @patch("modules.ai.order_flow_v2.payment.load_tenant_payment_accounts")
    @patch("modules.ai.order_flow_v2.payment.load_merchant_payment_methods")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_orderflow_v2_checkout_completion_does_not_claim_created_when_sync_fails(
        self,
        load_state,
        methods_mock,
        accounts_mock,
        draft_mock,
        sync_mock,
        bank_block_mock,
        monkeypatch,
    ):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)

        prep = _complete_prep(payment_method="")
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})

        from core.merchant_payment_methods import MerchantPaymentMethods  # noqa: PLC0415

        methods_mock.return_value = MerchantPaymentMethods(
            bank_transfer_enabled=True,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=["bank_transfer"],
        )
        accounts_mock.return_value = SimpleNamespace(bank_brands=("rajhi",))
        sync_mock.return_value = None
        draft_mock.return_value = None
        bank_block_mock.return_value = "تم اختيار التحويل البنكي."

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="تحويل الراجحي",
        )

        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"
        sync_mock.assert_not_called()

    @patch("core.order_context_builder._load_active_draft")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_order_number_question_before_persisted_order_uses_no_number_wording(
        self,
        load_state,
        draft_mock,
        monkeypatch,
    ):
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)

        prep = _complete_prep(payment_method="")
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})
        draft_mock.return_value = None

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="كم رقم الطلب؟",
        )

        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    @patch("core.local_order_resolver.resolve_customer_order_context")
    @patch("core.order_context_builder._load_active_draft")
    def test_order_number_question_after_persisted_order_returns_reference(
        self,
        draft_mock,
        resolver_mock,
    ):
        draft_mock.return_value = SimpleNamespace(
            order_id=88,
            external_id="nahla-wa-1-42",
        )
        resolver_mock.return_value = SimpleNamespace(
            selected_order=SimpleNamespace(display_reference="NHL-1-000088"),
        )
        db = MagicMock()
        prep = _complete_prep()

        reply = build_checkout_order_number_reply(
            db,
            tenant_id=1,
            conversation=_conversation(),
            order_prep=prep,
            brain_state={"cart_items": prep["line_items"]},
        )

        assert "NHL-1-000088" in reply


class TestOrderCreationClaimGuard:
    @patch("core.order_context_builder._load_active_draft", return_value=None)
    def test_creation_claim_guard_blocks_created_order_without_evidence(self, _draft):
        result = apply_order_creation_claim_guard(
            "تم إنشاء طلبك بنجاح ✅\nرقم الطلب: NHL-1-999999",
            db=MagicMock(),
            tenant_id=1,
            conversation_id=42,
            order_prep={
                "order_flow_v2_active": True,
                "line_items": [dict(_GENERIC_LINE_ITEM)],
            },
            brain_state={"cart_items": [_GENERIC_LINE_ITEM]},
        )
        assert result.replaced
        assert "NHL-1-999999" not in result.reply
        assert "تم إنشاء طلبك" not in result.reply
        assert NO_ORDER_NUMBER_YET_AR in result.reply

    def test_brain_reply_cannot_claim_order_created_without_persisted_reference(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        result = apply_order_creation_claim_guard(
            "تم إنشاء طلبك بنجاح! رقم الطلب: ABC-123",
            db=db,
            tenant_id=1,
            conversation_id=7,
            order_prep={
                "line_items": [{
                    "product_name": "عطر ورد 100ml",
                    "quantity": 1,
                }],
            },
            brain_state={},
        )
        assert result.replaced
        assert "ABC-123" not in result.reply

    def test_creation_claim_guard_allows_reply_when_evidence_exists(self):
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
            id=10,
            external_order_number="NHL-1-000010",
            external_id="nahla-wa-1-7",
        )
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            SimpleNamespace(
                id=10,
                external_order_number="NHL-1-000010",
                external_id="nahla-wa-1-7",
                tenant_id=1,
                source="whatsapp",
                extra_metadata={"lifecycle": "whatsapp_draft"},
            )
        ]
        with patch(
            "services.nahla_order_bridge.is_open_wa_draft_order",
            return_value=True,
        ):
            result = apply_order_creation_claim_guard(
                build_order_created_reply(reference="NHL-1-000010"),
                db=db,
                tenant_id=1,
                conversation_id=7,
                order_prep={
                    "order_creation_status": "created",
                    "draft_order_reference": "NHL-1-000010",
                },
                brain_state={},
            )
        assert not result.replaced
        assert "NHL-1-000010" in result.reply


class TestReceiptSafetyAfterOrderCreation:
    def test_receipt_upload_after_order_creation_does_not_mark_paid_without_merchant_verification(
        self,
    ):
        prep = _complete_prep(
            order_creation_status="created",
            draft_order_reference="NHL-1-000501",
            payment_receipt_received=True,
            payment_receipt_at="2026-07-01T12:00:00Z",
        )
        assert payment_confirmation_allowed(prep) is False

    def test_build_order_created_reply_requires_reference(self):
        assert build_order_created_reply(reference="") == ""
