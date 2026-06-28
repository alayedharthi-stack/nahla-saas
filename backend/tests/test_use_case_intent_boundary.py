"""
tests/test_use_case_intent_boundary.py
──────────────────────────────────────
PR-D1 — use-case / benefit / recommendation intent must not become
product label, catalog search, or catalog-miss deterministic reply.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.catalog_search_evidence import (
    compose_catalog_miss_deterministic_reply,
)
from modules.ai.brain.commerce.product_label_hygiene import (
    is_non_product_label,
    sanitize_product_label,
)
from modules.ai.brain.commerce.solution_seeking import (
    classify_solution_seeking_commerce,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    Intent,
    MerchantConversationState,
)


def _ctx(message: str, *, intent: Intent | None = None) -> BrainContext:
    resolved = intent or match(message) or Intent(
        name="general", confidence=0.5, raw_message=message,
    )
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=resolved,
        state=MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
    )


def _decide(message: str) -> object:
    return DefaultDecisionEngine().decide(_ctx(message))


class TestUseCaseIntentBoundary:
    def test_t1_fertility_not_product_label(self):
        msg = "أريد عسل للإنجاب بعد الله سبحانه"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is not None
        intent = match(msg)
        assert intent is not None
        assert intent.name == INTENT_NEED_BASED_PRODUCT_ADVICE
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert (decision.args or {}).get("topic") == "solution_seeking_commerce"
        assert is_non_product_label(msg)
        assert sanitize_product_label(msg) == ""
        miss = compose_catalog_miss_deterministic_reply()
        assert msg not in miss

    def test_t2_immunity_solution_seeking(self):
        msg = "أبغى عسل للمناعة"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is not None
        assert ss.source == "purpose_phrasing"
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert is_non_product_label(msg)

    def test_t3_cough_not_catalog_miss(self):
        msg = "عسل للكحة"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is not None
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_t4_recommendation_wording(self):
        msg = "وش تنصحني للبرد؟"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is not None
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("topic") == "solution_seeking_commerce"

    def test_t5_product_availability_still_works(self):
        msg = "هل عسل السمر متوفر؟"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is None
        decision = _decide(msg)
        assert decision.action != ACTION_LLM_REPLY or (
            (decision.args or {}).get("topic") != "solution_seeking_commerce"
        )

    def test_t6_explicit_product_order_still_works(self):
        msg = "أبي عسل الطلح كيلو"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is None
        decision = _decide(msg)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_t7_best_seller_browse_unchanged(self):
        msg = "أكثر مبيعًا"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is None
        decision = _decide(msg)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_t8_medical_advisory_not_catalog_miss(self):
        msg = "أبغى عسل يعالج السكر"
        ss = classify_solution_seeking_commerce(msg)
        assert ss is not None
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("topic") == "solution_seeking_commerce"
        miss = compose_catalog_miss_deterministic_reply()
        assert "ما لقيت تطابق" not in miss or msg not in miss
