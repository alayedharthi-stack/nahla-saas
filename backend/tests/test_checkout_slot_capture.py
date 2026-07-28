"""Regression — active-order checkout slot answers must reach DraftOrderHandler.

Live acceptance run 2026-07-29 (tenant 1): «الرياض» extracted ``city`` but fell
through to ``ACTION_LLM_REPLY`` because ``facts.orderable=False`` and
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
    _merge_message_details,
    _missing_checkout_fields,
    _order_prep_export_dict,
)
from modules.ai.brain.intent.ordering_extractor import (  # noqa: E402
    extract_ordering_quantity,
    extract_ordering_slots,
)
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
OPTION_META = [
    {
        "id": 1,
        "name": "المقاس",
        "required": True,
        "values": [{"id": 2002, "name": "42 - L", "price": 114}],
    }
]


def _t4_prep(*, city: str = "", short_code: str = "") -> OrderPreparationState:
    """Product + size captured; city/address still pending — mirrors acceptance t3→t4."""
    return OrderPreparationState(
        product_id=DRESS_ID,
        quantity=1,
        city=city,
        short_address_code=short_code,
        product_options=dict(SIZE_OPTION),
        product_options_meta=list(OPTION_META),
        product_has_required_options=True,
        product_options_loaded=True,
        missing_fields=[
            "customer_first_name",
            "customer_last_name",
            "city",
            "address_location",
        ],
        order_creation_status="creating",
        line_items=[
            {
                "product_id": DRESS_ID,
                "product_name": "فستان سادة",
                "quantity": 1,
                "unit_price": 114.0,
                "variant": "42 - L",
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
) -> BrainContext:
    state = MerchantConversationState(
        stage=stage,
        greeted=True,
        order_prep=prep,
        current_product_focus=focus if focus is not None else dict(DRESS),
        last_search_candidates=candidates,
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


def _apply_writer(prep: OrderPreparationState, message: str, slots: dict | None = None) -> None:
    _merge_message_details(prep, slots or extract_ordering_slots(message) or {}, message)


class TestCheckoutSlotDecisionGate:
    def test_riyadh_routes_to_draft_order_not_llm(self) -> None:
        decision = _decide("الرياض", prep=_t4_prep(), orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_LLM_REPLY
        assert "checkout" in (decision.reason or "").lower()

    def test_writer_persists_city(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        assert prep.city == "الرياض"

    def test_city_removed_from_missing_fields(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        missing = _missing_checkout_fields(prep, is_sa=True)
        assert "city" not in missing
        assert "address_location" in missing

    def test_next_field_is_address_after_city(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        missing = _missing_checkout_fields(prep, is_sa=True)
        assert missing[0] in {"customer_first_name", "customer_last_name", "address_location"}

    def test_product_id_4_preserved(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        assert prep.product_id == DRESS_ID

    def test_size_and_value_id_preserved(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        assert prep.product_options["المقاس"]["value_name"] == "42 - L"
        assert prep.product_options["المقاس"]["value_id"] == 2002

    def test_price_and_quantity_preserved(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        exported = _order_prep_export_dict(prep)
        assert exported["price"] == 114.0
        assert exported["quantity"] == 1
        assert prep.line_items[0]["unit_price"] == 114.0

    def test_no_search_or_reselection(self) -> None:
        decision = _decide("الرياض", prep=_t4_prep(), candidates=ALT_CANDIDATES)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_product_289_not_selected(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        assert str(prep.product_id) != "289"

    def test_active_candidates_do_not_block_city_gate(self) -> None:
        decision = _decide("الرياض", prep=_t4_prep(), candidates=ALT_CANDIDATES)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_pre_commerce_shortcut_orderable_false_still_routes(self) -> None:
        decision = _decide("الرياض", prep=_t4_prep(), orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_city_outside_active_order_does_not_route(self) -> None:
        decision = _decide(
            "الرياض",
            stage=STAGE_DISCOVERY,
            prep=None,
            candidates=[],
            focus=None,
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_unconfirmed_generic_text_not_forced(self) -> None:
        decision = _decide("مرحبا", prep=_t4_prep())
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_quantity_not_captured_as_city(self) -> None:
        decision = _decide("كميتين", prep=_t4_prep())
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "quantity" in (decision.reason or "")

    def test_price_not_captured_as_city(self) -> None:
        decision = _decide(
            "114",
            prep=_t4_prep(),
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="114",
                slots={"quantity": 114},
            ),
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER or "checkout" not in (
            decision.reason or ""
        )

    def test_option_value_not_captured_as_city(self) -> None:
        prep = OrderPreparationState(
            product_id=DRESS_ID,
            quantity=1,
            product_options={},
            product_options_meta=list(OPTION_META),
            product_has_required_options=True,
            product_options_loaded=True,
            missing_fields=["customer_first_name", "city", "address_location"],
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
        assert "option" in (decision.reason or "").lower()

    def test_city_not_applied_twice(self) -> None:
        prep = _t4_prep()
        _apply_writer(prep, "الرياض")
        decision = _decide("الرياض", prep=prep)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_name_path_not_regressed(self) -> None:
        prep = _t4_prep()
        prep.city = "الرياض"
        decision = _decide(
            "أحمد",
            prep=prep,
            intent=Intent(
                name="general",
                confidence=0.72,
                raw_message="أحمد",
                slots={"customer_first_name": "أحمد"},
            ),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "checkout" in (decision.reason or "").lower()

    def test_ordinal_not_captured_as_city(self) -> None:
        decision = _decide(
            "الثاني",
            prep=_t4_prep(),
            intent=Intent(
                name=INTENT_PICK_LIST_ITEM,
                confidence=0.95,
                raw_message="الثاني",
                slots={"list_index": 2},
            ),
        )
        assert "checkout" not in (decision.reason or "")


class TestShortAddressPath:
    def test_rrrd1234_uses_order_context_update_not_checkout_gate(self) -> None:
        """Short codes are owned by order_context_gate earlier in decide()."""
        prep = _t4_prep(city="الرياض")
        prep.missing_fields = ["customer_first_name", "customer_last_name", "address_location"]
        decision = _decide("RRRD1234", prep=prep)
        assert decision.action == "order_context_update"
        assert "checkout" not in (decision.reason or "").lower()

    def test_short_address_writer_persists_code(self) -> None:
        prep = _t4_prep(city="الرياض")
        _apply_writer(prep, "RRRD1234")
        assert prep.short_address_code == "RRRD1234"


class TestCheckoutSlotWriterChain:
    def test_decision_gate_and_writer_chain_for_acceptance_t4(self) -> None:
        prep = _t4_prep()
        decision = _decide("الرياض", prep=prep, orderable=False)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        _apply_writer(prep, "الرياض")
        assert prep.city == "الرياض"
        assert prep.product_options["المقاس"]["value_id"] == 2002
        exported = _order_prep_export_dict(prep)
        assert exported["product_id"] == DRESS_ID
        assert exported["price"] == 114.0
        assert "city" not in _missing_checkout_fields(prep, is_sa=True)
