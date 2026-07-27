"""Active in-flight order review routing — not DB track_order."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.stages import STAGE_DISCOVERY, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.order_flow_v2.replies import build_resume_ack  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "نورة عبدالله"
SHIRT = {
    "title": "قميص قطني أزرق",
    "external_id": "ext-shirt-1",
    "id": 1,
    "can_checkout": True,
    "price": 129.0,
}

REVIEW_PHRASES = [
    "راجع طلبي",
    "راجع الطلب",
    "ورني طلبي",
    "اعرض طلبي",
    "وش طلبي الحالي",
    "إيش طلبت",
    "ملخص الطلب",
]

REVIEW_VARIANTS = [
    "راجع  طلبي",
    "وريني طلبي",
    "اعرض  الطلب",
    "ابي اشوف طلبي",
]

TRACK_PHRASES = [
    "وين طلبي؟",
    "تتبع طلبي",
    "حالة الطلب",
]

COMPLETION_PHRASES = [
    "أبي أكمل الطلب",
    "كمل الطلب",
    "أبغى أتم الطلب",
    "اعتمد الطلب",
]

# Baseline (before fix) with active order — recorded from main ac16d52a behaviour.
BASELINE_WITH_ACTIVE_ORDER = {
    "راجع طلبي": ACTION_TRACK_ORDER,
    "راجع الطلب": ACTION_PROPOSE_DRAFT_ORDER,
    "ورني طلبي": ACTION_TRACK_ORDER,
    "اعرض طلبي": ACTION_TRACK_ORDER,
    "وش طلبي الحالي": "llm_reply",
    "إيش طلبت": "llm_reply",
    "ملخص الطلب": ACTION_PROPOSE_DRAFT_ORDER,
    "وين طلبي؟": ACTION_TRACK_ORDER,
    "تتبع طلبي": ACTION_TRACK_ORDER,
    "حالة الطلب": ACTION_TRACK_ORDER,
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _line_items() -> list[dict]:
    return [
        {
            "product_name": "قميص قطني أزرق (مقاس L)",
            "quantity": 2,
            "catalog_price": 129.0,
            "item_price": 129.0,
        },
    ]


def _ctx(
    message: str,
    *,
    stage: str = STAGE_ORDERING,
    with_prep: bool = True,
    line_items: list | None = None,
    catalog_total: float | None = None,
) -> BrainContext:
    prep = None
    if with_prep:
        kwargs: dict = dict(
            product_id="ext-shirt-1",
            customer_first_name=GENERIC_CUSTOMER.split()[0],
            customer_phone="966500000002",
            city="الرياض",
            order_status="awaiting_address",
            quantity=2,
        )
        if line_items is not None:
            kwargs["line_items"] = line_items
        if catalog_total is not None:
            kwargs["catalog_checkout_total"] = catalog_total
        prep = OrderPreparationState(**kwargs)
    state = MerchantConversationState(
        stage=stage,
        greeted=True,
        order_prep=prep,
        current_product_focus=dict(SHIRT) if with_prep else None,
    )
    intent = rules.match(message) or Intent(
        name="general", confidence=0.5, raw_message=message,
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000002",
        message=message,
        intent=intent,
        state=state,
        facts=_facts(),
    )


def _decide(message: str, **kwargs):
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


class TestActiveOrderReviewRouting:
    @pytest.mark.parametrize("phrase", REVIEW_PHRASES)
    def test_review_phrase_with_active_order_not_track_order(self, phrase: str) -> None:
        decision = _decide(phrase, with_prep=True)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER, phrase
        assert decision.action != ACTION_TRACK_ORDER, phrase
        assert decision.args.get("review_in_flight_order") is True
        assert "active_order_review" in (decision.reason or "")

    @pytest.mark.parametrize("phrase", TRACK_PHRASES)
    def test_track_phrases_stay_track_order(self, phrase: str) -> None:
        decision = _decide(phrase, with_prep=True)
        assert decision.action == ACTION_TRACK_ORDER, phrase

    def test_review_without_active_order_does_not_invent_order(self) -> None:
        decision = _decide("راجع طلبي", with_prep=False, stage=STAGE_DISCOVERY)
        assert decision.action == ACTION_TRACK_ORDER
        assert decision.args.get("review_in_flight_order") is not True

    @pytest.mark.parametrize("variant", REVIEW_VARIANTS)
    def test_review_dialect_and_spacing_variants(self, variant: str) -> None:
        decision = _decide(variant, with_prep=True)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_TRACK_ORDER

    def test_summary_reflects_explicit_state(self) -> None:
        order_prep = {
            "product_id": "ext-shirt-1",
            "customer_first_name": GENERIC_CUSTOMER.split()[0],
            "order_status": "awaiting_address",
            "quantity": 2,
            "line_items": _line_items(),
            "order_total": 258.0,
        }
        brain_state: dict = {}
        reply = build_resume_ack(
            order_prep=order_prep,
            brain_state=brain_state,
            missing_fields=["city"],
        )
        assert "قميص قطني أزرق" in reply
        assert "مقاس L" in reply
        assert "129" in reply
        assert "2" in reply
        assert "258" in reply

    @pytest.mark.parametrize("phrase", COMPLETION_PHRASES)
    def test_completion_phrases_still_propose_draft_order(self, phrase: str) -> None:
        decision = _decide(phrase, with_prep=True)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER, phrase
        assert decision.action != ACTION_SEARCH_PRODUCTS, phrase

    def test_before_after_routing_table_documented(self) -> None:
        """Sanity: every review phrase moved off track_order / llm inquiry."""
        for phrase in REVIEW_PHRASES:
            before = BASELINE_WITH_ACTIVE_ORDER[phrase]
            after = _decide(phrase, with_prep=True).action
            assert after == ACTION_PROPOSE_DRAFT_ORDER
            assert before in {ACTION_TRACK_ORDER, "llm_reply", ACTION_PROPOSE_DRAFT_ORDER}
            if before == ACTION_TRACK_ORDER:
                assert after != ACTION_TRACK_ORDER
            if before == "llm_reply":
                assert after != "llm_reply"
