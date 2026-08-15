"""Platform interference / silent-path recovery.

Asserts ownership, evidence, capability, and outbound integrity.
Does not assert exact customer-facing prose.
Does not add phrase routers.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    should_restore_brain_reply_after_dedup_silence,
)
from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    should_block_catalog_grounding_fallback,
)
from modules.ai.brain.commerce.store_inquiry_compose_guard import (  # noqa: E402
    reconcile_store_link_body_when_url_found,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.order_context_gate import (  # noqa: E402
    should_skip_catalog_preload,
)
from modules.ai.brain.postprocess.catalog_browse_silent_recovery import (  # noqa: E402
    is_catalog_browse_silent_recovery_message,
    try_catalog_browse_silent_recovery,
)
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.merchant_knowledge_unknown_truth_guard import (  # noqa: E402
    apply_merchant_knowledge_unknown_truth_guard,
)
from modules.ai.brain.turn_owner_contract import (  # noqa: E402
    POSTPROCESS_CATALOG_GROUNDING,
    build_turn_owner_contract,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRODUCT,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_TRACK_ORDER,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

STORE_URL = "https://shop.example.test/demo-store"
GENERIC_DRESS = {
    "id": 21,
    "title": "فستان سهرة",
    "in_stock": True,
    "can_checkout": True,
    "external_id": "ext-dress",
    "image_url": "https://cdn.example.test/dress.jpg",
    "price": 180,
}


def _intent(name: str, message: str = "") -> Intent:
    return Intent(name=name, confidence=0.93, raw_message=message)


def _ctx(message: str, *, intent_name: str) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966555000001",
        message=message,
        intent=_intent(intent_name, message),
        state=MerchantConversationState(stage="exploring", turn=4, greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True, store_name="متجر تجريبي عام"),
        history=[],
        profile={"inbound_metadata": {}},
        commerce_bundle={},
    )


@pytest.fixture(autouse=True)
def _enforce_grounding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE", "enforce")


class TestLatestOrderNotCatalogFallback:
    def test_valid_llm_order_contents_are_not_rewritten(self) -> None:
        llm = (
            "آخر طلب لك كان فيه:\n"
            "- فستان × 1\n"
            "- فستان × 3\n"
            "الحالة: قيد التنفيذ."
        )
        result = apply_catalog_product_grounding_guard(
            reply=llm,
            inbound_text="آخر طلب لي وش كان فيه؟",
            inbound_metadata={"intent": INTENT_LATEST_ORDER_SUMMARY},
            intent=_intent(INTENT_LATEST_ORDER_SUMMARY, "آخر طلب لي وش كان فيه؟"),
        )
        assert result.replaced is False
        assert "الخيارات المؤكدة من الكتالوج" not in result.reply
        assert "فستان" in result.reply

    def test_fragment_guard_blocks_order_owner(self) -> None:
        blocked, reason = should_block_catalog_grounding_fallback(
            inbound_text="آخر طلب لي وش كان فيه؟",
            intent=_intent(INTENT_LATEST_ORDER_SUMMARY),
        )
        assert blocked is True
        assert reason == "order_evidence_owner"

    def test_engine_marks_latest_order_as_order_owner(self) -> None:
        ctx = _ctx("آخر طلب لي وش كان فيه؟", intent_name=INTENT_LATEST_ORDER_SUMMARY)
        decision = DefaultDecisionEngine().decide(ctx)
        contract = build_turn_owner_contract(decision)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "latest_order_summary"
        assert decision.args.get("block_catalog_push") is True
        assert contract.blocks(POSTPROCESS_CATALOG_GROUNDING)
        result = apply_catalog_product_grounding_guard(
            reply="آخر طلب: فستان × 1 — الحالة قيد التنفيذ.",
            inbound_text=ctx.message,
            inbound_metadata={"turn_owner_contract": contract.to_metadata()},
            intent=ctx.intent,
        )
        assert result.replaced is False


class TestPreviousOrdersKeepEvidenceText:
    def test_status_headers_are_not_stripped(self) -> None:
        llm = (
            "هذه طلباتك السابقة:\n"
            "1. **طلبك الحالي:**\n"
            "- فستان × 1\n"
            "2. **طلبات سابقة:**\n"
            "- تنورة: ملغاة"
        )
        result = apply_catalog_product_grounding_guard(
            reply=llm,
            inbound_text="وش طلباتي السابقة؟",
            inbound_metadata={"intent": INTENT_TRACK_ORDER, "topic": "order_history"},
            intent=_intent(INTENT_TRACK_ORDER, "وش طلباتي السابقة؟"),
        )
        assert result.replaced is False
        assert "طلبك الحالي" in result.reply
        assert "الخيارات المؤكدة" not in result.reply

    def test_engine_previous_orders_block_catalog_push(self) -> None:
        ctx = _ctx("وش طلباتي السابقة؟", intent_name=INTENT_TRACK_ORDER)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "order_history"
        assert decision.args.get("block_catalog_push") is True


class TestCurrentOrderKnownGood:
    def test_current_order_stays_on_track_order(self) -> None:
        ctx = _ctx("وين طلبي الحالي؟", intent_name=INTENT_TRACK_ORDER)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER
        assert decision.action != ACTION_LLM_REPLY


class TestFulfillmentLockDoesNotStarveCatalog:
    def test_ask_product_does_not_skip_catalog_preload(self) -> None:
        state = MerchantConversationState(stage="ordering", turn=8, greeted=True)
        state.order_prep = OrderPreparationState(
            customer_first_name="أحمد",
            customer_last_name="سالم",
        )
        intent = _intent(INTENT_ASK_PRODUCT, "أبي شيء حلو هدية")
        assert should_skip_catalog_preload(
            message="أبي شيء حلو هدية",
            state=state,
            intent=intent,
        ) is False
        assert should_skip_catalog_preload(
            message="طيب نرجع للتسوق",
            state=state,
            intent=intent,
        ) is False


class TestDedupDoesNotSilenceShoppingReturn:
    def test_return_to_shopping_restores_composed_reply(self) -> None:
        candidate = "أكيد، نرجع للتسوق! وش النوع اللي تحب تختارها؟"
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound="طيب نرجع للتسوق",
            candidate_reply=candidate,
            previous_outbound="حالة رقم الطلب 257404293: قيد التنفيذ",
        ) is True

    def test_pure_greeting_still_does_not_restore(self) -> None:
        assert should_restore_brain_reply_after_dedup_silence(
            current_inbound="صباح الخير",
            candidate_reply="صباح النور! 👋",
            previous_outbound="صباح النور! 👋",
        ) is False


class TestVisualReplayNotEmptiedByDeixis:
    def test_visual_replay_keeps_imageable_products(self) -> None:
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": "وريني صورته",
                "after_search": "product_visual",
                "force_product_card": True,
                "replay_candidates": [dict(GENERIC_DRESS)],
            },
            reason="product visual — imageable catalog candidates",
            confidence=0.9,
        )
        ctx = _ctx("وريني صورته", intent_name=INTENT_PRODUCT_VISUAL_REQUEST)
        result = asyncio.run(ProductSearchHandler().handle(decision, ctx))
        assert result.success is True
        products = list(result.data.get("products") or [])
        assert products
        assert products[0]["title"] == GENERIC_DRESS["title"]
        assert products[0]["image_url"]
        assert result.data.get("query") == GENERIC_DRESS["title"]

    def test_visual_request_is_not_catalog_browse_silent_recovery(self) -> None:
        assert is_catalog_browse_silent_recovery_message("وريني صورته") is False
        assert try_catalog_browse_silent_recovery(inbound_text="وريني صورته") is None


class TestStoreLinkFragmentIntegrity:
    def test_no_url_claim_strip_does_not_leave_orphan_temporal(self) -> None:
        body = "ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً."
        result = reconcile_store_link_body_when_url_found(body, STORE_URL)
        assert result.store_url == STORE_URL
        assert STORE_URL in result.body
        assert result.body.strip() != "حالياً"
        assert not result.body.strip().startswith("حاليا")


class TestShippingEtaUnknownInference:
    def test_city_method_inference_is_stripped_when_eta_unknown(self) -> None:
        raw = (
            "ما عندي معلومات مؤكدة عن مدة الشحن حالياً. "
            "لكن عادةً الشحن يعتمد على المدينة وطريقة الشحن المختارة."
        )
        result = apply_merchant_knowledge_unknown_truth_guard(
            raw,
            decision_args={
                "topic": "shipping_eta",
                "question_kind": "shipping_eta",
                "answer_contract": {
                    "fact_kind": "shipping_eta",
                    "status": "UNKNOWN",
                    "forbidden_inferences": ["city_variation_without_evidence"],
                },
                "retrieval_count": 0,
            },
        )
        assert result.scrubbed is True
        assert "مدينة" not in result.reply
        assert "ما عندي معلومات مؤكدة" in result.reply
