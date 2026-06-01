"""
tests/test_solution_seeking_commerce.py
───────────────────────────────────────
Global solution-seeking commerce intelligence — all verticals, not honey-only.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.solution_seeking import (
    classify_solution_seeking_commerce,
    intelligent_need_clarification,
)
from modules.ai.brain.decision.actions import ACTION_CLARIFY, ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match
from modules.ai.brain.product_discovery_gate import (
    clarify_instead_of_top_products,
    is_solution_seeking_commerce,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    Intent,
    MerchantConversationState,
)


def _ctx(message: str) -> BrainContext:
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=INTENT_NEED_BASED_PRODUCT_ADVICE,
            confidence=0.94,
            raw_message=message,
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
    )


class TestSolutionSeekingClassifier:
    def test_honey_diabetes(self):
        m = classify_solution_seeking_commerce("عندك عسل ما يرفع السكر؟")
        assert m is not None
        assert m.axis == "health_diet"

    def test_food_no_sugar(self):
        m = classify_solution_seeking_commerce("عندكم منتج بدون سكر؟")
        assert m is not None

    def test_perfume_long_lasting(self):
        m = classify_solution_seeking_commerce("عندكم عطر ثابت للرسمي؟")
        assert m is not None
        assert m.axis in {"durability_longevity", "formality_occasion", "general_attribute"}

    def test_clothing_size_kids(self):
        m = classify_solution_seeking_commerce("عندكم مقاس يناسب الأطفال؟")
        assert m is not None
        assert m.axis == "audience_age"

    def test_electronics_battery(self):
        m = classify_solution_seeking_commerce("أبي جوال بطاريته قوية")
        assert m is not None
        assert m.axis == "performance_spec"

    def test_laptop_editing(self):
        m = classify_solution_seeking_commerce("لابتوب للمونتاج")
        assert m is not None

    def test_bare_sku_not_solution_seeking(self):
        assert classify_solution_seeking_commerce("بكم كيلo") is None


class TestSolutionSeekingRouting:
    def test_routes_to_llm_not_sku_clarify(self):
        decision = DefaultDecisionEngine().decide(_ctx("عسل ما يرفع السكر"))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CLARIFY
        assert (decision.args or {}).get("topic") == "solution_seeking_commerce"

    def test_intent_match_global(self):
        intent = match("شي للدايت عندكم؟")
        assert intent is not None
        assert intent.name == INTENT_NEED_BASED_PRODUCT_ADVICE

    def test_clarify_instead_of_top_products_never_sku_name(self):
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="عسل مناسب للسكر",
            intent=Intent(name="general", confidence=0.5, raw_message=""),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True),
        )
        d = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
        assert "أي منتج تقصد" not in (d.args or {}).get("question", "")
        assert d.action in {ACTION_LLM_REPLY, ACTION_CLARIFY}

    def test_intelligent_clarification_mentions_need_not_sku(self):
        q = intelligent_need_clarification("health_diet")
        assert "مرضى السكر" in q or "سكر" in q
        assert "أي منتج تقصد" not in q


class TestIsSolutionSeekingHelper:
    def test_helper_true_for_perfume(self):
        ctx = _ctx("عطر للصيف")
        assert is_solution_seeking_commerce(ctx)
