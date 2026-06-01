"""
tests/test_semantic_turn_interpreter.py
───────────────────────────────────────
Phase 1 — contextual repair for short ambiguous turns.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.outbound_leakage_firewall import firewall_outbound_text
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match
from modules.ai.brain.interpret.semantic_routing import apply_semantic_intent_override
from modules.ai.brain.interpret.semantic_turn_interpreter import (
    ANCHOR_LAST_ASSISTANT_SIZE_QUESTION,
    INTENT_SHOW_ALL_VARIANTS_OR_PRICES,
    interpret_semantic_turn,
    should_run_semantic_interpreter,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_GENERAL,
    INTENT_PICK_LIST_ITEM,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _history(*turns: tuple[str, str]) -> list:
    out = []
    for direction, body in turns:
        out.append({"direction": direction, "body": body})
    return out


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    history: list | None = None,
    semantic=None,
) -> BrainContext:
    st = state or MerchantConversationState()
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        raw_message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.55, raw_message=message),
        state=st,
        facts=CommerceFacts(has_products=True, orderable=True),
        history=history or [],
        semantic_interpretation=semantic,
    )


def _size_context_state(**kwargs) -> MerchantConversationState:
    return MerchantConversationState(
        last_question_asked="أي حجم يناسبك؟",
        last_intent="ask_price",
        current_product_focus={
            "id": "p1",
            "title": "عسل سدر",
            "price": "120",
            "external_id": "SKU1",
        },
        **kwargs,
    )


class TestSemanticInterpreterRepair:
    def test_typo_all_sizes_with_size_context(self):
        state = _size_context_state()
        history = _history(
            ("in", "كم سعره؟"),
            ("out", "أي حجم يناسبك؟"),
        )
        interp = interpret_semantic_turn(
            raw_text="كل الحجام",
            state=state,
            history=history,
        )
        assert interp is not None
        assert interp.interpreted_intent == INTENT_SHOW_ALL_VARIANTS_OR_PRICES
        assert interp.is_typo_repair is True
        assert "الأحجام" in interp.canonical_text
        assert interp.context_anchor == ANCHOR_LAST_ASSISTANT_SIZE_QUESTION

    def test_kolaha_with_size_context(self):
        state = _size_context_state()
        history = _history(("out", "أي حجم تبغى؟"))
        interp = interpret_semantic_turn(
            raw_text="كلها",
            state=state,
            history=history,
        )
        assert interp is not None
        assert interp.interpreted_intent == INTENT_SHOW_ALL_VARIANTS_OR_PRICES

    def test_ordinal_second_with_options_context(self):
        state = MerchantConversationState(
            last_search_candidates=[
                {"title": "خيار أ", "external_id": "A"},
                {"title": "خيار ب", "external_id": "B"},
            ],
        )
        history = _history(("out", "اختر:\n1. خيار أ\n2. خيار ب"))
        interp = interpret_semantic_turn(
            raw_text="الثاني",
            state=state,
            history=history,
        )
        assert interp is not None
        assert interp.slots.get("list_index") == 2

    def test_price_large_with_product_focus(self):
        state = _size_context_state()
        interp = interpret_semantic_turn(
            raw_text="كم الكبير",
            state=state,
            history=[],
        )
        assert interp is not None
        assert interp.slots.get("size_hint") == "large"

    def test_fulfillment_send_here_with_active_order(self):
        op = OrderPreparationState(
            product_id="SKU1",
            customer_first_name="أحمد",
            missing_fields=["google_maps_url"],
            order_status="awaiting_address",
        )
        state = MerchantConversationState(
            order_prep=op,
            stage="ordering",
            current_product_focus={"title": "عسل", "external_id": "SKU1"},
        )
        msg = "ارسله هنا https://maps.google.com/?q=24.7,46.6"
        interp = interpret_semantic_turn(raw_text=msg, state=state, history=[])
        assert interp is not None
        assert interp.interpreted_intent == "fulfillment_location_update"

    def test_no_context_clarify_not_social(self):
        interp = interpret_semantic_turn(
            raw_text="كل الحجام",
            state=MerchantConversationState(),
            history=[],
        )
        assert interp is not None
        assert interp.interpreted_intent == "clarify_variants_natural"

    def test_social_ameen_not_interpreted(self):
        assert should_run_semantic_interpreter(
            "آمين",
            MerchantConversationState(),
            _history(("out", "الله يبارك فيك")),
        ) is False

    def test_topic_shift_skips_interpreter(self):
        assert should_run_semantic_interpreter(
            "خلاص أبي منتج ثاني",
            _size_context_state(),
            [],
        ) is False


class TestSemanticDecisionRouting:
    def test_all_sizes_routes_to_llm_not_social(self):
        state = _size_context_state()
        history = _history(("out", "أي حجم يناسبك؟"))
        interp = interpret_semantic_turn(
            raw_text="كل الحجام",
            state=state,
            history=history,
        )
        ctx = _ctx("كل الحجام", state=state, history=history, semantic=interp)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "show_all_variants_prices"
        assert decision.action != ACTION_SOCIAL_REPLY

    def test_solution_seeking_not_blocked(self):
        from modules.ai.brain.commerce.solution_seeking import classify_solution_seeking_commerce

        msg = "شيء للدايت"
        assert classify_solution_seeking_commerce(msg) is not None
        assert should_run_semantic_interpreter(msg, MerchantConversationState(), []) is False

    def test_outbound_firewall_strips_progressive_selling(self):
        dirty = "حسب قواعد البيع التدريجي Progressive Selling هذا السعر"
        clean = firewall_outbound_text(dirty)
        assert "Progressive Selling" not in clean
        assert "قواعد البيع" not in clean

    def test_ordinal_routes_pick_list_item_via_override(self):
        state = MerchantConversationState(
            last_search_candidates=[
                {"title": "A", "external_id": "1", "can_checkout": True},
                {"title": "B", "external_id": "2", "can_checkout": True},
            ],
        )
        history = _history(("out", "1. A\n2. B"))
        interp = interpret_semantic_turn(
            raw_text="الثاني",
            state=state,
            history=history,
        )
        intent = apply_semantic_intent_override(
            Intent(name=INTENT_GENERAL, confidence=0.5, raw_message="الثاني"),
            interp,
        )
        assert intent.name == INTENT_PICK_LIST_ITEM
        assert intent.slots.get("list_index") == 2

    def test_fulfillment_routes_order_context_update(self):
        op = OrderPreparationState(
            product_id="SKU1",
            customer_first_name="سارة",
            missing_fields=["google_maps_url"],
            order_status="awaiting_address",
        )
        state = MerchantConversationState(
            order_prep=op,
            stage="ordering",
            current_product_focus={
                "title": "عسل",
                "external_id": "SKU1",
                "id": "p1",
            },
        )
        msg = "وصلها هنا https://maps.google.com/?q=24.7,46.6"
        interp = interpret_semantic_turn(raw_text=msg, state=state, history=[])
        ctx = _ctx(msg, state=state, semantic=interp)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE

    def test_no_context_clarify_decision(self):
        interp = interpret_semantic_turn(
            raw_text="كل الحجام",
            state=MerchantConversationState(),
            history=[],
        )
        ctx = _ctx("كل الحجام", semantic=interp)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_CLARIFY

    def test_large_price_llm_with_focus(self):
        state = _size_context_state()
        interp = interpret_semantic_turn(
            raw_text="كم الكبير",
            state=state,
            history=[],
        )
        ctx = _ctx("كم الكبير", state=state, semantic=interp)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "price"
        assert decision.args.get("size_hint") == "large"
