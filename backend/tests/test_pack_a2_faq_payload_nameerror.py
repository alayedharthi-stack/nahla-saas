"""Pack A2 — FAQ compose must bind ActionResult payload (NameError regression)."""
from __future__ import annotations

import asyncio
from typing import Any

from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY
from modules.ai.brain.execution.faq import (
    FAQReplyHandler,
    TOPIC_OWNER_CONTACT,
    TOPIC_STORE_ABOUT,
    TOPIC_STORE_INFO,
)
from modules.ai.brain.types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _facts(**kwargs: Any) -> CommerceFacts:
    facts = CommerceFacts(
        store_name=kwargs.get("store_name", "متجر تجريبي عام"),
        store_description=kwargs.get(
            "description",
            "متجر تجريبي عام لمنتجات الملابس والأحذية.",
        ),
        store_url=kwargs.get("domain", "https://demo.example/store-a"),
        store_contact_email=kwargs.get("email", "hello@demo.example"),
        store_contact_phone=kwargs.get("phone", ""),
        has_products=True,
    )
    setattr(
        facts,
        "merchant_profile_social_links",
        kwargs.get("social", {"instagram": "https://instagram.com/demo_store"}),
    )
    setattr(facts, "merchant_profile_currency", kwargs.get("currency", "SAR"))
    setattr(facts, "merchant_profile_status", kwargs.get("status", "active"))
    return facts


def _ctx(message: str, facts: CommerceFacts, intent_name: str = "ask_store_info") -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        history=[],
        profile={},
        intent=Intent(name=intent_name, confidence=0.9, slots={}, raw_message=message),
        state=MerchantConversationState(stage="browsing", greeted=True),
        facts=facts,
    )


async def _compose_faq(topic: str, message: str, *, intent_name: str = "ask_store_info") -> str:
    facts = _facts()
    ctx = _ctx(message, facts, intent_name=intent_name)
    decision = Decision(action=ACTION_FAQ_REPLY, args={"topic": topic}, reason="test")
    result = await FAQReplyHandler().handle(decision, ctx)
    assert result.success
    assert isinstance((result.data or {}).get("payload"), dict)
    text = await DefaultComposer().compose(decision, result, ctx)
    return str(text or "")


class TestFaqPayloadBindRegression:
    def test_missing_payload_bind_was_nameerror_shape(self) -> None:
        """Document the live defect: using payload without binding raises NameError."""
        data = {
            "topic": TOPIC_STORE_ABOUT,
            "payload": {
                "store_name": "Store",
                "store_description": "Description A",
                "store_url": "",
            },
        }
        raised = False
        try:
            # Mimic broken compose branch: topic read, payload never bound.
            topic = data.get("topic", "")
            assert topic == TOPIC_STORE_ABOUT
            _ = payload.get("store_description", "")  # noqa: F821
        except NameError as exc:
            raised = True
            assert "payload" in str(exc)
        assert raised

    def test_about_compose_uses_description(self) -> None:
        text = asyncio.run(_compose_faq(TOPIC_STORE_ABOUT, "حدثني عن المتجر"))
        assert "ملابس" in text or "تجريبي" in text
        assert "حصل خطأ" not in text

    def test_store_url_compose_uses_domain(self) -> None:
        text = asyncio.run(_compose_faq(TOPIC_STORE_INFO, "وش رابط المتجر؟"))
        assert "demo.example/store-a" in text
        assert "لا يوجد" not in text
        assert "حصل خطأ" not in text

    def test_contact_compose_uses_email_social_no_phone(self) -> None:
        text = asyncio.run(
            _compose_faq(
                TOPIC_OWNER_CONTACT,
                "كيف أتواصل معكم؟",
                intent_name="ask_owner_contact",
            )
        )
        assert "hello@demo.example" in text
        assert "instagram.com/demo_store" in text
        assert "الجوال:" not in text
        assert "حصل خطأ" not in text

    def test_email_and_social_topics_share_owner_contact(self) -> None:
        email = asyncio.run(
            _compose_faq(TOPIC_OWNER_CONTACT, "وش إيميلكم؟", intent_name="ask_owner_contact")
        )
        social = asyncio.run(
            _compose_faq(
                TOPIC_OWNER_CONTACT,
                "عندكم حسابات تواصل؟",
                intent_name="ask_owner_contact",
            )
        )
        assert "hello@demo.example" in email
        assert "instagram" in social.lower() or "instagram.com/demo_store" in social

    def test_currency_status_remain_llm_not_faq(self) -> None:
        from modules.ai.brain.commerce.merchant_profile_intents import (
            build_merchant_profile_decision,
        )

        currency = build_merchant_profile_decision(message="وش العملة؟")
        status = build_merchant_profile_decision(message="هل المتجر نشط؟")
        assert currency is not None and currency.action == ACTION_LLM_REPLY
        assert status is not None and status.action == ACTION_LLM_REPLY

    def test_composer_tolerates_non_dict_payload(self) -> None:
        facts = _facts()
        ctx = _ctx("وش رابط المتجر؟", facts)
        decision = Decision(
            action=ACTION_FAQ_REPLY,
            args={"topic": TOPIC_STORE_INFO},
            reason="test",
        )
        result = ActionResult(
            success=True,
            data={"type": "faq", "topic": TOPIC_STORE_INFO, "payload": None},
        )
        text = asyncio.run(DefaultComposer().compose(decision, result, ctx))
        # Falls back to empty fields → no-url message, but must not NameError.
        assert isinstance(text, str)
        assert "حصل خطأ" not in text
