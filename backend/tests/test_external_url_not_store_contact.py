"""D3: customer-supplied URLs are not merchant store/contact requests."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.inbound_url_spans import semantic_text_excluding_url_spans
from core.wa_link_buttons import (
    bind_authorized_cta,
    consume_authorized_cta,
    split_text_for_cta_buttons,
)
from modules.ai.brain.commerce.merchant_profile_intents import (
    authorized_profile_cta_url,
    build_merchant_profile_decision,
    classify_store_profile_topic,
)
from modules.ai.brain.compose.templates import faq_owner_contact, faq_store_info
from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.classifier import DefaultIntentClassifier
from modules.ai.brain.intent.rules import match as rules_match
from modules.ai.brain.suggestion.engine import DefaultSuggestionEngine
from modules.ai.brain.types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_STORE_INFO,
    INTENT_GENERAL,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

GENERIC_STORE_URL = "https://demo.example/store-a"
GENERIC_STORE_B_URL = "https://other.example/store-b"
TIKTOK_LIVE = "https://vt.tiktok.com/test/"
SCHEMELESS = "vt.tiktok.com/test"
SOCIAL_IN_PATH = "https://cdn.example.net/tiktok/clip"
QUESTION = "وش رأيك؟"


def _facts(**kwargs: Any) -> CommerceFacts:
    facts = CommerceFacts(
        store_name=kwargs.get("store_name", "متجر تجريبي عام"),
        has_products=True,
        product_count=12,
        orderable=True,
        store_description="متجر تجريبي عام لمنتجات الملابس والأحذية.",
        store_url=kwargs.get("store_url", GENERIC_STORE_URL),
        store_contact_email=kwargs.get("email", "hello@demo.example"),
        store_contact_phone=kwargs.get("phone", "966500000001"),
    )
    setattr(
        facts,
        "merchant_profile_social_links",
        kwargs.get("social", {"instagram": "https://instagram.com/demo_store"}),
    )
    setattr(facts, "merchant_profile_currency", "SAR")
    setattr(facts, "merchant_profile_status", "active")
    return facts


def _ctx(message: str, intent: Intent, facts: CommerceFacts | None = None) -> BrainContext:
    facts = facts or _facts()
    return BrainContext(
        tenant_id=kwargs_tenant(facts),
        customer_phone="966500000001",
        message=message,
        history=[],
        profile={},
        intent=intent,
        state=MerchantConversationState(stage="browsing", greeted=True),
        facts=facts,
        merchant_context={
            "merchant_profile": {
                "description": facts.store_description,
                "domain": facts.store_url,
                "email": facts.store_contact_email,
                "phone": facts.store_contact_phone,
                "social_links": getattr(facts, "merchant_profile_social_links", {}) or {},
                "currency": "SAR",
                "status": "active",
            }
        },
    )


def kwargs_tenant(facts: CommerceFacts) -> int:
    return int(getattr(facts, "tenant_id", 1) or 1)


def _intent_for(message: str) -> Intent:
    return rules_match(message) or Intent(
        name=INTENT_GENERAL,
        confidence=0.50,
        slots={},
        raw_message=message,
    )


def _decide(message: str, facts: CommerceFacts | None = None) -> Decision:
    intent = _intent_for(message)
    ctx = _ctx(message, intent, facts)
    ctx._db = object()
    return DefaultDecisionEngine().decide(ctx)


class TestAGenericBareUrl:
    def test_generic_bare_url_not_contact_or_store(self) -> None:
        msg = "https://example.net/share/abc"
        assert rules_match(msg) is None or rules_match(msg).name not in {
            INTENT_ASK_OWNER_CONTACT,
            INTENT_ASK_STORE_INFO,
        }
        assert classify_store_profile_topic(msg) is None
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") not in {"owner_contact", "store_info"}
        assert not decision.args.get("authorized_cta_url")


class TestBLiveShapedSocialUrl:
    def test_vt_tiktok_url_not_owner_contact(self) -> None:
        msg = TIKTOK_LIVE
        assert classify_store_profile_topic(msg) is None
        matched = rules_match(msg)
        assert matched is None or matched.name != INTENT_ASK_OWNER_CONTACT
        decision = _decide(msg)
        assert decision.action != ACTION_FAQ_REPLY
        assert decision.args.get("topic") != "owner_contact"
        assert not decision.args.get("authorized_cta_url")


class TestCSocialTokenInsidePath:
    def test_generic_host_tiktok_path(self) -> None:
        msg = SOCIAL_IN_PATH
        assert classify_store_profile_topic(msg) is None
        matched = rules_match(msg)
        assert matched is None or matched.name != INTENT_ASK_OWNER_CONTACT

    def test_website_token_inside_url_path(self) -> None:
        msg = "https://cdn.example.net/website/clip"
        assert classify_store_profile_topic(msg) is None
        decision = _decide(msg)
        assert decision.args.get("topic") not in {"owner_contact", "store_info"}
        assert not decision.args.get("authorized_cta_url")


class TestDSchemelessUrl:
    def test_schemeless_same_protection(self) -> None:
        assert classify_store_profile_topic(SCHEMELESS) is None
        matched = rules_match(SCHEMELESS)
        assert matched is None or matched.name != INTENT_ASK_OWNER_CONTACT
        decision = _decide(SCHEMELESS)
        assert decision.args.get("topic") not in {"owner_contact", "store_info"}


class TestEUrlPlusQuestion:
    def test_question_preserved_no_merchant_cta(self) -> None:
        msg = f"{TIKTOK_LIVE} {QUESTION}"
        assert semantic_text_excluding_url_spans(msg) == QUESTION
        assert classify_store_profile_topic(msg) is None
        decision = _decide(msg)
        assert decision.args.get("topic") not in {"owner_contact", "store_info"}
        assert not decision.args.get("authorized_cta_url")


class TestFExplicitSocialRequest:
    def test_tiktok_account_request_is_owner_contact_llm(self) -> None:
        msg = "وش حسابكم في تيك توك؟"
        assert classify_store_profile_topic(msg) == "owner_contact"
        matched = rules_match(msg)
        assert matched is not None and matched.name == INTENT_ASK_OWNER_CONTACT
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "owner_contact"
        assert decision.args.get("profile_surface") == "merchant_profile"
        assert "faq_owner_contact" not in str(decision.args)


class TestGExplicitStoreRequest:
    def test_store_link_request_may_attach_tenant_url(self) -> None:
        msg = "وش رابط المتجر؟"
        assert classify_store_profile_topic(msg) == "store_info"
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "store_info"
        assert decision.args.get("authorized_cta_url") == GENERIC_STORE_URL
        assert decision.args.get("authorized_cta_url") != TIKTOK_LIVE


class TestHUrlPlusExplicitStoreRequest:
    def test_remainder_owns_store_request(self) -> None:
        msg = f"{TIKTOK_LIVE} وش رابط متجركم؟"
        assert semantic_text_excluding_url_spans(msg).startswith("وش رابط")
        assert classify_store_profile_topic(msg) == "store_info"
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "store_info"
        assert decision.args.get("authorized_cta_url") == GENERIC_STORE_URL
        assert TIKTOK_LIVE.rstrip("/") not in str(decision.args.get("authorized_cta_url") or "")


class TestIGenericContactRequest:
    def test_generic_contact_no_automatic_store_cta(self) -> None:
        msg = "كيف أتواصل معكم؟"
        assert classify_store_profile_topic(msg) == "owner_contact"
        decision = _decide(msg)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "owner_contact"
        assert not decision.args.get("authorized_cta_url")


class TestJModelOwnsProse:
    def test_valid_turns_are_llm_not_faq_templates(self) -> None:
        contact = _decide("كيف أتواصل معكم؟")
        store = _decide("وش رابط المتجر؟")
        for decision in (contact, store):
            assert decision.action == ACTION_LLM_REPLY
            assert decision.action != ACTION_FAQ_REPLY
        from modules.ai.brain.types import ActionResult

        follow = DefaultSuggestionEngine().suggest(
            _ctx("كيف أتواصل معكم؟", _intent_for("كيف أتواصل معكم؟")),
            Decision(action=ACTION_LLM_REPLY, args={"topic": "owner_contact"}, reason="t"),
            ActionResult(success=True, data={"topic": "owner_contact"}),
        )
        assert "إذا تحب أساعدك هنا مباشرة قبل التواصل" not in str(
            getattr(follow, "follow_up_question", "") or ""
        )
        # Templates still exist but are not the Decision owner.
        assert "تقدر تتواصل معنا عبر" in faq_owner_contact(store_url=GENERIC_STORE_URL)
        assert GENERIC_STORE_URL in faq_store_info(store_url=GENERIC_STORE_URL)

    def test_url_only_skips_slot_extractor(self) -> None:
        import asyncio

        clf = DefaultIntentClassifier()

        async def _run() -> None:
            with patch(
                "modules.ai.brain.intent.slot_extractor.extract_slots",
                new_callable=AsyncMock,
            ) as extract:
                intent = await clf.classify(
                    TIKTOK_LIVE,
                    [],
                    MerchantConversationState(stage="browsing", greeted=True),
                )
            extract.assert_not_called()
            assert intent.name == INTENT_GENERAL
            assert intent.raw_message == TIKTOK_LIVE

        asyncio.run(_run())


class TestKStructuredCtaOutOfBand:
    def test_authorized_cta_does_not_append_url_to_body(self) -> None:
        consume_authorized_cta()
        body = "تقدر ت tap المتجر من الزر."
        msgs = split_text_for_cta_buttons(
            body,
            authorized_cta_url=GENERIC_STORE_URL,
            inbound_url_spans=[TIKTOK_LIVE],
        )
        assert len(msgs) == 1
        assert msgs[0].cta is not None
        assert msgs[0].cta.url.rstrip("/") == GENERIC_STORE_URL.rstrip("/")
        assert GENERIC_STORE_URL not in msgs[0].body
        assert msgs[0].body == body


class TestLInboundUrlNeverBecomesCta:
    def test_customer_url_rejected_as_authorized_cta(self) -> None:
        facts = _facts(store_url=TIKTOK_LIVE)
        cta = authorized_profile_cta_url(
            topic="store_info",
            message=TIKTOK_LIVE,
            facts=facts,
        )
        assert cta == ""

    def test_body_echo_of_inbound_is_not_lifted(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url="", inbound_url_spans=[TIKTOK_LIVE])
        msgs = split_text_for_cta_buttons(f"شفت {TIKTOK_LIVE}")
        assert all(item.cta is None for item in msgs)


class TestMMissingConfiguredChannel:
    def test_explicit_social_without_matching_link_no_store_cta(self) -> None:
        facts = _facts(social={"instagram": "https://instagram.com/demo_store"})
        decision = build_merchant_profile_decision(
            message="وش حسابكم في تيك توك؟",
            facts=facts,
            merchant_context={"merchant_profile": {"social_links": facts.merchant_profile_social_links, "domain": GENERIC_STORE_URL}},
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert not decision.args.get("authorized_cta_url")


class TestNNoExternalFetch:
    def test_classify_and_decide_do_not_http(self) -> None:
        calls: list[str] = []

        class _Boom:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                calls.append("init")
                raise AssertionError("external fetch not authorized")

        with patch("urllib.request.urlopen", _Boom), patch(
            "http.client.HTTPConnection", _Boom
        ):
            classify_store_profile_topic(TIKTOK_LIVE)
            rules_match(TIKTOK_LIVE)
            _decide(TIKTOK_LIVE)
        assert calls == []


class TestOTenantIsolation:
    def test_tenant_b_does_not_receive_tenant_a_cta(self) -> None:
        facts_a = _facts(store_url=GENERIC_STORE_URL)
        facts_b = _facts(store_url=GENERIC_STORE_B_URL, email="b@other.example")
        setattr(facts_b, "tenant_id", 2)
        a = _decide("وش رابط المتجر؟", facts_a)
        b = _decide("وش رابط المتجر؟", facts_b)
        assert a.args.get("authorized_cta_url") == GENERIC_STORE_URL
        assert b.args.get("authorized_cta_url") == GENERIC_STORE_B_URL
        assert a.args.get("authorized_cta_url") != b.args.get("authorized_cta_url")


class TestPNoTemplateReplay:
    def test_second_distinct_url_is_not_faq_replay(self) -> None:
        first = _decide("https://example.net/one")
        second = _decide("https://example.net/two")
        for decision in (first, second):
            assert decision.action == ACTION_LLM_REPLY
            assert decision.args.get("topic") not in {"owner_contact", "store_info"}
            body = faq_owner_contact(store_url=GENERIC_STORE_URL)
            assert decision.reason != body


class TestRawMessagePreserved:
    def test_rules_raw_message_keeps_url(self) -> None:
        msg = f"{TIKTOK_LIVE} وش رابط متجركم؟"
        matched = rules_match(msg)
        assert matched is not None
        assert matched.raw_message == msg
        assert TIKTOK_LIVE in matched.raw_message
