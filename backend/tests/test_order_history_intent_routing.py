"""
tests/test_order_history_intent_routing.py
──────────────────────────────────────────
Regression: «طلباتي السابقة عندكم» / «طلباتي عندكم» must route to
order_history_count, not ask_product; purchase-intent «طلب» verbs must
stay pinned after narrowing the bare «طلب» token in INTENT_ASK_PRODUCT.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.intent import rules
from modules.ai.brain.types import (
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_GREETING,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_START_ORDER,
)

# Baseline intents recorded before the rules.py fix (2026-07-27).
_PURCHASE_PINNED = {
    "أبي أطلب": INTENT_START_ORDER,
    "أبغى أطلب المنتج": INTENT_START_ORDER,
    "كيف أطلب؟": INTENT_START_ORDER,
    "أريد طلب هذا المنتج": INTENT_ASK_PRODUCT,
    "هل أقدر أطلبه؟": INTENT_START_ORDER,
}

_SANITY_PINNED = {
    "مرحبا": INTENT_GREETING,
    "متى يوصل الطلب؟": INTENT_ASK_SHIPPING,
}

_PURCHASE_ORIENTED = frozenset({INTENT_START_ORDER, INTENT_ASK_PRODUCT})


class TestOrderHistoryIntentRouting:
    def test_order_history_count_phrasings(self):
        messages = [
            "طلباتي السابقة عندكم",
            "طلباتي عندكم",
            "عندي طلبات سابقة",
            "طلباتي السابقة",
        ]
        for msg in messages:
            result = rules.match(msg)
            assert result is not None, msg
            assert result.name == INTENT_ORDER_HISTORY_COUNT, (
                f"{msg!r} -> {result.name} (expected {INTENT_ORDER_HISTORY_COUNT})"
            )

    def test_order_history_does_not_swallow_shipping_or_complaint(self):
        # EM review 2026-07-27: an unanchored «طلباتي السابقة» substring match
        # reclassified shipping/ETA and delivery-complaint turns as
        # order_history_count. Clean-HEAD baseline for all three was
        # ask_product (via bare «طلب» inside «طلباتي»); after the approved
        # طلب(?!ات) narrowing, «متى توصل…» / «…ما وصلت» may fall through
        # to None — that is acceptable here. The pin below proves no behaviour
        # drift when a rule *does* fire; the hard guard is != order_history_count.
        _anti_overreach_pinned = {
            "متى توصل طلباتي السابقة؟": INTENT_ASK_PRODUCT,
            "وين طلباتي السابقة؟": INTENT_ASK_PRODUCT,
            "طلباتي السابقة ما وصلت": INTENT_ASK_PRODUCT,
        }
        for msg, expected_intent in _anti_overreach_pinned.items():
            result = rules.match(msg)
            assert result is None or result.name != INTENT_ORDER_HISTORY_COUNT, (
                f"{msg!r} must not be order_history_count (got {result.name})"
            )
            if result is not None:
                assert result.name == expected_intent, (
                    f"{msg!r} -> {result.name} (pinned baseline {expected_intent})"
                )

    def test_purchase_intent_not_regressed(self):
        for msg, expected_intent in _PURCHASE_PINNED.items():
            result = rules.match(msg)
            assert result is not None, msg
            assert result.name != INTENT_ORDER_HISTORY_COUNT, msg
            assert result.name in _PURCHASE_ORIENTED, (
                f"{msg!r} -> {result.name} (expected purchase-oriented intent)"
            )
            assert result.name == expected_intent, (
                f"{msg!r} -> {result.name} (pinned baseline {expected_intent})"
            )

    def test_unrelated_intents_unchanged(self):
        for msg, expected_intent in _SANITY_PINNED.items():
            result = rules.match(msg)
            assert result is not None, msg
            assert result.name == expected_intent, (
                f"{msg!r} -> {result.name} (pinned baseline {expected_intent})"
            )
