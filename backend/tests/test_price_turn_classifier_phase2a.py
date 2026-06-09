"""
tests/test_price_turn_classifier_phase2a.py
───────────────────────────────────────────
P0 Commerce Clarification — Phase 2a regression.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.price_turn_classifier import (  # noqa: E402
    PriceTurnKind,
    classify_price_turn,
    normalize_price_subject,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    _resolved_product_query,
    try_price_query_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

_FOCUS = {
    "id": 1,
    "title": "عسل السمر 1447",
    "price": 280,
    "external_id": "ext-1",
}


def _ctx(
    message: str,
    *,
    with_focus: bool = False,
    intent_name: str | None = None,
) -> BrainContext:
    intent = rules.match(message)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.5,
            raw_message=message,
        )
    state = MerchantConversationState(greeted=True, stage="discovery")
    if with_focus:
        state.current_product_focus = dict(_FOCUS)
    return BrainContext(
        tenant_id=42,
        customer_phone="966500000099",
        message=message,
        intent=intent,
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, product_count=10),
    )


class TestPriceTurnClassifier:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("سعره سمح", PriceTurnKind.PRICE_COMMENT),
            ("سعره زين", PriceTurnKind.PRICE_COMMENT),
            ("سعره مناسب", PriceTurnKind.PRICE_COMMENT),
            ("the price is fair", PriceTurnKind.PRICE_COMMENT),
        ],
    )
    def test_price_comment_with_focus(self, message: str, expected: PriceTurnKind):
        assert classify_price_turn(_ctx(message, with_focus=True)) == expected

    def test_pronoun_reference_with_focus(self):
        assert (
            classify_price_turn(_ctx("سعره؟", with_focus=True))
            == PriceTurnKind.PRONOUN_REFERENCE
        )

    def test_product_price_ask_cross_vertical(self):
        for msg in (
            "قميص رجالي بكم",
            "عسل السمر بكم الكيلو",
            "coffee beans how much per kg",
        ):
            assert classify_price_turn(_ctx(msg)) == PriceTurnKind.PRODUCT_PRICE_ASK

    def test_unit_only_reference(self):
        assert (
            classify_price_turn(_ctx("كم سعر الكيلو؟"))
            == PriceTurnKind.UNIT_PRICE_REFERENCE
        )


class TestNormalizePriceSubject:
    def test_strips_filler_verb_before_unit(self):
        ctx = _ctx("بكم يطلع الكيلو")
        assert normalize_price_subject(ctx) == ""

    def test_price_comment_yields_empty_for_focus_path(self):
        ctx = _ctx("سعره سمح", with_focus=True)
        assert normalize_price_subject(ctx) == ""
        assert _resolved_product_query(ctx) == ""

    def test_well_formed_product_preserved(self):
        ctx = _ctx("عسل الطلح بكم الكيلو")
        assert normalize_price_subject(ctx) == "عسل الطلح"


class TestFocusPrecedence:
    @pytest.mark.parametrize(
        "message",
        ["سعره سمح", "سعره زين", "سعره مناسب", "سعره؟"],
    )
    def test_price_comment_uses_focus_not_search(self, message: str):
        ctx = _ctx(message, with_focus=True)
        tp = try_price_query_decision(ctx)
        assert tp is not None
        assert tp.action in {ACTION_LLM_REPLY, ACTION_VARIANT_PRICING}
        assert tp.action != ACTION_SEARCH_PRODUCTS

        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_unit_only_with_focus_uses_variant_or_price(self):
        ctx = _ctx("كم سعر الكيلو؟", with_focus=True)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_VARIANT_PRICING, ACTION_LLM_REPLY}

    def test_distorted_prefix_no_longer_search_with_focus(self):
        ctx = _ctx("بكم يطلع الكيلو", with_focus=True)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS


class TestPhase2aCorpusRegression:
    """Corpus from P0 audit — must not route to search_products."""

    _NO_SEARCH_WITH_FOCUS = (
        "سعره سمح",
        "سعره زين",
        "بكم يطلع الكيلو",
    )

    @pytest.mark.parametrize("message", _NO_SEARCH_WITH_FOCUS)
    def test_focus_wins_over_distorted_extract(self, message: str):
        decision = DefaultDecisionEngine().decide(_ctx(message, with_focus=True))
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_bare_kilo_still_clarifies_without_focus(self):
        decision = DefaultDecisionEngine().decide(_ctx("كم سعر الكيلو؟"))
        assert decision.action == ACTION_CLARIFY

    def test_valid_product_price_still_searches(self):
        decision = DefaultDecisionEngine().decide(_ctx("عسل الطلح بكم الكيلو"))
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query") == "عسل الطلح"
