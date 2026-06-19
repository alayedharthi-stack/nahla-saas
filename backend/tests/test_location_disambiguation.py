"""Regression tests for physical-location vs e-commerce store URL disambiguation.

Production failure (May 2026): customer said ``موقع المتجر`` and received
the online ``store_url`` instead of the Google Maps branch link.
"""
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


@pytest.mark.parametrize("message", [
    "موقع المتجر",
    "موقع المعرض",
    "موقع المحل",
    "وين موقعكم",
    "وين أنتم",
    "ارسل اللوكيشن",
])
def test_physical_phrases_classify_as_location_intent(message: str) -> None:
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_LOCATION

    intent = match(message)
    assert intent is not None, f"expected intent for {message!r}"
    assert intent.name == INTENT_ASK_LOCATION


@pytest.mark.parametrize("message", [
    "رابط المتجر",
    "المتجر الإلكتروني",
])
def test_ecommerce_phrases_classify_as_store_info(message: str) -> None:
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_STORE_INFO

    intent = match(message)
    assert intent is not None, f"expected intent for {message!r}"
    assert intent.name == INTENT_ASK_STORE_INFO


@pytest.mark.parametrize("message", [
    "موقعكم الإلكتروني",
    "رابط الطلب",
    "أبي أطلب من الموقع",
    "أونلاين",
])
def test_ecommerce_explicit_triggers_store_link_not_maps(message: str) -> None:
    from modules.ai.brain.intent.link_disambiguation import (
        looks_like_ecommerce_store_link_request,
        looks_like_physical_location_request,
    )

    assert looks_like_ecommerce_store_link_request(message)
    assert not looks_like_physical_location_request(message)


def test_mawqe_almatjar_routes_to_faq_location_when_maps_configured() -> None:
    from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY
    from modules.ai.brain.execution.faq import TOPIC_LOCATION
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_ASK_LOCATION,
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
    intent = Intent(
        name=INTENT_ASK_LOCATION,
        confidence=0.93,
        slots={},
        raw_message="موقع المتجر",
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="موقع المتجر",
        intent=intent,
        state=state,
        facts=facts,
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_FAQ_REPLY
    assert decision.args.get("topic") == TOPIC_LOCATION


def test_store_link_safety_net_suppressed_for_mawqe_almatjar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

    monkeypatch.setattr(
        "modules.ai.postprocess.safety_nets._lookup_tenant_store_url",
        lambda _db, _tid: (_STORE_URL, "snapshot"),
    )

    result = apply_store_link_safety_net(
        db=None,
        tenant_id=1,
        customer_msg="موقع المتجر",
        reply_text="تفضل 🌷",
    )
    assert not result.fired
    assert result.skipped_reason == "no_store_link_intent"
    assert _STORE_URL not in (result.new_reply or "")


def test_location_safety_net_injects_maps_for_mawqe_almatjar(
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
        customer_msg="موقع المتجر",
        reply_text="تفضل 🌷",
    )
    assert result.fired
    assert _MAPS_URL in (result.new_reply or "")
    assert _STORE_URL not in (result.new_reply or "")


def test_faq_location_template_prefers_maps_over_store_url() -> None:
    from modules.ai.brain.compose.templates import faq_location, faq_store_info

    loc = faq_location(maps_url=_MAPS_URL, store_name="آل عايد")
    store = faq_store_info(store_url=_STORE_URL, store_name="آل عايد")

    assert _MAPS_URL in loc
    assert _STORE_URL not in loc
    assert _STORE_URL in store
    assert _MAPS_URL not in store


def test_location_faq_skips_order_resume_hint() -> None:
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.execution.faq import TOPIC_LOCATION
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        OrderPreparationState,
    )

    prep = OrderPreparationState(product_id="p1")
    state = MerchantConversationState(
        greeted=True,
        current_product_focus={"id": "p1", "title": "عسل سدر"},
        order_prep=prep,
    )
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message="وين موقعكم؟",
        intent=Intent(name="ask_location", confidence=0.9, raw_message="وين موقعكم؟"),
        state=state,
        facts=CommerceFacts(has_products=True),
    )
    composer = DefaultComposer()
    out = composer._with_follow_up("موقعنا 📍", ctx, topic=TOPIC_LOCATION)
    assert "نكمل" not in out
    assert out == "موقعنا 📍"


def test_location_delivery_response_goal() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.pipeline import _compose_base_response_goal
    from modules.ai.brain.types import Decision, SuggestionSnapshot

    goal = _compose_base_response_goal(
        Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "location_delivery", "topic_hint": "location"},
            reason="location ask",
        ),
        SuggestionSnapshot(),
    )
    assert goal.startswith("location_delivery")
    assert "نكمل إنشاء طلب" in goal
    assert "maps" in goal.lower() or "CTA" in goal
