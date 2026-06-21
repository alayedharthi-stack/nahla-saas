"""
tests/test_turn_arbiter_enforce.py
──────────────────────────────────
Phase 2A — limited Turn Arbiter enforce tests (platform-wide, tenant via env).
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
    ACTION_SUGGEST_COUPON,
)
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_PRICE_INQUIRY,
    IntentPriorityVerdict,
)
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.turn.enforce import maybe_enforce_turn_decision  # noqa: E402
from modules.ai.brain.turn.mismatch import (  # noqa: E402
    MISMATCH_CHECKOUT_VS_DISCOVERY,
    MISMATCH_CHECKOUT_VS_SUPPORT,
    MISMATCH_STAFF_VS_PERSONA,
)
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_COMPLAINT_REFUND,
    INTENT_SOCIAL,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    msg: str,
    *,
    tenant_id: int = 33,
    intent_name: str = "general",
    state: MerchantConversationState | None = None,
    state_relevance: StateRelevanceVerdict | None = None,
    intent_priority: IntentPriorityVerdict | None = None,
    social_human_context=None,
    has_coupons: bool = False,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=intent_name, confidence=0.92, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True, has_coupons=has_coupons),
        history=[],
        state_relevance=state_relevance,
        intent_priority=intent_priority,
        social_human_context=social_human_context,
    )


def _stale_checkout_state() -> MerchantConversationState:
    st = MerchantConversationState(turn=5, stage="checkout")
    st.order_prep = OrderPreparationState(product_id="p1", missing_fields=["city"])
    st.last_question_asked = "ما المدينة التي سيصلها الطلب؟"
    st.last_question_answered = False
    return st


def _state_rel() -> StateRelevanceVerdict:
    return StateRelevanceVerdict(
        safe_to_resume_state=False,
        detected_topic_shift=True,
        active_workflows=("active_fulfillment",),
    )


class _PureSocialShc:
    is_pure_social_turn = True
    social_category = "gratitude"


def _enable_enforce_platform_wide(monkeypatch):
    monkeypatch.setenv("TURN_ARBITER_ENFORCE_ENABLED", "true")
    monkeypatch.delenv("TURN_ARBITER_ENFORCE_TENANTS", raising=False)
    monkeypatch.setenv(
        "TURN_ARBITER_ENFORCE_MISMATCH_TYPES",
        "checkout_vs_support,checkout_vs_discovery,staff_vs_persona",
    )


def _enable_enforce_allowlist_33(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    monkeypatch.setenv("TURN_ARBITER_ENFORCE_TENANTS", "33")


def test_enforce_disabled_by_default():
    ctx = _ctx("العسل خفيف", intent_name=INTENT_COMPLAINT_REFUND, state=_stale_checkout_state())
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)
    assert result.enforced is False
    assert new_decision.action == ACTION_ORDER_CONTEXT_UPDATE


def test_enforce_skips_non_allowlisted_tenant(monkeypatch):
    _enable_enforce_allowlist_33(monkeypatch)
    ctx = _ctx(
        "العسل خفيف",
        tenant_id=99,
        intent_name=INTENT_COMPLAINT_REFUND,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)
    assert result.enforced is False


def test_enforce_works_platform_wide_without_tenant_allowlist(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    ctx = _ctx(
        "العسل خفيف ومو مثل أول",
        tenant_id=99,
        intent_name=INTENT_COMPLAINT_REFUND,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

    assert result.enforced is True
    assert result.mismatch_type == MISMATCH_CHECKOUT_VS_SUPPORT
    assert new_decision.action == ACTION_LLM_REPLY


def test_enforce_checkout_vs_support_on_complaint(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    ctx = _ctx(
        "العسل خفيف ومو مثل أول",
        intent_name=INTENT_COMPLAINT_REFUND,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

    assert result.enforced is True
    assert result.mismatch_type == MISMATCH_CHECKOUT_VS_SUPPORT
    assert new_decision.action == ACTION_LLM_REPLY
    assert new_decision.args.get("topic") == "support_complaint_refund"
    assert ctx.state.last_question_asked == ""


def test_enforce_checkout_vs_discovery_on_discount(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    ctx = _ctx(
        "ما عندكم كود خصم",
        intent_name=INTENT_ASK_PRICE,
        state=_stale_checkout_state(),
        state_relevance=_state_rel(),
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
        has_coupons=True,
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="slot_fill")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

    assert result.enforced is True
    assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
    assert new_decision.action in {ACTION_SUGGEST_COUPON, ACTION_SEARCH_PRODUCTS, ACTION_LLM_REPLY}
    assert new_decision.action != ACTION_ORDER_CONTEXT_UPDATE


def test_enforce_staff_vs_persona_on_gratitude(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    ctx = _ctx(
        "وصل والله يبيض وجهك",
        intent_name=INTENT_SOCIAL,
        social_human_context=_PureSocialShc(),
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_HANDOFF, reason="keyword_staff")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

    assert result.enforced is True
    assert result.mismatch_type == MISMATCH_STAFF_VS_PERSONA
    assert new_decision.action == ACTION_SOCIAL_REPLY


def test_enforce_noop_when_owners_match(monkeypatch):
    _enable_enforce_platform_wide(monkeypatch)
    ctx = _ctx(
        "ما عندكم كود خصم",
        intent_name=INTENT_ASK_PRICE,
        intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
    )
    prepare_turn_arbitration(ctx)
    legacy = Decision(action=ACTION_SEARCH_PRODUCTS, reason="catalog")
    new_decision, result = maybe_enforce_turn_decision(ctx, legacy)
    assert result.enforced is False
    assert new_decision.action == ACTION_SEARCH_PRODUCTS
