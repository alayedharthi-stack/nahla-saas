"""
PR-D6A — storefront link request reliability + media-source guard.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_MAPS_URL = "https://maps.app.goo.gl/test-branch"
_STORE_URL = "https://shop.example.sa"

_VISION_STOREFRONT_OCR = (
    "[وصف الصورة المرسلة] لقطة شاشة تظهر نص رابط المتجر الإلكتروني "
    "والموقع الإلكتروني للمتجر"
)


def _brain_ctx(
    message: str,
    *,
    maps_url: str = _MAPS_URL,
    store_url: str = _STORE_URL,
):
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
    )

    intent = match(message) or Intent(name="general", confidence=0.5, raw_message=message)
    ctx = BrainContext(
        tenant_id=10,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=MerchantConversationState(),
        facts=CommerceFacts(
            has_products=True,
            store_url=store_url,
            maps_url=maps_url,
        ),
    )
    return DefaultDecisionEngine().decide(ctx)


class TestStorefrontLinkIntentReliability:
    def test_rabt_almatjar_alelectroni_storefront_route(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = "رابط المتجر الإلكتروني"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_almawqe_alelectroni_storefront_route(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = "الموقع الإلكتروني"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_wain_mawqecom_physical_not_storefront(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION, TOPIC_STORE_INFO

        msg = "وين موقعكم؟"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.PHYSICAL_LOCATION
        decision = _brain_ctx(msg)
        topic = str(decision.args.get("topic") or "")
        assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
        assert topic in {TOPIC_LOCATION, "location_delivery"}
        assert decision.args.get("topic") != TOPIC_STORE_INFO

    def test_mawqe_almaarid_physical_not_storefront(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION, TOPIC_STORE_INFO

        msg = "موقع المعرض"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.PHYSICAL_LOCATION
        decision = _brain_ctx(msg)
        topic = str(decision.args.get("topic") or "")
        assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
        assert topic in {TOPIC_LOCATION, "location_delivery"}
        assert decision.args.get("topic") != TOPIC_STORE_INFO

    def test_missing_store_url_honest_not_configured(self) -> None:
        from core.native_catalog_fallback import compose_native_catalog_failure_decision
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_inbound_link_intent
        from modules.ai.brain.compose.templates import MSG_STORE_LINK_NOT_CONFIGURED, faq_store_info
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION, TOPIC_STORE_INFO

        msg = "رابط المتجر الإلكتروني"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg, store_url="")
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO
        assert decision.args.get("topic") != TOPIC_LOCATION
        assert not decision.args.get("authorized_cta_url")

        reply = faq_store_info(store_url="", store_name="متجر")
        assert reply == MSG_STORE_LINK_NOT_CONFIGURED
        assert _MAPS_URL not in reply

        fallback = compose_native_catalog_failure_decision(None, 10, customer_message=msg)
        assert fallback.text != MSG_STORE_LINK_NOT_CONFIGURED
        assert _MAPS_URL not in (fallback.text or "")

    def test_vision_only_storefront_phrase_does_not_trigger_route(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.commerce.link_intent_media_source_guard import (
            link_intent_message,
        )
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = _VISION_STOREFRONT_OCR
        assert link_intent_message(msg) == ""
        assert resolve_inbound_link_intent(msg) == LinkIntentType.UNKNOWN_LINK
        decision = _brain_ctx(msg)
        assert not (
            decision.action == ACTION_FAQ_REPLY
            and decision.args.get("topic") == TOPIC_STORE_INFO
        )

    def test_customer_caption_with_vision_still_triggers_storefront(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = f"رابط المتجر الإلكتروني\n\n{_VISION_STOREFRONT_OCR}"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.WEBSITE_URL
        decision = _brain_ctx(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_link_intent_message_plain_text_passthrough(self) -> None:
        from modules.ai.brain.commerce.link_intent_media_source_guard import (
            is_media_framed_inbound_message,
            link_intent_message,
        )

        msg = "الموقع الإلكتروني"
        assert is_media_framed_inbound_message(msg) is False
        assert link_intent_message(msg) == msg


class TestD5StaffMediaGuardRegression:
    def test_tiktok_vision_does_not_trigger_staff_lookup(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import types as _types

        from modules.ai.brain.commerce.staff_contact_evidence import (
            classify_staff_contact_request,
            compile_staff_contact_registry,
        )
        from modules.ai.brain.commerce.staff_contact_media_source_guard import (
            staff_contact_intent_message,
        )

        call_stub = _types.ModuleType("services.call_resolver")
        call_stub.CallTarget = object  # type: ignore[attr-defined]
        call_stub._normalize_saudi_phone = lambda p: p  # type: ignore[attr-defined]
        call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)

        class _Section:
            def __init__(self) -> None:
                self.id = 1
                self.kind = "custom"
                self.body = "هشام: 0549815590"
                self.title = ""
                self.metadata = {}
                self.metadata_json = {}
                self.updated_at = 1

        sections = [_Section()]
        reg = compile_staff_contact_registry(sections)
        vision = (
            "[وصف الصورة المرسلة] Teddy&Abuk Get ready with us skincare edition"
        )
        assert staff_contact_intent_message(vision) == ""
        assert classify_staff_contact_request(vision, registry=reg).kind == "none"


class TestPhysicalLocationOwnershipRegression:
    def test_pr337_storefront_and_physical_cases_still_hold(self) -> None:
        from modules.ai.brain.commerce.link_intent import LinkIntentType, resolve_link_intent
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
        from modules.ai.brain.execution.faq import TOPIC_LOCATION, TOPIC_STORE_INFO

        physical_cases = ("وين موقعكم؟", "موقع المعرض")
        for msg in physical_cases:
            assert resolve_link_intent(msg) == LinkIntentType.PHYSICAL_LOCATION
            decision = _brain_ctx(msg)
            topic = str(decision.args.get("topic") or "")
            assert decision.action in {ACTION_FAQ_REPLY, ACTION_LLM_REPLY}
            assert topic in {TOPIC_LOCATION, "location_delivery"}
            assert topic != TOPIC_STORE_INFO

        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        storefront_cases = ("رابط المتجر الإلكتروني", "الموقع الإلكتروني")
        for msg in storefront_cases:
            assert resolve_link_intent(msg) == LinkIntentType.WEBSITE_URL
            decision = _brain_ctx(msg)
            assert decision.action == ACTION_LLM_REPLY
            assert decision.args.get("topic") == TOPIC_STORE_INFO
