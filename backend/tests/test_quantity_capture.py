"""Regression — active-order quantity slot must reach DraftOrderHandler.

Live acceptance run 2026-07-28 (tenant 1): «كميتين» extracted ``quantity=2``
but fell through to ``ACTION_LLM_REPLY`` because ``facts.orderable=False`` and
``last_search_candidates`` blocked the existing continuation gates.
"""
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
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    _apply_quantity_from_message,
    _order_prep_export_dict,
)
from modules.ai.brain.intent.ordering_extractor import extract_ordering_quantity  # noqa: E402
from modules.ai.brain.state.stages import STAGE_DISCOVERY, STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_PICK_LIST_ITEM,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
DRESS_ID = "4"
DRESS = {
    "id": 4,
    "title": "فستان سادة",
    "external_id": DRESS_ID,
    "price": 114.0,
    "can_checkout": True,
}
ALT_CANDIDATES = [
    DRESS,
    {"id": 289, "title": "فستان", "external_id": "289", "price": 99.0},
    {"id": 5, "title": "فستان آخر", "external_id": "5", "price": 150.0},
    {"id": 6, "title": "فستان ثالث", "external_id": "6", "price": 180.0},
]
SIZE_OPTION = {
    "المقاس": {
        "option_id": 1,
        "option_name": "المقاس",
        "value_id": 2002,
        "value_name": "42 - L",
    }
}


def _acceptance_prep(*, quantity: int = 1) -> OrderPreparationState:
    return OrderPreparationState(
        product_id=DRESS_ID,
        quantity=quantity,
        city="الرياض",
        short_address_code="RRRD1234",
        missing_fields=[],
        order_creation_status="creating",
        line_items=[
            {
                "product_id": DRESS_ID,
                "product_name": "فستان سادة",
                "quantity": quantity,
                "unit_price": 114.0,
                "variant": "42 - L",
            }
        ],
        product_options=dict(SIZE_OPTION),
    )


def _ctx(
    message: str,
    *,
    stage: str = STAGE_ORDERING,
    prep: OrderPreparationState | None = None,
    candidates: list | None = ALT_CANDIDATES,
    focus: dict | None = None,
    intent: Intent | None = None,
    orderable: bool = False,
) -> BrainContext:
    state = MerchantConversationState(
        stage=stage,
        greeted=True,
        order_prep=prep,
        current_product_focus=focus if focus is not None else dict(DRESS),
        last_search_candidates=candidates,
    )
    if intent is None:
        slots = {}
        qty = extract_ordering_quantity(message)
        if qty is not None:
            slots["quantity"] = qty
        intent = Intent(
            name="general",
            confidence=0.72,
            raw_message=message,
            slots=slots,
        )
    return BrainContext(
        tenant_id=1,
        customer_phone="966555906901",
        message=message,
        intent=intent,
        state=state,
        facts=CommerceFacts(
            has_products=True,
            product_count=12,
            orderable=orderable,
            store_name=GENERIC_MERCHANT,
        ),
    )


def _decide(message: str, **kwargs):
    return DefaultDecisionEngine().decide(_ctx(message, **kwargs))


class TestQuantityDecisionGate:
    def test_kmityn_routes_to_draft_order_not_llm(self) -> None:
        decision = _decide(
            "كميتين",
            prep=_acceptance_prep(),
            orderable=False,
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_LLM_REPLY
        assert "quantity" in (decision.reason or "")

    def test_active_candidates_do_not_block_quantity_gate(self) -> None:
        decision = _decide("كميتين", prep=_acceptance_prep(), candidates=ALT_CANDIDATES)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_pre_commerce_shortcut_orderable_false_still_routes(self) -> None:
        decision = _decide("كميتين", prep=_acceptance_prep(), orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    @pytest.mark.parametrize(
        "message,slots",
        [
            ("كميتين", {"quantity": 2}),
            ("حبتين", {"quantity": 2}),
            ("أبغى 2", {"quantity": 2}),
            ("الكمية 2", {"quantity": 2}),
            ("عدد 2", {"quantity": 2}),
        ],
    )
    def test_equivalent_phrases_with_quantity_slot(
        self, message: str, slots: dict,
    ) -> None:
        intent = Intent(
            name="general",
            confidence=0.72,
            raw_message=message,
            slots=slots,
        )
        decision = DefaultDecisionEngine().decide(
            _ctx(message, prep=_acceptance_prep(), intent=intent),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER, message

    def test_114_not_quantity_when_it_matches_unit_price(self) -> None:
        decision = _decide(
            "114",
            prep=_acceptance_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="114",
                slots={"quantity": 114},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_option_value_not_quantity(self) -> None:
        decision = _decide(
            "42 - L",
            prep=_acceptance_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="42 - L",
                slots={},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_bare_ordinal_with_candidates_not_quantity(self) -> None:
        decision = _decide(
            "2",
            prep=_acceptance_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="2",
                slots={"quantity": 2},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_quantity_outside_active_order_does_not_route(self) -> None:
        decision = _decide(
            "كميتين",
            stage=STAGE_DISCOVERY,
            prep=None,
            candidates=[],
            focus=None,
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_candidates_without_focus_or_prep_do_not_route(self) -> None:
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            last_search_candidates=ALT_CANDIDATES,
        )
        intent = Intent(
            name="general",
            confidence=0.72,
            raw_message="كميتين",
            slots={"quantity": 2},
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966555906901",
            message="كميتين",
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_pick_list_item_not_quantity(self) -> None:
        intent = Intent(
            name=INTENT_PICK_LIST_ITEM,
            confidence=0.95,
            raw_message="2",
            slots={"list_index": 2, "quantity": 2},
        )
        decision = DefaultDecisionEngine().decide(
            _ctx("2", prep=_acceptance_prep(), intent=intent),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER or "quantity" not in (
            decision.reason or ""
        )

    def test_invalid_zero_quantity_not_routed(self) -> None:
        intent = Intent(
            name="general",
            confidence=0.72,
            raw_message="0",
            slots={"quantity": 0},
        )
        decision = DefaultDecisionEngine().decide(
            _ctx("0", prep=_acceptance_prep(), intent=intent),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_same_quantity_not_routed(self) -> None:
        decision = _decide(
            "كميتين",
            prep=_acceptance_prep(quantity=2),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


class TestQuantityHandlerPersistence:
    def test_apply_quantity_updates_line_item_and_total(self) -> None:
        prep = _acceptance_prep()
        assert _apply_quantity_from_message(prep, "كميتين") is True
        assert prep.quantity == 2
        assert prep.line_items[0]["quantity"] == 2
        exported = _order_prep_export_dict(prep)
        assert exported["price"] == 114.0
        assert exported["total_price"] == 228.0

    def test_product_size_city_address_preserved(self) -> None:
        prep = _acceptance_prep()
        _apply_quantity_from_message(prep, "كميتين")
        assert prep.product_id == DRESS_ID
        assert prep.city == "الرياض"
        assert prep.short_address_code == "RRRD1234"
        assert prep.product_options["المقاس"]["value_name"] == "42 - L"
        assert prep.product_options["المقاس"]["value_id"] == 2002

    def test_quantity_not_applied_twice(self) -> None:
        prep = _acceptance_prep()
        assert _apply_quantity_from_message(prep, "كميتين") is True
        assert _apply_quantity_from_message(prep, "كميتين") is False
        assert prep.quantity == 2

    def test_decision_gate_and_writer_chain_for_acceptance_state(self) -> None:
        prep = _acceptance_prep()
        decision = _decide("كميتين", prep=prep, orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert _apply_quantity_from_message(prep, "كميتين") is True
        exported = _order_prep_export_dict(prep)
        assert exported["quantity"] == 2
        assert exported["total_price"] == 228.0
        assert prep.line_items[0]["quantity"] == 2
