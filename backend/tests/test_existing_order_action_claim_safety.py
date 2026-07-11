"""PR B — action-claim safety and placed-order protection regressions."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    CommerceTurnContract,
    build_commerce_turn_contract,
    is_placed_order_statement,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
)
from modules.ai.brain.postprocess.order_creation_claim_guard import (  # noqa: E402
    apply_order_creation_claim_guard,
)
from modules.ai.brain.postprocess.shipment_truth_guard import (  # noqa: E402
    apply_shipment_truth_guard,
    detect_ungrounded_shipment_claim_kinds,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_ORDER_REF = "284719365"


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=3,
        in_stock_count=3,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(message: str, *, history: list | None = None) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        message=message,
        intent=Intent(name="ask_product", confidence=0.8, raw_message=message),
        state=MerchantConversationState(),
        facts=_facts(),
        history=history or [],
        commerce_bundle={},
        customer_phone="+966501112233",
    )


class TestCompensationClaims:
    def test_b1_no_trusted_coupon_scrubs_discount_promise(self) -> None:
        reply = "نعتذر عن التأخير وسنعطيك خصم 10% على طلبك القادم"
        result = apply_order_creation_claim_guard(reply)
        assert result.replaced is True
        assert "10%" not in result.reply

    def test_b2_trusted_coupon_allows_grounded_discount(self) -> None:
        reply = "تم تطبيق كود الخصم بنسبة 10%"
        result = apply_order_creation_claim_guard(
            reply,
            brain_state={"coupon_id": "SAVE10", "discount_applied": True},
        )
        assert result.replaced is False
        assert "10%" in result.reply


class TestShippingModificationClaims:
    def test_b3_unsupported_carrier_change_scrubbed(self) -> None:
        reply = "تم تغيير شركة الشحن إلى أرامكس كما طلبت"
        kinds = detect_ungrounded_shipment_claim_kinds(reply)
        assert "ungrounded_carrier_change" in kinds
        result = apply_shipment_truth_guard(reply=reply)
        assert result.replaced is True
        assert "أرامكس" not in result.reply

    def test_b4_successful_carrier_change_allowed_with_execution_metadata(self) -> None:
        reply = "تم تغيير شركة الشحن إلى أرامكس بنجاح"
        result = apply_shipment_truth_guard(
            reply=reply,
            extra_metadata={
                "last_execution": {
                    "action": "change_shipping_carrier",
                    "success": True,
                }
            },
        )
        assert result.replaced is False


class TestPlacedOrderProtection:
    @pytest.mark.parametrize(
        "message",
        ["خلاص طلبت", "أنا طلبت", "تم الطلب"],
    )
    def test_b5_b6_placed_order_statements_detected(self, message: str) -> None:
        assert is_placed_order_statement(message) is True

    def test_b5_blocks_checkout_continuation(self) -> None:
        ctx = _ctx("خلاص طلبت")
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("placed_order_support_only") is True
        raw = SimpleNamespace(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={"product": {"title": "حذاء رياضي أبيض"}},
            reason="test",
            confidence=0.9,
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"

    def test_b7_existing_order_product_list_stays_support(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        ctx = _ctx("الطلب فيه حذاء رياضي أبيض", history=history)
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is True
        raw = SimpleNamespace(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "حذاء"},
            reason="test",
            confidence=0.8,
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY


class TestAvailabilityGuard:
    def test_b8_existing_order_thread_exempt_from_availability_rewrite(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        exempt = inbound_exempt_from_availability_rewrite(
            "الطلب فيه حذاء رياضي أبيض",
            availability_context={"history": history},
        )
        assert exempt is True


class TestRegressionIntact:
    def test_b10_same_order_confirmation_still_works(self) -> None:
        from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_same_order_confirmation,
        )

        assert is_same_order_confirmation("نفس الطلب") is True
