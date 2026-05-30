"""
tests/test_non_commerce_media_gate.py
─────────────────────────────────────
Regression suite for the May 2026 non-commerce safety layer.

Production failure: Eid dua / greeting images (long OCR, zero buying
intent) escalated into product recommendations + catalog cards.

These tests lock in:
  * classify_non_commerce fires on Eid / dua / social forwards
  * INTENT_SOCIAL wins over commerce intents in rules.py
  * decision engine blocks search_products / top_products fallbacks
  * catalog orchestrator rejects blocked / weak-commerce turns
  * weak unknown intent on media does NOT escalate

Run:
    cd backend
    python -m pytest tests/test_non_commerce_media_gate.py -v
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from modules.ai.brain.intent.non_commerce_classifier import (
    NC_DUA,
    NC_EID_GREETING,
    NON_COMMERCE_IMAGE_TAG,
    classify_non_commerce,
    commerce_escalation_allowed,
    has_positive_commerce_intent,
    resolve_commerce_block,
)
from modules.ai.brain.intent import rules as intent_rules
from modules.ai.brain.types import (
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    INTENT_SOCIAL,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.decision.actions import (
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
    ACTION_LLM_REPLY,
)
from services.catalog_product_orchestrator import (
    REASON_NON_COMMERCE_BLOCKED,
    REASON_NO_POSITIVE_COMMERCE,
    REASON_WEAK_CONFIDENCE,
    evaluate_product_card_send,
)


# ── Fixtures: realistic OCR dumps from greeting images ─────────────────────

EID_OCR_LONG = (
    f"{NON_COMMERCE_IMAGE_TAG}\n"
    "[وصف الصورة المرسلة] تصميم تهنئة بمناسبة عيد الأضحى المبارك. "
    "النص يحتوي: كل عام وأنتم بخير. تقبل الله طاعتكم. "
    "أسماء: محمد، فاطمة، أحمد. دعاء: اللهم تقبل منا ومنكم."
)

DUA_OCR = (
    "[وصف الصورة المرسلة] صورة تحتوي دعاء: اللهم إني أسألك العفو والعافية. "
    "يا رب العالمين. آمين. بدون أي mention لمنتجات أو أسعار."
)

KULL_AM_WA_ANTUM = "كل عام وأنتم بخير 🌹"
TAQABAL_ALLAH = "تقبل الله طاعتكم وكل عام وأنت بخير"

SOCIAL_FORWARD_OCR = (
    "[وصف الصورة المرسلة] forwarded message. "
    "تهنئة بمناسبة ذي الحجة. اللهم تقبل. frequently forwarded."
)

COMMERCE_MESSAGE = "أبغى عسل سدر بكم؟"


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_GENERAL,
    intent_confidence: float = 0.55,
    block: bool = False,
) -> BrainContext:
    slots = {}
    if block:
        slots["block_commerce_escalation"] = True
        slots["social_category"] = NC_EID_GREETING
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=intent_confidence,
            slots=slots,
            raw_message=message,
        ),
        state=MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
        block_commerce_escalation=block,
        non_commerce_category=NC_EID_GREETING if block else "",
    )


class TestNonCommerceClassifier:
    def test_eid_greeting_long_ocr(self):
        m = classify_non_commerce(EID_OCR_LONG, media_type="image")
        assert m is not None
        assert m.block_commerce is True
        assert m.category in {NC_EID_GREETING, NC_DUA, "religious_media"}

    def test_dua_screenshot(self):
        m = classify_non_commerce(DUA_OCR, media_type="image")
        assert m is not None
        assert m.category in {NC_DUA, "religious_media"}

    def test_kull_am_wa_antum_bkheir(self):
        m = classify_non_commerce(KULL_AM_WA_ANTUM)
        assert m is not None
        assert m.category == NC_EID_GREETING

    def test_taqabal_allah_taatakum(self):
        m = classify_non_commerce(TAQABAL_ALLAH)
        assert m is not None
        assert m.category == NC_EID_GREETING

    def test_social_forward(self):
        m = classify_non_commerce(SOCIAL_FORWARD_OCR, media_type="image")
        assert m is not None

    def test_commerce_message_not_blocked(self):
        assert classify_non_commerce(COMMERCE_MESSAGE) is None

    def test_metadata_flag_short_circuit(self):
        m = resolve_commerce_block(
            "anything",
            inbound_metadata={
                "block_commerce_escalation": True,
                "non_commerce_category": NC_EID_GREETING,
            },
        )
        assert m is not None
        assert m.category == NC_EID_GREETING

    def test_weak_media_intent_guard(self):
        m = classify_non_commerce(
            DUA_OCR,
            media_type="image",
            intent_name=INTENT_GENERAL,
            intent_confidence=0.50,
        )
        assert m is not None


class TestIntentRules:
    @pytest.mark.parametrize("message", [
        KULL_AM_WA_ANTUM,
        TAQABAL_ALLAH,
        EID_OCR_LONG,
    ])
    def test_rules_prefer_social_over_commerce(self, message):
        intent = intent_rules.match(message)
        assert intent is not None
        assert intent.name == INTENT_SOCIAL
        assert intent.slots.get("block_commerce_escalation") or intent.slots.get("social_category")

    def test_commerce_still_routes(self):
        intent = intent_rules.match(COMMERCE_MESSAGE)
        assert intent is not None
        assert intent.name in {INTENT_ASK_PRODUCT, "start_order", "ask_price"}


class TestDecisionEngine:
    def test_eid_ocr_no_search_products(self):
        engine = DefaultDecisionEngine()
        ctx = _ctx(EID_OCR_LONG, intent_name=INTENT_GENERAL, block=True)
        ctx.block_commerce_escalation = True
        decision = engine.decide(ctx)
        assert decision.action in {ACTION_SOCIAL_REPLY, ACTION_LLM_REPLY}
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_top_products_pattern_blocked_on_eid(self):
        engine = DefaultDecisionEngine()
        msg = "ما عندكم " + EID_OCR_LONG  # would trigger top_products pattern if unguarded
        ctx = _ctx(msg, intent_name=INTENT_GENERAL)
        ctx.block_commerce_escalation = True
        ctx.non_commerce_category = NC_EID_GREETING
        decision = engine.decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_non_commerce_llm_fallback_has_block_flag(self):
        engine = DefaultDecisionEngine()
        ctx = _ctx(EID_OCR_LONG, intent_name=INTENT_GENERAL)
        ctx.block_commerce_escalation = True
        ctx.non_commerce_category = NC_EID_GREETING
        decision = engine.decide(ctx)
        if decision.action == ACTION_LLM_REPLY:
            assert decision.args.get("block_commerce_escalation") is True
        else:
            assert decision.action == ACTION_SOCIAL_REPLY


class TestCatalogOrchestrator:
    _ATTACHMENT = {
        "kind": "product_card",
        "id": 1,
        "title": "عسل",
        "external_id": "SKU1",
        "confidence": "weak",
        "file_url": "https://example.com/p.jpg",
    }

    class _Conn:
        status = "connected"
        sending_enabled = True
        phone_number_id = "1234567890"
        catalog_enabled = True
        meta_catalog_id = "CAT-1"
        provider = "meta"

    def test_block_commerce_rejects_catalog(self):
        d = evaluate_product_card_send(
            tenant_id=33,
            connection=self._Conn(),
            attachment=dict(self._ATTACHMENT),
            block_commerce_escalation=True,
        )
        assert d.reason == REASON_NON_COMMERCE_BLOCKED

    def test_no_positive_commerce_without_intent(self):
        att = dict(self._ATTACHMENT)
        att["confidence"] = "weak"
        d = evaluate_product_card_send(
            tenant_id=33,
            connection=self._Conn(),
            attachment=att,
            positive_commerce_intent=False,
        )
        assert d.reason in {REASON_NO_POSITIVE_COMMERCE, REASON_WEAK_CONFIDENCE}

    def test_positive_commerce_allows_strong_confidence_path(self):
        att = dict(self._ATTACHMENT)
        att["confidence"] = "strong"
        d = evaluate_product_card_send(
            tenant_id=33,
            connection=self._Conn(),
            attachment=att,
            positive_commerce_intent=False,
        )
        assert d.reason != REASON_NO_POSITIVE_COMMERCE


class TestCommerceEscalationAllowed:
    def test_eid_blocked(self):
        assert not commerce_escalation_allowed(
            EID_OCR_LONG,
            intent_name=INTENT_GENERAL,
            inbound_metadata={"block_commerce_escalation": True},
        )

    def test_explicit_product_allowed(self):
        assert commerce_escalation_allowed(
            COMMERCE_MESSAGE,
            intent_name=INTENT_ASK_PRODUCT,
            intent_confidence=0.88,
        )

    def test_has_positive_commerce_intent(self):
        assert has_positive_commerce_intent(INTENT_ASK_PRODUCT, 0.88)
        assert not has_positive_commerce_intent(INTENT_GENERAL, 0.50)
