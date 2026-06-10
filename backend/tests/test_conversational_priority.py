"""Tests for conversational priority gates (social, short continuation, payment consent)."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.conversational_priority import (  # noqa: E402
    CONTINUATION_DELIVERY,
    commerce_signal_strength,
    detect_payment_intent_strength,
    detect_short_transactional_continuation,
    has_payment_outbound_consent,
    infer_continuation_mode,
    is_receipt_inbound,
    is_single_offer_short_acceptance,
    try_priority_before_suppression,
    try_short_continuation_decision,
    try_social_non_commerce_decision,
)
from modules.ai.brain.product_discovery_gate import clarify_instead_of_top_products  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    msg: str,
    *,
    focus: dict | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3)
    if focus:
        st.current_product_focus = focus
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        intent=Intent(name="general", confidence=0.4, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
    )


def test_short_continuation_with_focus():
    v = detect_short_transactional_continuation(
        "أبيك علامه",
        state=MerchantConversationState(
            current_product_focus={"title": "كريم مرطب", "id": 42},
        ),
    )
    assert v.matched
    assert v.focus_title == "كريم مرطب"


def test_short_continuation_without_focus():
    v = detect_short_transactional_continuation("أبغاه", state=MerchantConversationState())
    assert not v.matched


def test_delivery_mode_preserves_fulfillment_on_tamam():
    state = MerchantConversationState(turn=4, stage="ordering")
    state.current_product_focus = {"title": "عطر", "id": 1}
    state.order_prep = OrderPreparationState(product_id="1", missing_fields=["city"])
    mode = infer_continuation_mode(state)
    assert mode == CONTINUATION_DELIVERY
    v = detect_short_transactional_continuation("تمام", state=state)
    assert v.matched
    assert v.continuation_mode == CONTINUATION_DELIVERY
    dec = try_short_continuation_decision(_ctx("تمام", state=state), route="test")
    assert dec is not None
    assert dec.args.get("continuation_mode") == CONTINUATION_DELIVERY
    assert dec.args.get("topic") == "ask_shipping"


def test_receipt_blocks_payment_consent():
    assert is_receipt_inbound({"pdf_kind": "payment_receipt"})
    assert not has_payment_outbound_consent(
        "رقم الحساب 123456",
        inbound_metadata={"pdf_kind": "payment_receipt"},
        tenant_id=1,
    )


def test_explicit_barcode_request_has_consent():
    assert has_payment_outbound_consent(
        "ارسل لي باركود التحويل",
        inbound_metadata={},
        tenant_id=1,
    )


def test_semantic_payment_consent_send_account():
    v = detect_payment_intent_strength("ارسل الحساب")
    assert v.strength >= 0.65
    assert v.source == "semantic"
    assert has_payment_outbound_consent("ارسل الحساب", tenant_id=1)


def test_social_bypass_when_commerce_signal_present():
    strength = commerce_signal_strength("الله يبارك فيك كم سعره؟")
    assert strength >= 0.32
    ctx = _ctx("الله يبارك فيك كم سعره؟")
    assert try_social_non_commerce_decision(ctx, route="test") is None


def test_clarify_short_circuits_generic_need_question():
    ctx = _ctx("تمام", focus={"title": "عطر فاخر", "id": 7})
    dec = try_short_continuation_decision(ctx, route="test")
    assert dec is not None
    assert dec.args.get("preserve_product_focus") is True


def test_clarify_social_before_solution_seeking():
    ctx = _ctx("مبروك يا غالي على التخرج")
    dec = clarify_instead_of_top_products(ctx, reason="test_social")
    assert dec.action == "llm_reply"
    assert dec.args.get("topic") == "social_persona_ack"


def test_priority_social_routes_celebration():
    ctx = _ctx("مبروك عليك")
    dec = try_priority_before_suppression(ctx, route="test")
    assert dec is not None
    assert dec.action == "llm_reply"
    assert dec.args.get("topic") == "social_persona_ack"


def test_single_offer_short_acceptance_never_generic_clarify():
    state = MerchantConversationState(turn=5)
    state.current_product_focus = {"title": "عطر فاخر", "id": 1}
    state.last_search_candidates = [{"id": 1, "title": "عطر فاخر"}]
    ctx = _ctx("أبغاه", state=state)
    assert is_single_offer_short_acceptance(ctx)
    dec = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
    assert dec.action == "llm_reply"
    question = str((dec.args or {}).get("question") or "")
    for marker in (
        "أي منتج تقصد",
        "تقصد حاجة أو مواصفة",
        "أي منتج أو خدمة",
    ):
        assert marker not in question
