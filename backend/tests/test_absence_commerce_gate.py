"""Absence-of-positive-commerce-signal gate — platform-wide slice tests."""
from __future__ import annotations

import inspect
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.conversational_priority import (  # noqa: E402
    CONTINUATION_DELIVERY,
    absence_of_positive_commerce_signal,
    positive_commerce_signal,
    try_absence_non_sales_decision,
    try_short_continuation_decision,
)
from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.pipeline import _compose_response_goal
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SuggestionSnapshot,
)


def _ctx(
    msg: str,
    *,
    intent: Intent | None = None,
    state: MerchantConversationState | None = None,
    tenant_id: int = 1,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3, greeted=True)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="+966500000000",
        message=msg,
        intent=intent or Intent(name=INTENT_GENERAL, confidence=0.5, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
    )


# ── 1. Commerce flows remain unchanged ─────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "ابغى شي",
        "ودي شي",
        "كم السعر",
        "أبغى أطلب",
    ],
)
def test_commerce_messages_have_positive_signal(msg: str) -> None:
    assert positive_commerce_signal(msg, intent_name=INTENT_GENERAL)


@pytest.mark.parametrize(
    "msg",
    [
        "ابغى شي",
        "ودي شي",
        "كم السعر",
        "أبغى أطلب",
    ],
)
def test_commerce_messages_do_not_hit_absence_gate(msg: str) -> None:
    ctx = _ctx(msg)
    assert try_absence_non_sales_decision(ctx, route="test") is None


def test_commerce_intent_skips_absence_gate() -> None:
    ctx = _ctx(
        "hello",
        intent=Intent(name="ask_product", confidence=0.9, slots={}),
    )
    assert try_absence_non_sales_decision(ctx, route="test") is None


# ── 2. Checkout / fulfillment continuations unchanged ──────────────────────


def test_checkout_tamam_uses_short_continuation_not_absence() -> None:
    state = MerchantConversationState(turn=4, stage="ordering", greeted=True)
    state.current_product_focus = {"title": "عطر", "id": 1}
    state.order_prep = OrderPreparationState(product_id="1", missing_fields=["city"])
    ctx = _ctx("تمام", state=state)
    assert positive_commerce_signal("تمام", state=state)
    assert try_absence_non_sales_decision(ctx, route="test") is None
    dec = try_short_continuation_decision(ctx, route="test")
    assert dec is not None
    assert dec.args.get("continuation_mode") == CONTINUATION_DELIVERY


def test_pending_offer_context_blocks_absence_gate() -> None:
    state = MerchantConversationState(turn=5, greeted=True)
    state.last_question_asked = "تبين أرسل الرابط؟"
    ctx = _ctx("تمام", state=state)
    assert positive_commerce_signal("تمام", state=state)
    assert try_absence_non_sales_decision(ctx, route="test") is None


def test_engine_preserves_fulfillment_on_tamam() -> None:
    state = MerchantConversationState(turn=4, stage="ordering", greeted=True)
    state.current_product_focus = {"title": "كريم", "id": 2}
    state.order_prep = OrderPreparationState(product_id="2", missing_fields=["city"])
    ctx = _ctx("تمام", state=state)
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") != "non_sales_ambiguous"


# ── 3. Absence gate uses generative compose profile ────────────────────────


def test_absence_gate_fires_for_ambiguous_general() -> None:
    ctx = _ctx("طيب")
    dec = try_absence_non_sales_decision(ctx, route="test")
    assert dec is not None
    assert dec.action == ACTION_LLM_REPLY
    assert dec.args.get("topic") == "non_sales_ambiguous"
    assert dec.args.get("block_commerce_escalation") is True


def test_absence_gate_requires_greeted_state() -> None:
    state = MerchantConversationState(turn=1, greeted=False)
    ctx = _ctx("طيب", state=state)
    assert not absence_of_positive_commerce_signal(
        "طيب",
        intent_name=INTENT_GENERAL,
        state=state,
    )
    assert try_absence_non_sales_decision(ctx, route="test") is None


def test_engine_routes_ambiguous_to_non_sales_topic() -> None:
    ctx = _ctx("يعني")
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "non_sales_ambiguous"


def test_response_goal_is_generative_not_sales_default() -> None:
    dec = Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "non_sales_ambiguous", "block_commerce_escalation": True},
        reason="test",
        confidence=0.88,
    )
    goal = _compose_response_goal(dec, SuggestionSnapshot())
    assert "Generate" in goal
    assert "non_sales_ambiguous" in goal
    assert "advance the conversation toward the next sales step" not in goal


def test_prompt_uses_persona_expression_profile() -> None:
    state = BrainReplyState(
        store_name="Test Store",
        tone="warm",
        stage="browsing",
        response_goal=_compose_response_goal(
            Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "non_sales_ambiguous",
                    "block_commerce_escalation": True,
                },
                reason="test",
                confidence=0.88,
            ),
            SuggestionSnapshot(),
        ),
        persona_expression_mode=True,
        persona_topic="non_sales_ambiguous",
        non_commerce_block_mode=True,
        intent_name=INTENT_GENERAL,
    )
    prompt = build_brain_reply_prompt(state)
    assert "SALESPERSON BEHAVIOR" not in prompt
    assert "معطّلة لهذه الجولة" in prompt
    assert "جولة شخصية/اجتماعية" in prompt


def test_pre_commerce_shortcut_on_absence() -> None:
    state = MerchantConversationState(turn=3, greeted=True)
    intent = Intent(name=INTENT_GENERAL, confidence=0.5, slots={})
    assert should_pre_commerce_shortcut(
        intent,
        None,
        message="طيب",
        state=state,
    )


# ── 4. No new deterministic Arabic reply templates in gate logic ───────────


_FORBIDDEN_CUSTOMER_TEMPLATES = (
    "وش تقصد",
    "ما فهمت عليك",
    "وضح أكثر",
    "إذا تحتاج أي مساعدة",
    "وضح الاستخدام أو الصفة المطلوبة",
)


def test_gate_module_has_no_canned_arabic_replies() -> None:
    from modules.ai.brain import commerce as commerce_pkg  # noqa: PLC0415
    from modules.ai.brain.commerce import conversational_priority  # noqa: PLC0415

    sources = inspect.getsource(conversational_priority)
    for phrase in _FORBIDDEN_CUSTOMER_TEMPLATES:
        assert phrase not in sources


def test_persona_goal_is_behavioral_not_canned_reply() -> None:
    from modules.ai.brain.persona_expression import compose_non_sales_ambiguous_goal

    goal = compose_non_sales_ambiguous_goal()
    assert goal.startswith("non_sales_ambiguous — Generate")
    for phrase in _FORBIDDEN_CUSTOMER_TEMPLATES:
        assert phrase not in goal


# ── 5. No tenant-specific branching in gate logic ──────────────────────────


def test_absence_gate_no_tenant_id_branching() -> None:
    from modules.ai.brain.commerce import conversational_priority  # noqa: PLC0415

    src = inspect.getsource(conversational_priority.absence_of_positive_commerce_signal)
    src += inspect.getsource(conversational_priority.positive_commerce_signal)
    src += inspect.getsource(conversational_priority.try_absence_non_sales_decision)
    lowered = src.lower()
    assert "tenant 33" not in lowered
    assert "al-ayed" not in lowered
    assert "al_ayed" not in lowered
    assert "tenant_id ==" not in lowered
    assert "tenant_id==" not in lowered


def test_absence_gate_same_for_different_tenants() -> None:
    ctx_a = _ctx("طيب", tenant_id=1)
    ctx_b = _ctx("طيب", tenant_id=999)
    dec_a = try_absence_non_sales_decision(ctx_a, route="test")
    dec_b = try_absence_non_sales_decision(ctx_b, route="test")
    assert dec_a is not None
    assert dec_b is not None
    assert dec_a.args.get("topic") == dec_b.args.get("topic")
