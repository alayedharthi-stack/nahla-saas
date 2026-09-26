"""
tests/test_product_correction_topic_shift.py
──────────────────────────────────────────────
P0 regression: product correction, usage/information topic shift, checkout
guards, and long-OCR matcher guard.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

pytestmark = pytest.mark.governance_contract

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.dedup_order_state_gate import should_suppress_dedup_order_templates  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import _MISSING_FIELD_PROMPTS_AR  # noqa: E402
from modules.ai.brain.order_context_gate import try_fulfillment_lock_continuation  # noqa: E402
from modules.ai.brain.state.product_correction import (  # noqa: E402
    clear_stale_product_state_for_correction,
    detect_product_correction,
    extract_replacement_product_query,
    parse_product_correction,
)
from modules.ai.brain.state.product_information_topic import (  # noqa: E402
    detect_product_information_topic_shift,
    product_information_blocks_checkout,
)
from modules.ai.brain.state.state_relevance import (  # noqa: E402
    validate_state_relevance,
)
from modules.ai.knowledge.product_matcher import (  # noqa: E402
    CatalogProductForMatch,
    match_products,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    history: Optional[list] = None,
    orderable: bool = True,
    intent_name: str = "general",
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.5, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(orderable=orderable),
        history=list(history or []),
    )


def _stale_honey_state(*, title: str = "عسل طلح") -> MerchantConversationState:
    prep = OrderPreparationState(
        missing_fields=["city", "address_location"],
        product_id="27310682888555270",
    )
    return MerchantConversationState(
        stage="ordering",
        current_product_focus={
            "title": title,
            "id": 27310682888555270,
            "external_id": "27310682888555270",
            "price": 120,
        },
        order_prep=prep,
    )


class TestProductCorrectionDetection:
    def test_negation_detected(self) -> None:
        assert detect_product_correction("لا مو عسل طلح")

    def test_replacement_extracted(self) -> None:
        q = extract_replacement_product_query(
            "لا مش عسل طلح، عسل خلطة منتجات العسل",
        )
        assert "خلطة" in q
        assert "عسل" in q

    def test_clear_stale_state(self) -> None:
        state = _stale_honey_state()
        clear_stale_product_state_for_correction(state)
        assert state.current_product_focus is None
        assert state.order_prep.product_id == ""
        assert state.order_prep.missing_fields == []


class TestProductCorrectionClearsStaleFocus:
    def test_correction_blocks_checkout_and_dedup(self) -> None:
        msg = "لا مو عسل طلح"
        state = _stale_honey_state()
        clear_stale_product_state_for_correction(state)
        ctx = _ctx(msg, state=state)
        ctx.state_relevance = validate_state_relevance(ctx)

        assert ctx.state_relevance.product_correction_topic_shift is True
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action == ACTION_LLM_REPLY

        suppress, reason = should_suppress_dedup_order_templates(
            message=msg,
            summary={
                "selected_product": "عسل طلح",
                "stage": "ordering",
                "missing_fields": ["city"],
            },
        )
        assert suppress is True
        assert reason == "product_correction"


class TestReplacementProductReResolves:
    def test_replacement_routes_to_search(self) -> None:
        msg = "لا مش عسل طلح، عسل خلطة منتجات العسل"
        state = _stale_honey_state()
        clear_stale_product_state_for_correction(state)
        ctx = _ctx(msg, state=state)
        ctx.state_relevance = validate_state_relevance(ctx)

        verdict = parse_product_correction(msg)
        assert verdict.replacement_query

        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert "خلطة" in str(decision.args.get("query") or "")
        assert decision.args.get("source") == "product_correction"
        assert "عسل طلح" not in str(decision.reason or "")


class TestUsageQuestionBlocksCheckout:
    def test_usage_question_routes_to_llm_not_checkout(self) -> None:
        msg = "ياليت تخبرني طريقة استخدام عسل طلح الصحيحة"
        state = _stale_honey_state()
        state.turn = 2
        state.product_focus_turn = 1
        ctx = _ctx(msg, state=state, intent_name="general")
        ctx.state_relevance = validate_state_relevance(ctx)

        assert detect_product_information_topic_shift(msg, state=state)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _MISSING_FIELD_PROMPTS_AR["city"] not in (
            decision.args.get("reply") or ""
        )
        assert "أرسل رابط الموقع" not in (decision.args.get("reply") or "")

    def test_fulfillment_lock_blocked_on_usage(self) -> None:
        msg = "ياليت تخبرني طريقة استخدام عسل طلح الصحيحة"
        ctx = _ctx(msg, state=_stale_honey_state())
        ctx.state_relevance = validate_state_relevance(ctx)
        assert try_fulfillment_lock_continuation(ctx) is None


class TestLocationAfterUnresolvedUsage:
    def test_location_does_not_resume_checkout(self) -> None:
        history = [
            {
                "direction": "inbound",
                "body": "ياليت تخبرني طريقة استخدام عسل طلح الصحيحة",
            },
        ]
        msg = "الرياض حي النرجس"
        ctx = _ctx(msg, state=_stale_honey_state(), history=history)
        ctx.state_relevance = validate_state_relevance(ctx)

        assert product_information_blocks_checkout(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert try_fulfillment_lock_continuation(ctx) is None


class TestLongOcrMatcherGuard:
    _OCR = "خلطة منتجات النحل بعسل الطلح لمشاكل الخصوبة وللمتزوجين"

    @pytest.fixture()
    def catalog(self) -> list[CatalogProductForMatch]:
        return [
            CatalogProductForMatch(
                id=1,
                title="عسل طلح",
                sku=None,
                external_id=None,
            ),
            CatalogProductForMatch(
                id=2,
                title="خلطة منتجات النحل بعسل الطلح لمشاكل الخصوبة وللمتزوجين",
                sku=None,
                external_id=None,
            ),
        ]

    def test_does_not_collapse_to_generic_honey(
        self,
        catalog: list[CatalogProductForMatch],
    ) -> None:
        matches = match_products(self._OCR, catalog, min_confidence=0.4)
        assert matches
        assert matches[0].product_id == 2
        assert matches[0].title != "عسل طلح"
        generic_ids = {m.product_id for m in matches if m.title == "عسل طلح"}
        assert generic_ids == set()
