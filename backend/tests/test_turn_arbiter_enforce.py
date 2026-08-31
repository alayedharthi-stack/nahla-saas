"""
tests/test_turn_arbiter_enforce.py
──────────────────────────────────
Phase 2A/2B — Turn Arbiter enforce + OwnerBrief compose routing tests.
"""
from __future__ import annotations

import os
import re
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
)
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_PRICE_INQUIRY,
    IntentPriorityVerdict,
)
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.turn.enforce import maybe_enforce_turn_decision  # noqa: E402
from modules.ai.brain.turn.mismatch import (  # noqa: E402
    MISMATCH_CHECKOUT_VS_DISCOVERY,
    MISMATCH_CHECKOUT_VS_SUPPORT,
    MISMATCH_STAFF_VS_PERSONA,
)
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
from modules.ai.brain.turn_owner_contract import build_turn_owner_contract  # noqa: E402
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

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


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
    facts: CommerceFacts | None = None,
    intent_slots: dict | None = None,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=intent_name, confidence=0.92, slots=intent_slots or {}),
        state=st,
        facts=facts if facts is not None else CommerceFacts(
            has_products=True,
            has_coupons=has_coupons,
        ),
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


def _assert_no_template_text_in_brief(brief: dict) -> None:
    """OwnerBrief must be goals/constraints only — no Arabic reply templates."""
    for key in ("reply_goal", "customer_goal", "tone_guidance"):
        value = str(brief.get(key) or "")
        assert not _ARABIC_RE.search(value), f"{key} must not contain Arabic template text"
    for obj in brief.get("forbidden_objectives") or ():
        assert not _ARABIC_RE.search(str(obj)), "forbidden_objectives must not contain Arabic"


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
    assert new_decision.args.get("turn_owner") in {"support", "post_purchase"}
    brief = new_decision.args.get("owner_brief") or {}
    forbidden = set(brief.get("forbidden_objectives") or ())
    assert {"checkout", "ordering", "product_upsell"}.issubset(forbidden)
    assert brief.get("compose_mode") == "persona"
    _assert_no_template_text_in_brief(brief)
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
    assert new_decision.action == ACTION_LLM_REPLY
    assert new_decision.action != ACTION_ORDER_CONTEXT_UPDATE
    brief = new_decision.args.get("owner_brief") or {}
    reply_goal = str(brief.get("reply_goal") or "")
    assert "answer_discount_or_product_question_first" in reply_goal
    assert brief.get("compose_mode") == "persona"
    _assert_no_template_text_in_brief(brief)


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
    assert new_decision.action == ACTION_LLM_REPLY
    assert new_decision.action != ACTION_SOCIAL_REPLY
    brief = new_decision.args.get("owner_brief") or {}
    assert brief.get("compose_mode") == "persona"
    assert new_decision.args.get("compose_mode") == "persona"
    _assert_no_template_text_in_brief(brief)


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


_CONCRETE_SHOE = {
    "id": 501,
    "external_id": "shoe-white-501",
    "title": "حذاء رياضي أبيض",
    "description": "حذاء شبكي خفيف مناسب للمشي اليومي.",
    "price": 199,
    "can_checkout": True,
    "in_stock": True,
}
_OTHER_PERFUME = {
    "id": 8801,
    "external_id": "perfume-rose-8801",
    "title": "عطر ورد 100ml",
    "description": "عطر ورد بتركيز 100 مل.",
    "price": 180,
    "can_checkout": True,
    "in_stock": True,
}


def _concrete_product_facts(*products: dict) -> CommerceFacts:
    rows = [dict(product) for product in products]
    return CommerceFacts(
        has_products=True,
        product_count=len(rows),
        in_stock_count=len(rows),
        orderable=True,
        snapshot_fresh=True,
        top_products=rows,
        discovery_products=rows,
    )


def _stale_checkout_with_concrete_focus() -> MerchantConversationState:
    state = _stale_checkout_state()
    set_product_focus(
        state,
        dict(_CONCRETE_SHOE),
        reason="test_concrete_catalog_subject",
        turn=state.turn,
    )
    return state


class TestConcreteProductInformationEnforcement:
    def test_catalog_confirmed_product_info_uses_existing_fact_owner(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        state = _stale_checkout_with_concrete_focus()
        ctx = _ctx(
            "حدثني عن حذاء رياضي أبيض",
            intent_name="ask_product",
            state=state,
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_CONCRETE_SHOE),
            intent_slots={"product_query": _CONCRETE_SHOE["title"]},
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        assert new_decision.action == ACTION_LLM_REPLY
        assert new_decision.args.get("topic") == "product_knowledge_facts"
        assert new_decision.args.get("question_kind") == "attribute"
        assert new_decision.args.get("subject_product", {}).get("id") == 501
        assert "catalog_description" in (new_decision.args.get("allowed_facts") or {})
        assert new_decision.args.get("block_order_flow") is True
        assert "source" not in (new_decision.args or {})
        assert str((ctx.state.current_product_focus or {}).get("id") or "") == "501"
        assert ctx.state.order_prep.missing_fields == []
        contract = build_turn_owner_contract(new_decision, ctx)
        assert contract.owner == "product_knowledge"
        assert contract.block_catalog_push is True

    def test_broad_browse_still_uses_discovery_search(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "ما أنواع الأحذية عندكم؟",
            intent_name="ask_product",
            state=_stale_checkout_with_concrete_focus(),
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_CONCRETE_SHOE, _OTHER_PERFUME),
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("topic") == "discovery"
        assert new_decision.args.get("source") == "state_continuity_reresolve"
        assert str((ctx.state.current_product_focus or {}).get("id") or "") == "501"

    def test_availability_question_does_not_enter_product_knowledge_facts(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "هل الحذاء الرياضي الأبيض متوفر؟",
            intent_name="ask_product",
            state=_stale_checkout_with_concrete_focus(),
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_CONCRETE_SHOE),
            intent_slots={"product_query": _CONCRETE_SHOE["title"]},
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.args.get("topic") != "product_knowledge_facts"
        assert new_decision.action == ACTION_SEARCH_PRODUCTS

    def test_explicit_product_switch_does_not_reresolve_old_focus(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "حدثني عن عطر ورد 100ml",
            intent_name="ask_product",
            state=_stale_checkout_with_concrete_focus(),
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_CONCRETE_SHOE, _OTHER_PERFUME),
            intent_slots={"product_query": _OTHER_PERFUME["title"]},
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("topic") == "discovery"
        assert new_decision.args.get("query") == ctx.message
        assert "product_id" not in (new_decision.args or {})
        assert "external_id" not in (new_decision.args or {})

    def test_ambiguous_product_inquiry_does_not_claim_existing_focus(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "ممكن أعرف عن منتج؟",
            intent_name="ask_product",
            state=_stale_checkout_with_concrete_focus(),
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_CONCRETE_SHOE, _OTHER_PERFUME),
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("topic") == "discovery"
        assert new_decision.args.get("topic") != "product_knowledge_facts"

    def test_foreign_catalog_rows_cannot_confirm_product_information(self, monkeypatch):
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "حدثني عن حذاء رياضي أبيض",
            tenant_id=77,
            intent_name="ask_product",
            state=_stale_checkout_with_concrete_focus(),
            state_relevance=_state_rel(),
            facts=_concrete_product_facts(_OTHER_PERFUME),
            intent_slots={"product_query": _CONCRETE_SHOE["title"]},
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")

        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("topic") != "product_knowledge_facts"
