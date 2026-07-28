"""Regression — active-order pending option value must reach DraftOrderHandler.

Live acceptance run 2026-07-29 (tenant 1): «42 - L» fell through to
``ACTION_LLM_REPLY`` because ``facts.orderable=False`` and
``last_search_candidates`` blocked existing continuation gates.
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
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import (  # noqa: E402
    _merge_message_options,
    _missing_checkout_fields,
    _order_prep_export_dict,
)
from modules.ai.brain.intent.ordering_extractor import (  # noqa: E402
    extract_ordering_quantity,
    extract_ordering_slots,
)
from modules.ai.brain.observability.order_flow_evidence import detect_input_types  # noqa: E402
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
OPTION_META = [
    {
        "id": 1,
        "name": "المقاس",
        "required": True,
        "values": [{"id": 2002, "name": "42 - L", "price": 114}],
    }
]


def _t3_prep(*, quantity: int = 1) -> OrderPreparationState:
    """Product picked, size still pending — mirrors acceptance t2→t3 handoff."""
    return OrderPreparationState(
        product_id=DRESS_ID,
        quantity=quantity,
        product_options={},
        product_options_meta=list(OPTION_META),
        product_has_required_options=True,
        product_options_loaded=True,
        missing_fields=["customer_first_name", "city", "address_location"],
        order_creation_status="creating",
        line_items=[
            {
                "product_id": DRESS_ID,
                "product_name": "فستان سادة",
                "quantity": quantity,
                "unit_price": 114.0,
            }
        ],
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
    checkout_url: str = "",
) -> BrainContext:
    state = MerchantConversationState(
        stage=stage,
        greeted=True,
        order_prep=prep,
        current_product_focus=focus if focus is not None else dict(DRESS),
        last_search_candidates=candidates,
        checkout_url=checkout_url,
        pending_option_groups=["المقاس"],
    )
    if intent is None:
        slots = extract_ordering_slots(message) or {}
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


def _apply_writer(prep: OrderPreparationState, message: str) -> int:
    return _merge_message_options(prep, message)


class TestOptionDecisionGate:
    def test_size_message_routes_to_draft_order_not_llm(self) -> None:
        decision = _decide("42 - L", prep=_t3_prep(), orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_LLM_REPLY
        assert "option" in (decision.reason or "").lower()

    def test_writer_records_size_in_selected_options(self) -> None:
        prep = _t3_prep()
        assert _apply_writer(prep, "42 - L") == 1
        assert prep.product_options["المقاس"]["value_name"] == "42 - L"

    def test_writer_records_value_id_2002(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        assert prep.product_options["المقاس"]["value_id"] == 2002

    def test_product_id_4_preserved(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        assert prep.product_id == DRESS_ID
        assert prep.line_items[0]["product_id"] == DRESS_ID

    def test_unit_price_114_preserved(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        exported = _order_prep_export_dict(prep)
        assert exported["price"] == 114.0
        assert prep.line_items[0]["unit_price"] == 114.0

    def test_quantity_1_preserved(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        assert prep.quantity == 1
        assert prep.line_items[0]["quantity"] == 1

    def test_collection_continues_from_name_slot(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        missing = _missing_checkout_fields(prep, is_sa=True)
        assert "customer_first_name" in missing
        assert prep.product_options.get("المقاس")

    def test_no_search_or_reselection(self) -> None:
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            greeted=True,
            order_prep=_t3_prep(),
            current_product_focus=dict(DRESS),
            last_search_candidates=ALT_CANDIDATES,
        )
        decision = DefaultDecisionEngine().decide(
            BrainContext(
                tenant_id=1,
                customer_phone="966555906901",
                message="42 - L",
                intent=Intent(name="general", confidence=0.72, raw_message="42 - L"),
                state=state,
                facts=CommerceFacts(has_products=True, orderable=False),
            )
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_product_289_not_selected(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        assert prep.product_id == DRESS_ID
        assert str(prep.product_id) != "289"

    def test_active_candidates_do_not_block_option_gate(self) -> None:
        decision = _decide("42 - L", prep=_t3_prep(), candidates=ALT_CANDIDATES)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_pre_commerce_shortcut_orderable_false_still_routes(self) -> None:
        decision = _decide("42 - L", prep=_t3_prep(), orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_size_not_consumed_as_quantity(self) -> None:
        assert extract_ordering_quantity("42 - L") is None
        decision = _decide(
            "42 - L",
            prep=_t3_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="42 - L",
                slots={"quantity": 42},
            ),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_kmityn_stays_quantity_not_option(self) -> None:
        prep = _t3_prep()
        _apply_writer(prep, "42 - L")
        decision = _decide("كميتين", prep=prep)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "quantity" in (decision.reason or "")

    def test_114_price_context_not_option(self) -> None:
        decision = _decide(
            "114",
            prep=_t3_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="114",
                slots={"quantity": 114},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER or "option" not in (
            decision.reason or ""
        )

    def test_ordinal_not_captured_as_size(self) -> None:
        decision = _decide(
            "الثاني",
            prep=_t3_prep(),
            intent=Intent(
                name=INTENT_PICK_LIST_ITEM,
                confidence=0.95,
                raw_message="الثاني",
                slots={"list_index": 2},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER or "option" not in (
            decision.reason or ""
        )

    def test_option_outside_active_order_does_not_route(self) -> None:
        decision = _decide(
            "42 - L",
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
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966555906901",
            message="42 - L",
            intent=Intent(name="general", confidence=0.72, raw_message="42 - L"),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_nonexistent_option_value_not_forced(self) -> None:
        prep = _t3_prep()
        assert _apply_writer(prep, "XXL") == 0
        assert not prep.product_options
        decision = _decide("XXL", prep=prep)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_option_not_applied_twice(self) -> None:
        prep = _t3_prep()
        assert _apply_writer(prep, "42 - L") == 1
        assert _apply_writer(prep, "42 - L") == 0

    def test_simple_product_without_options_no_regression(self) -> None:
        prep = OrderPreparationState(
            product_id=DRESS_ID,
            quantity=1,
            product_options={},
            product_options_meta=[],
            line_items=[
                {
                    "product_id": DRESS_ID,
                    "product_name": "فستان سادة",
                    "quantity": 1,
                    "unit_price": 114.0,
                }
            ],
        )
        decision = _decide("42 - L", prep=prep)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


class TestOptionGateWriterChain:
    def test_decision_gate_and_writer_chain_for_acceptance_t3(self) -> None:
        prep = _t3_prep()
        decision = _decide("42 - L", prep=prep, orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert _apply_writer(prep, "42 - L") == 1
        exported = _order_prep_export_dict(prep)
        assert exported["product_id"] == DRESS_ID
        assert exported["price"] == 114.0
        assert exported["quantity"] == 1
        assert prep.product_options["المقاس"]["value_id"] == 2002


class TestSlotConsumeEvidence:
    """Evidence layer may still tag «42 - L» as quantity — order state is authoritative."""

    def test_slot_consume_may_still_label_quantity_for_size_message(self) -> None:
        intent = Intent(
            name="general",
            confidence=0.72,
            raw_message="42 - L",
            slots=extract_ordering_slots("42 - L") or {},
        )
        types = detect_input_types(message="42 - L", intent=intent)
        # Known evidence-layer limitation: _QTY_HINT_RE matches embedded digits.
        assert "quantity" in types or types == ["quantity"]
