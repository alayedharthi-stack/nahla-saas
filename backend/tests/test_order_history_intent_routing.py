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

import pytest

pytestmark = pytest.mark.governance_contract

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.intent import rules
from modules.ai.brain.types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_GREETING,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_ORDER_REFERENCE_LIST,
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

    def test_order_reference_list_unambiguous_phrasings(self):
        messages = [
            "أرسل أرقام الطلبات",
            "أرقام طلباتي السابقة",
            "أرقام طلباتي",
        ]
        for msg in messages:
            result = rules.match(msg)
            assert result is not None, msg
            assert result.name == INTENT_ORDER_REFERENCE_LIST, (
                f"{msg!r} -> {result.name} (expected {INTENT_ORDER_REFERENCE_LIST})"
            )

    def test_order_reference_list_does_not_match_bare_pronoun(self):
        # Bare «أرقامها» follow-ups are ambiguous (order refs vs contact number).
        # Production routes them via staff_contact_non_product in the decision
        # engine when there is no prior orders context. A rule-layer match here
        # would steal legitimate contact-number questions for every tenant.
        for msg in ("تعرف أرقامها؟", "وش أرقامها؟"):
            result = rules.match(msg)
            assert result is None or result.name != INTENT_ORDER_REFERENCE_LIST, (
                f"{msg!r} must not be order_reference_list at rule layer (got {result.name})"
            )

    def test_contact_number_questions_not_regressed(self):
        # Pinned at rules.match layer (2026-07-27 baseline on fix/order-reference-listing-709).
        _contact_pinned = {
            "وش رقم المسؤول؟": None,
            "أرسل رقم الجوال": None,
            "تعرف رقمه؟": None,
            "رقم خدمة العملاء": INTENT_ASK_OWNER_CONTACT,
        }
        for msg, expected in _contact_pinned.items():
            result = rules.match(msg)
            if expected is None:
                assert result is None, (
                    f"{msg!r} -> {result.name if result else None} (pinned baseline None)"
                )
            else:
                assert result is not None, msg
                assert result.name == expected, (
                    f"{msg!r} -> {result.name} (pinned baseline {expected})"
                )

    def test_latest_order_summary_family_unchanged(self):
        # «آخر طلباتي» deliberately left unclaimed — see deliverable decision.
        _latest_pinned = {
            "وش آخر طلباتي": INTENT_LATEST_ORDER_SUMMARY,
            "آخر طلب لي": INTENT_LATEST_ORDER_SUMMARY,
        }
        for msg, expected_intent in _latest_pinned.items():
            result = rules.match(msg)
            assert result is not None, msg
            assert result.name == expected_intent, (
                f"{msg!r} -> {result.name} (pinned baseline {expected_intent})"
            )
        bare_latest = rules.match("آخر طلباتي")
        assert bare_latest is None or bare_latest.name != INTENT_ORDER_REFERENCE_LIST, (
            "آخر طلباتي must not be stolen for order_reference_list"
        )
