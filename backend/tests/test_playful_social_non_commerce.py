"""Playful / social non-commerce routing — platform-wide regression."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent.persona_interaction_classifier import (  # noqa: E402
    classify_persona_interaction,
)
from modules.ai.brain.persona.catalog_product_answer import classify_catalog_question_kind  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    INTENT_PERSONA_INTERACTION,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

_PERSONA_SOCIAL_TOPICS = frozenset({"persona_identity", "persona_social"})
_PERSONA_SOCIAL_INTENTS = frozenset({INTENT_WHO_ARE_YOU, INTENT_PERSONA_INTERACTION})

_CATALOG_MARKERS = ("حسب الكتالوج", "أي نوع يهمك", "اختر رقم", "اختر من")
_CHECKOUT_PRESSURE = ("إنشاء طلب", "اسمك", "عنوانك", "طريقة الدفع", "رقم الجوال")


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=3,
        in_stock_count=3,
        orderable=True,
        store_name="متجر تجريبي عام",
        assistant_name="نحلة",
    )


def _ctx(msg: str, intent: Intent | None = None) -> BrainContext:
    matched = intent or rules.match(msg)
    if matched is None:
        matched = Intent(
            name=INTENT_GENERAL,
            confidence=0.5,
            slots={},
            raw_message=msg,
        )
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=msg,
        intent=matched,
        state=MerchantConversationState(),
        facts=_facts(),
    )


def _assert_no_catalog_hijack(reply: str) -> None:
    for marker in _CATALOG_MARKERS:
        assert marker not in reply


def _assert_no_checkout_pressure(reply: str) -> None:
    for marker in _CHECKOUT_PRESSURE:
        assert marker not in reply


def _assert_persona_social_ownership(
    msg: str,
    *,
    intent: Intent | None,
    decision,
) -> None:
    assert intent is not None
    assert intent.name != INTENT_ASK_PRODUCT
    assert intent.name in _PERSONA_SOCIAL_INTENTS
    assert decision.action == ACTION_LLM_REPLY
    assert decision.action not in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}
    assert decision.args.get("topic") in _PERSONA_SOCIAL_TOPICS
    assert decision.args.get("block_commerce_escalation") is True

    social = resolve_current_turn_social_non_commerce(msg, intent=intent)
    assert social.matched is True


class TestPlayfulIdentityNonCommerce:
    def test_playful_assistant_identity_stays_out_of_catalog(self) -> None:
        msg = "هل أنت نحلة بلدية أم مستوردة؟"
        intent = rules.match(msg)
        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))

        _assert_persona_social_ownership(msg, intent=intent, decision=decision)

        playful_reply = "هههه 😄 أنا مساعد المتجر، موجودة أخدمك بالطلبات والمنتجات."
        guarded = apply_product_claim_grounding_guard(
            reply=playful_reply,
            tenant_id=1,
            inbound_metadata={"inbound_text": msg},
        )
        assert guarded.action == "allowed_social_noncommerce"
        assert guarded.reply == playful_reply
        _assert_no_catalog_hijack(guarded.reply)
        _assert_no_checkout_pressure(guarded.reply)

        hijacked_reply = "السدر أخف من الطلح حسب الكتالوج. أي نوع يهمك؟"
        guarded_hijack = apply_product_claim_grounding_guard(
            reply=hijacked_reply,
            tenant_id=1,
            inbound_metadata={"inbound_text": msg},
        )
        assert guarded_hijack.action == "allowed_social_noncommerce"
        assert guarded_hijack.reply == hijacked_reply
        assert guarded_hijack.replaced is False


class TestPlayfulNickname:
    def test_yaa_nahla_yaa_asal_routes_persona_and_bypasses_claim_guard(self) -> None:
        msg = "يا نحلة يا عسل 😄"
        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_PERSONA_INTERACTION
        assert intent.slots.get("block_commerce_escalation") is True

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is True

        playful_reply = "هلا فيك! 😄 كيف أقدر أساعدك؟"
        guarded = apply_product_claim_grounding_guard(
            reply=playful_reply,
            tenant_id=1,
            inbound_metadata={"inbound_text": msg},
        )
        assert guarded.action == "allowed_social_noncommerce"
        assert guarded.reply == playful_reply
        _assert_no_catalog_hijack(guarded.reply)


class TestColloquialSawalif:
    def test_wesh_indak_sawalif_not_ask_product(self) -> None:
        msg = "وش عندك من سوالف؟"
        intent = rules.match(msg)
        assert intent is None or intent.name != INTENT_ASK_PRODUCT

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is True
        assert social.reason == "colloquial_social_inventory"

        catalog_reply = "عندنا عسل سدر وطلح حسب الكتالوج. أي نوع يهمك؟"
        cpgg = apply_catalog_product_grounding_guard(
            reply=catalog_reply,
            inbound_text=msg,
            inbound_metadata={"inbound_text": msg},
            tenant_id=1,
        )
        assert cpgg.action == "allowed_social_noncommerce"
        assert cpgg.reply == catalog_reply


class TestTeasePhrase:
    def test_tadhak_aleina_persona_and_no_catalog(self) -> None:
        msg = "تضحك علينا؟"
        persona = classify_persona_interaction(msg)
        assert persona is not None
        assert persona.persona_kind == "tease"

        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_PERSONA_INTERACTION

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is True

        llm_reply = "لا والله، بس أحب أخفف الجو 😊"
        guarded = apply_product_claim_grounding_guard(
            reply=llm_reply,
            tenant_id=1,
            inbound_metadata={"inbound_text": msg},
        )
        assert guarded.action == "allowed_social_noncommerce"
        _assert_no_catalog_hijack(guarded.reply)


class TestHumorSocial:
    @pytest.mark.parametrize(
        "msg",
        [
            "هههه",
            "أمزح معك",
        ],
    )
    def test_humor_turns_block_commerce_escalation(self, msg: str) -> None:
        persona = classify_persona_interaction(msg)
        assert persona is not None

        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_PERSONA_INTERACTION
        assert intent.slots.get("block_commerce_escalation") is True

        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("block_commerce_escalation") is True

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is True


class TestCommerceRegression:
    def test_honey_types_still_commerce(self) -> None:
        msg = "وش أنواع العسل؟"
        intent = rules.match(msg)

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is False

        assert classify_catalog_question_kind(msg) == "browse"

        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
        assert decision.action in {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}


class TestClass2PriceRegression:
    def test_talh_price_still_catalog_product_answer_path(self) -> None:
        msg = "كم سعر الطلح؟"
        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_ASK_PRICE

        assert classify_catalog_question_kind(msg) == "price"

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is False

        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
        assert decision.action != ACTION_TRACK_ORDER


class TestClass4OrderRegression:
    def test_wain_talabi_still_track_order(self) -> None:
        msg = "وين طلبي؟"
        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_TRACK_ORDER

        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is False

        decision = DefaultDecisionEngine().decide(_ctx(msg, intent))
        assert decision.action == ACTION_TRACK_ORDER
