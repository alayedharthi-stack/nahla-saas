"""Regression tests for Customer Intent Priority Layer (AI-ARCH-007)."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.intent_priority import (  # noqa: E402
    GOAL_LOCATION_REQUEST,
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SHIPPING_INQUIRY,
    GOAL_STAFF_CONTACT,
    compute_customer_intent_priority,
    enrich_intent_with_priority,
    intent_priority_compose_directive,
)
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    ELEMENT_BLESSING,
    ELEMENT_COURTESY,
    ELEMENT_GREETING,
    ELEMENT_IMAGE_ATTACHMENT,
    ELEMENT_PRICE_INQUIRY,
    ELEMENT_PRODUCT_AVAILABILITY,
    ELEMENT_QUANTITY_UNIT,
    ELEMENT_SHIPPING_INQUIRY,
    ELEMENT_STAFF_CONTACT,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_GREETING_ONLY,
    GOAL_SOCIAL_ONLY,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    Intent,
    MerchantConversationState,
)


def _analyze(
    msg: str,
    *,
    intent_name: str = "general",
    intent_confidence: float = 0.5,
    profile: dict | None = None,
    focus: dict | None = None,
) -> object:
    state = MerchantConversationState()
    if focus:
        state.current_product_focus = focus
    intent = Intent(name=intent_name, confidence=intent_confidence, slots={})
    return compute_customer_intent_priority(
        message=msg,
        intent=intent,
        state=state,
        profile=profile or {},
    )


def _element_types(verdict) -> set[str]:
    return {e.element_type for e in verdict.detected_elements}


# ── Case 1: courtesy + price/unit + image ─────────────────────────────────────
def test_case1_courtesy_plus_price_with_image():
    verdict = _analyze(
        "ما شاء الله تبارك الله كم الكيلو",
        intent_name=INTENT_ASK_PRICE,
        intent_confidence=0.9,
        profile={"inbound_metadata": {"normalized_type": "image"}},
    )
    assert verdict.primary_customer_goal == GOAL_PRICE_INQUIRY
    assert ELEMENT_COURTESY in _element_types(verdict)
    assert ELEMENT_QUANTITY_UNIT in _element_types(verdict)
    assert ELEMENT_IMAGE_ATTACHMENT in _element_types(verdict)
    assert ELEMENT_COURTESY in verdict.secondary_elements
    assert verdict.requires_clarification is True
    assert verdict.clarification_reason == "image_product_uncertain"
    directive = intent_priority_compose_directive(verdict)
    assert "ممنوع جعل عبارة المجاملة" in directive or "لا تكرري" in directive
    assert "صورة" in verdict.recommended_focus or "image" in verdict.recommended_focus.lower()


# ── Case 2: greeting + product availability ───────────────────────────────────
def test_case2_greeting_plus_product_availability():
    verdict = _analyze(
        "هلا عندكم سمر؟",
        intent_name=INTENT_ASK_PRODUCT,
        intent_confidence=0.85,
    )
    assert verdict.primary_customer_goal == GOAL_PRODUCT_AVAILABILITY
    assert ELEMENT_GREETING in _element_types(verdict)
    assert ELEMENT_PRODUCT_AVAILABILITY in _element_types(verdict)
    assert ELEMENT_GREETING in verdict.secondary_elements
    assert verdict.requires_clarification is False


# ── Case 3: blessing + location ─────────────────────────────────────────────
def test_case3_blessing_plus_location():
    verdict = _analyze(
        "الله يبارك لك وين موقعكم؟",
        intent_name=INTENT_ASK_LOCATION,
        intent_confidence=0.9,
    )
    assert verdict.primary_customer_goal == GOAL_LOCATION_REQUEST
    assert ELEMENT_BLESSING in _element_types(verdict)
    assert ELEMENT_BLESSING in verdict.secondary_elements
    assert verdict.requires_clarification is False
    directive = intent_priority_compose_directive(verdict)
    assert "location" in directive or "موقع" in directive


# ── Case 4: filler + shipping price ─────────────────────────────────────────
def test_case4_shipping_price_no_generic_clarification():
    verdict = _analyze(
        "طيب كم الشحن؟",
        intent_name=INTENT_ASK_SHIPPING,
        intent_confidence=0.9,
    )
    assert verdict.primary_customer_goal == GOAL_SHIPPING_INQUIRY
    assert ELEMENT_SHIPPING_INQUIRY in _element_types(verdict)
    assert verdict.requires_clarification is False


# ── Case 5: staff contact — no product clarification ────────────────────────
def test_case5_staff_contact_no_product_clarification():
    verdict = _analyze(
        "أرسل لي رقم أمين",
        intent_name=INTENT_ASK_OWNER_CONTACT,
        intent_confidence=0.9,
    )
    assert verdict.primary_customer_goal == GOAL_STAFF_CONTACT
    assert ELEMENT_STAFF_CONTACT in _element_types(verdict)
    assert verdict.requires_clarification is False
    assert verdict.clarification_reason == ""


# ── Case 6: price without product context ───────────────────────────────────
def test_case6_price_without_product_requires_goal_bound_clarification():
    verdict = _analyze(
        "كم سعره؟",
        intent_name=INTENT_ASK_PRICE,
        intent_confidence=0.9,
    )
    assert verdict.primary_customer_goal == GOAL_PRICE_INQUIRY
    assert verdict.requires_clarification is True
    assert verdict.clarification_reason == "missing_product_for_price"
    assert "المنتج المقصود" in verdict.recommended_focus
    assert "أي نوع أو صفة" in verdict.recommended_focus


def test_enrich_intent_stamps_priority_slots():
    intent = Intent(name=INTENT_ASK_PRICE, confidence=0.9, slots={})
    verdict = compute_customer_intent_priority(
        message="ما شاء الله كم الكيلو",
        intent=intent,
        state=MerchantConversationState(),
        profile={},
    )
    enriched = enrich_intent_with_priority(intent, verdict)
    assert enriched.slots["primary_customer_goal"] == GOAL_PRICE_INQUIRY
    assert enriched.slots.get("embedded_greeting") is True
    assert enriched.slots.get("requires_goal_bound_clarification") is True


def test_priority_ranking_commercial_beats_courtesy():
    verdict = _analyze("ما شاء الله تبارك الله كم الكيلو", intent_name=INTENT_ASK_PRICE)
    ranking = verdict.priority_ranking
    if ELEMENT_COURTESY in ranking and ELEMENT_PRICE_INQUIRY in ranking:
        assert ranking.index(ELEMENT_PRICE_INQUIRY) < ranking.index(ELEMENT_COURTESY)
    if ELEMENT_COURTESY in ranking and ELEMENT_QUANTITY_UNIT in ranking:
        assert ranking.index(ELEMENT_QUANTITY_UNIT) < ranking.index(ELEMENT_COURTESY)


# ── P1 fix: welcome «بكم» must not trigger price_inquiry ─────────────────────
def _analyze_with_rules(msg: str) -> object:
    intent = rules.match(msg) or Intent(name="general", confidence=0.4, slots={})
    return compute_customer_intent_priority(
        message=msg,
        intent=intent,
        state=MerchantConversationState(),
        profile={},
    )


@pytest.mark.parametrize(
    "message,acceptable_goals",
    [
        ("مرحبا بكم", {GOAL_GREETING_ONLY, GOAL_SOCIAL_ONLY, "greeting_only"}),
        ("أهلا بكم", {GOAL_GREETING_ONLY, GOAL_SOCIAL_ONLY, "greeting_only"}),
        ("حياكم الله", {GOAL_GREETING_ONLY, GOAL_SOCIAL_ONLY, "greeting_only"}),
    ],
)
def test_welcome_bkm_not_price_inquiry(message, acceptable_goals):
    verdict = _analyze_with_rules(message)
    assert verdict.primary_customer_goal != GOAL_PRICE_INQUIRY
    assert verdict.primary_customer_goal in acceptable_goals
    assert ELEMENT_PRICE_INQUIRY not in _element_types(verdict)


def test_marhaba_bikum_with_product_is_availability_not_price():
    verdict = _analyze_with_rules("مرحبا بكم عندكم طلح؟")
    assert verdict.primary_customer_goal == GOAL_PRODUCT_AVAILABILITY
    assert verdict.primary_customer_goal != GOAL_PRICE_INQUIRY
    assert ELEMENT_PRICE_INQUIRY not in _element_types(verdict)


@pytest.mark.parametrize(
    "message",
    [
        "بكم الكيلو؟",
        "بكم السمر؟",
        "هذا بكم؟",
        "بكم سعره؟",
    ],
)
def test_valid_bkm_remains_price_inquiry(message):
    verdict = _analyze(message, intent_name=INTENT_ASK_PRICE, intent_confidence=0.9)
    assert verdict.primary_customer_goal == GOAL_PRICE_INQUIRY
    assert ELEMENT_PRICE_INQUIRY in _element_types(verdict)
