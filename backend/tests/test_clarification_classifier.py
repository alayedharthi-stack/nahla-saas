"""Tests for platform-wide missing-information clarification classifier."""
from __future__ import annotations

import os

import pytest

from modules.ai.brain.clarification.classifier import (
    classify_missing_information,
    would_action_for_spec,
)
from modules.ai.brain.clarification.flags import (
    is_clarification_shadow_enabled,
    is_contextual_clarify_enabled,
)
from modules.ai.brain.clarification.router import (
    try_contextual_clarification_fallback,
)
from modules.ai.brain.clarification.types import (
    AMBIGUITY_MISSING_CUSTOMER_PREFERENCE,
    AMBIGUITY_MISSING_PRODUCT_REF,
    AMBIGUITY_MISSING_VARIANT,
    RECOVERY_DETERMINISTIC,
    RECOVERY_GENERATIVE,
)
from modules.ai.brain.decision.actions import ACTION_CLARIFY, ACTION_LLM_REPLY
from modules.ai.brain.intent import rules
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_ASK_PRICE,
    MerchantConversationState,
)


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_ASK_PRICE,
    intent_conf: float = 0.9,
    focus: dict | None = None,
    candidates: list | None = None,
) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery")
    if focus:
        state.current_product_focus = focus
    if candidates:
        state.last_search_candidates = candidates
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=intent_conf,
            raw_message=message,
        ),
        state=state,
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            in_stock_count=5,
        ),
    )


class TestClassifierEvidence:
    def test_price_without_product_is_generative_not_deterministic(self):
        spec = classify_missing_information(
            _ctx("بكم السعر والقسط؟"),
            trigger="ask_price_no_product",
        )
        assert spec.ambiguity_class in {
            AMBIGUITY_MISSING_PRODUCT_REF,
            "missing_objective",
        }
        assert spec.recovery_mode == RECOVERY_GENERATIVE
        assert would_action_for_spec(spec) == "llm_reply"

    def test_search_candidates_yield_deterministic_list_pick(self):
        spec = classify_missing_information(
            _ctx(
                "2",
                intent_name="pick_list_item",
                candidates=[{"title": "SKU A"}, {"title": "SKU B"}],
            ),
            trigger="numeric_pick",
        )
        assert spec.ambiguity_class == AMBIGUITY_MISSING_CUSTOMER_PREFERENCE
        assert spec.recovery_mode == RECOVERY_DETERMINISTIC
        assert spec.structured_prompt is not None
        assert would_action_for_spec(spec) == "clarify"

    def test_focus_unit_price_is_deterministic_variant(self):
        spec = classify_missing_information(
            _ctx("بكم الكيلو؟", focus={"title": "Product X", "id": "1"}),
            trigger="unit_price",
        )
        assert spec.ambiguity_class == AMBIGUITY_MISSING_VARIANT
        assert spec.recovery_mode == RECOVERY_DETERMINISTIC


class TestRouterFlags:
    def test_shadow_on_by_default(self, monkeypatch):
        monkeypatch.delenv("CLARIFICATION_SHADOW_ENABLED", raising=False)
        assert is_clarification_shadow_enabled() is True

    def test_contextual_clarify_off_by_default(self, monkeypatch):
        monkeypatch.delenv("CONTEXTUAL_CLARIFY_ENABLED", raising=False)
        assert is_contextual_clarify_enabled() is False

    def test_fallback_returns_none_when_flag_off(self, monkeypatch):
        monkeypatch.setenv("CONTEXTUAL_CLARIFY_ENABLED", "false")
        monkeypatch.setenv("CLARIFICATION_SHADOW_ENABLED", "true")
        intent = rules.match("تقسيط بكم والسعر الإجمالي كم؟")
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966500000001",
            message="تقسيط بكم والسعر الإجمالي كم؟",
            intent=intent,
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True),
        )
        assert try_contextual_clarification_fallback(
            ctx, trigger="discovery_blocked",
        ) is None

    def test_fallback_returns_llm_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("CONTEXTUAL_CLARIFY_ENABLED", "true")
        intent = rules.match("تقسيط بكم والسعر الإجمالي كم؟")
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966500000001",
            message="تقسيط بكم والسعر الإجمالي كم؟",
            intent=intent,
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True),
        )
        dec = try_contextual_clarification_fallback(
            ctx, trigger="discovery_blocked",
        )
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "contextual_clarify"
        assert dec.args.get("ambiguity_class") == AMBIGUITY_MISSING_PRODUCT_REF
