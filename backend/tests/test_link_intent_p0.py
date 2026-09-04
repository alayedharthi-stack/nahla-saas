"""P0 regression tests — website URL vs Google Maps link intent routing."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_MAPS_URL = "https://maps.app.goo.gl/test-branch"
_STORE_URL = "https://shop.example.sa"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("الموقع الإلكتروني", "website_url"),
        ("رابط الموقع", "website_url"),
        ("رابط المتجر", "website_url"),
        ("وين موقعكم؟", "physical_location"),
        ("ارسل اللوكيشن", "physical_location"),
        ("موقع المعرض", "physical_location"),
        ("رابط المنتج", "product_url"),
        ("رابط الدفع", "payment_link"),
    ],
)
def test_resolve_link_intent_p0_cases(message: str, expected: str) -> None:
    from modules.ai.brain.commerce.link_intent import (
        LinkIntentType,
        resolve_link_intent,
    )

    assert resolve_link_intent(message).value == expected


@pytest.mark.parametrize("message", [
    "الموقع الإلكتروني",
    "رابط الموقع",
    "رابط المتجر",
])
def test_website_phrases_classify_as_store_info(message: str) -> None:
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_STORE_INFO

    intent = match(message)
    assert intent is not None, f"expected intent for {message!r}"
    assert intent.name == INTENT_ASK_STORE_INFO


@pytest.mark.parametrize("message", [
    "وين موقعكم؟",
    "ارسل اللوكيشن",
    "موقع المعرض",
])
def test_physical_phrases_classify_as_location(message: str) -> None:
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_LOCATION

    intent = match(message)
    assert intent is not None, f"expected intent for {message!r}"
    assert intent.name == INTENT_ASK_LOCATION


def test_website_intent_routes_to_store_info_not_maps() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.execution.faq import TOPIC_STORE_INFO
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_ASK_STORE_INFO,
    )

    state = MerchantConversationState()
    facts = CommerceFacts(
        has_products=True,
        product_count=3,
        orderable=True,
        store_name="متجر الاختبار",
        store_url=_STORE_URL,
        maps_url=_MAPS_URL,
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="الموقع الإلكتروني",
        intent=Intent(
            name=INTENT_ASK_STORE_INFO,
            confidence=0.92,
            slots={},
            raw_message="الموقع الإلكتروني",
        ),
        state=state,
        facts=facts,
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_STORE_INFO
    assert decision.args.get("topic") != "location"


def test_rabt_almawqe_routes_to_store_info_not_maps() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.execution.faq import TOPIC_STORE_INFO
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_ASK_STORE_INFO,
    )

    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="رابط الموقع",
        intent=Intent(
            name=INTENT_ASK_STORE_INFO,
            confidence=0.92,
            slots={},
            raw_message="رابط الموقع",
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(
            store_url=_STORE_URL,
            maps_url=_MAPS_URL,
        ),
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == TOPIC_STORE_INFO


def test_physical_intent_routes_to_maps_faq() -> None:
    from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.execution.faq import TOPIC_LOCATION
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_ASK_LOCATION,
    )

    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="وين موقعكم؟",
        intent=Intent(
            name=INTENT_ASK_LOCATION,
            confidence=0.93,
            slots={},
            raw_message="وين موقعكم؟",
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(
            store_url=_STORE_URL,
            maps_url=_MAPS_URL,
        ),
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
    topic = str(decision.args.get("topic") or "")
    kind = str(decision.args.get("question_kind") or "")
    assert topic in {TOPIC_LOCATION, "location_delivery"} or kind == "location"


def test_website_phrase_skips_pre_brain_location_policy() -> None:
    from modules.ai.brain.commerce.location_link_policy import (
        evaluate_location_link_policy,
    )

    db = object()
    assert evaluate_location_link_policy(
        db, tenant_id=1, message="الموقع الإلكتروني",
    ) is None
    assert evaluate_location_link_policy(
        db, tenant_id=1, message="رابط الموقع",
    ) is None


def test_location_safety_net_skips_website_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_location_safety_net

    monkeypatch.setattr(
        "modules.ai.postprocess.safety_nets._lookup_tenant_maps_url",
        lambda _db, _tid: (_MAPS_URL, "snapshot"),
    )

    result = apply_location_safety_net(
        db=None,
        tenant_id=1,
        customer_msg="الموقع الإلكتروني",
        reply_text="تفضل 🌷",
    )
    assert not result.fired
    assert result.skipped_reason == "no_location_intent"


def test_store_link_safety_net_fires_for_website_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

    monkeypatch.setattr(
        "modules.ai.postprocess.safety_nets._lookup_tenant_store_url",
        lambda _db, _tid: _STORE_URL,
    )

    result = apply_store_link_safety_net(
        db=None,
        tenant_id=1,
        customer_msg="الموقع الإلكتروني",
        reply_text="تفضل 🌷",
    )
    assert result.fired
    assert _STORE_URL in (result.new_reply or "")
    assert _MAPS_URL not in (result.new_reply or "")


def test_faq_store_info_uses_website_wording() -> None:
    from modules.ai.brain.compose.templates import faq_store_info

    reply = faq_store_info(store_url=_STORE_URL, store_name="متجر")
    assert _STORE_URL in reply
    assert "رابط المتجر الإلكتروني" in reply
    assert _MAPS_URL not in reply


def test_product_and_payment_intents_not_confused_with_website_or_maps() -> None:
    from modules.ai.brain.commerce.link_intent import (
        LinkIntentType,
        resolve_link_intent,
    )

    assert resolve_link_intent("رابط المنتج") == LinkIntentType.PRODUCT_URL
    assert resolve_link_intent("رابط الدفع") == LinkIntentType.PAYMENT_LINK
    assert resolve_link_intent("ارسل رابط الطلح") == LinkIntentType.PRODUCT_URL
