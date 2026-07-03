"""Explicit intent priority over stale checkout rehydration — platform regressions."""
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

from modules.ai.brain.decision.actions import ACTION_CUSTOMER_LEDGER_REPLY  # noqa: E402
from modules.ai.checkout_authority import (  # noqa: E402
    LocalDraftEvidence,
    brain_payment_paths_should_defer_to_checkout_owner,
)
from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: E402
    PAYMENT_BARCODE_IMAGE_REQUEST,
    detect_explicit_non_checkout_intent,
    evaluate_stale_checkout_suppression,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402

_GENERIC_ITEM = {
    "product_id": "sku-perfume-rose",
    "product_name": "عطر ورد 100ml",
    "quantity": 1,
    "catalog_price": 180.0,
}


@pytest.fixture(autouse=True)
def _v2_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)


def _draft_evidence(*, ref: str = "NHL-1-000099") -> LocalDraftEvidence:
    return LocalDraftEvidence(
        order_id=88,
        external_id="nahla-wa-1-42",
        external_order_number=ref,
        status="draft",
        line_items=[dict(_GENERIC_ITEM)],
        total=180.0,
        currency="SAR",
        missing_fields=["customer_name", "city", "delivery_address", "payment_method"],
    )


def _conversation(conv_id: int = 42):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=1,
        extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
    )


def _run_v2(
    message: str,
    *,
    prep: dict | None = None,
    draft: LocalDraftEvidence | None = None,
    conv_id: int = 42,
):
    draft = draft if draft is not None else _draft_evidence()
    with patch(
        "modules.ai.order_flow_v2.owner.operational_tuple",
        return_value=(True, False, "test_mode_canary_enforcement"),
    ), patch(
        "modules.ai.order_flow_v2.owner.load_local_draft_evidence",
        return_value=draft,
    ), patch(
        "modules.ai.order_flow_v2.owner._load_brain_state",
        return_value=(_conversation(conv_id), {"order_prep": dict(prep or {})}),
    ):
        return try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message=message,
        )


class TestExplicitIntentDetection:
    def test_ledger_intent_detected(self) -> None:
        assert detect_explicit_non_checkout_intent("طلباتي السابقة كم؟") == "order_history_count"

    def test_track_intent_detected(self) -> None:
        assert detect_explicit_non_checkout_intent("وين طلبي؟") == "track_order"

    def test_barcode_intent_detected(self) -> None:
        assert (
            detect_explicit_non_checkout_intent("أرسل باركود الراجحي")
            == PAYMENT_BARCODE_IMAGE_REQUEST
        )

    def test_payment_info_intent_detected(self) -> None:
        assert detect_explicit_non_checkout_intent("كيف أحول على الراجحي؟") == "ask_payment_info"

    def test_catalog_browse_detected(self) -> None:
        assert detect_explicit_non_checkout_intent("وش عندكم منتجات؟") == "catalog_browse"

    def test_latest_order_summary_detected(self) -> None:
        assert detect_explicit_non_checkout_intent("وش آخر طلباتي؟") == "latest_order_summary"

    def test_explicit_order_ref_question_routes_track_not_checkout(self) -> None:
        assert detect_explicit_non_checkout_intent("كم رقم الطلب 269866315؟") == "track_order"

    def test_bare_order_number_question_is_checkout_not_bypass(self) -> None:
        assert detect_explicit_non_checkout_intent("كم رقم الطلب؟") == ""


class TestStaleCheckoutSuppression:
    @pytest.mark.parametrize(
        "message",
        [
            "طلباتي السابقة كم؟",
            "وين طلبي؟",
            "أرسل باركود الراجحي",
            "كيف أحول على الراجحي؟",
            "وش عندكم منتجات؟",
        ],
    )
    def test_explicit_intents_suppress_with_active_draft(self, message: str) -> None:
        decision = evaluate_stale_checkout_suppression(
            message=message,
            order_prep={"order_flow_v2_active": True, "line_items": [_GENERIC_ITEM]},
            missing_fields=["customer_name", "city", "delivery_address", "payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert decision.detected_intent

    @pytest.mark.parametrize(
        "message",
        [
            "نعم",
            "اعتمد نفس العنوان",
            "تحويل بنكي",
            "1",
        ],
    )
    def test_checkout_continuation_not_suppressed(self, message: str) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "order_flow_v2_last_field": "payment_method",
        }
        missing = ["payment_method"]
        if message == "1":
            prep["order_flow_v2_last_field"] = "quantity"
            missing = ["quantity"]
        decision = evaluate_stale_checkout_suppression(
            message=message,
            order_prep=prep,
            missing_fields=missing,
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is False

    def test_bare_order_number_question_not_suppressed_with_active_draft(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message="كم رقم الطلب؟",
            order_prep={"order_flow_v2_active": True, "line_items": [_GENERIC_ITEM]},
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is False
        assert decision.reason == "checkout_continuation_turn"

    def test_named_order_ref_question_suppresses_checkout(self) -> None:
        decision = evaluate_stale_checkout_suppression(
            message="كم رقم الطلب 269866315؟",
            order_prep={"order_flow_v2_active": True, "line_items": [_GENERIC_ITEM]},
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert decision.detected_intent == "track_order"


class TestOrderFlowV2BypassWithActiveDraft:
    def test_ledger_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("طلباتي السابقة كم؟")
        assert not result.handled
        assert result.reason.startswith("explicit_intent_suppressed:")

    def test_track_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("وين طلبي؟")
        assert not result.handled
        assert "track_order" in (result.reason or "")

    def test_barcode_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("أرسل باركود الراجحي")
        assert not result.handled
        assert PAYMENT_BARCODE_IMAGE_REQUEST in (result.reason or "")

    def test_payment_info_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("كيف أحول على الراجحي؟")
        assert not result.handled
        assert "ask_payment_info" in (result.reason or "")

    def test_catalog_browse_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("وش عندكم منتجات؟")
        assert not result.handled
        assert "catalog_browse" in (result.reason or "")

    def test_named_order_ref_bypasses_order_flow_v2(self) -> None:
        result = _run_v2("كم رقم الطلب 269866315؟")
        assert not result.handled
        assert "track_order" in (result.reason or "")

    def test_draft_order_number_still_owned_by_order_flow_v2(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
        }
        with patch(
            "modules.ai.order_flow_v2.owner.build_checkout_order_number_reply",
            return_value="رقم طلبك الحالي NHL-1-000099.",
        ):
            result = _run_v2("كم رقم الطلب؟", prep=prep)
        assert result.handled
        assert "NHL-1-000099" in result.reply

    def test_yes_still_owned_by_order_flow_v2(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "customer_first_name": "أحمد",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
        }
        result = _run_v2("نعم", prep=prep)
        assert result.handled

    def test_address_confirm_still_owned_by_order_flow_v2(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "customer_first_name": "أحمد",
            "customer_last_name": "سالم",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
        }
        with patch(
            "core.order_context_builder.build_order_context",
        ) as ctx_mock:
            ctx_mock.return_value = SimpleNamespace(
                known_previous_address=SimpleNamespace(
                    city="الرياض",
                    district="حي النخيل",
                    short_address_code="RRRD1234",
                ),
                shipping=SimpleNamespace(locked_by_merchant=False),
            )
            result = _run_v2("اعتمد نفس العنوان", prep=prep)
        assert result.handled
    def test_bank_transfer_method_answer_still_owned(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "customer_first_name": "أحمد",
            "customer_last_name": "سالم",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
            "delivery_address_status": "accepted",
            "order_flow_v2_last_field": "payment_method",
        }
        result = _run_v2("تحويل بنكي", prep=prep)
        assert result.handled
        assert "payment" in (result.reason or "").lower() or result.state_patch

    def test_numeric_slot_still_owned(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "order_flow_v2_last_field": "quantity",
        }
        result = _run_v2("1", prep=prep)
        assert result.handled

    def test_no_active_draft_routes_unchanged_for_greeting(self) -> None:
        with patch(
            "modules.ai.order_flow_v2.owner.operational_tuple",
            return_value=(True, False, "test_mode_canary_enforcement"),
        ), patch(
            "modules.ai.order_flow_v2.owner.load_local_draft_evidence",
            return_value=None,
        ), patch(
            "modules.ai.order_flow_v2.owner._load_brain_state",
            return_value=(_conversation(), {"order_prep": {}}),
        ):
            result = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=1,
                customer_phone="966500000001",
                message="مرحبا",
            )
        assert not result.handled
        assert result.reason == "greeting_no_pending"


class TestBrainPaymentDeferral:
    def test_payment_defer_false_for_explicit_barcode_with_active_draft(self) -> None:
        conv = _conversation()
        with patch(
            "modules.ai.checkout_authority.load_local_draft_evidence",
            return_value=_draft_evidence(),
        ):
            assert not brain_payment_paths_should_defer_to_checkout_owner(
                MagicMock(),
                tenant_id=1,
                conversation=conv,
                message="أرسل باركود الراجحي",
            )

    def test_payment_defer_true_for_checkout_continuation(self) -> None:
        conv = _conversation()
        conv.extra_metadata = {
            "brain_state": {
                "order_prep": {
                    "order_flow_v2_active": True,
                    "line_items": [dict(_GENERIC_ITEM)],
                    "order_flow_v2_last_field": "payment_method",
                }
            }
        }
        with patch(
            "modules.ai.checkout_authority.load_local_draft_evidence",
            return_value=_draft_evidence(),
        ):
            assert brain_payment_paths_should_defer_to_checkout_owner(
                MagicMock(),
                tenant_id=1,
                conversation=conv,
                message="تحويل بنكي",
            )


class TestDecisionEngineLedgerRoute:
    def test_decision_engine_routes_ledger_with_active_draft_context(self) -> None:
        from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: PLC0415
        from modules.ai.brain.intent import rules  # noqa: PLC0415
        from modules.ai.brain.types import BrainContext, CommerceFacts, MerchantConversationState  # noqa: PLC0415

        intent = rules.match("طلباتي السابقة كم؟")
        assert intent is not None
        state = MerchantConversationState()
        state.order_prep.line_items = [dict(_GENERIC_ITEM)]
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="طلباتي السابقة كم؟",
            intent=intent,
            state=state,
            facts=CommerceFacts(store_name="متجر تجريبي عام"),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
