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
        assert not decision.args.get("authorized_cta_url")
        social = getattr(_facts(), "merchant_profile_social_links", {}) or {}
        assert social.get("instagram")
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


TIKTOK_CTA = "https://social.example/merchant"
INSTAGRAM_CTA = "https://instagram.example/merchant"
MODEL_BODY = "هذا رد نموذجي بدون رابط خام."


def _http_boom_patches(calls: list[str]):
    class _Boom:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.append("init")
            raise AssertionError("external fetch not authorized")

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("call")
            raise AssertionError("external fetch not authorized")

        def __enter__(self) -> "_Boom":
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def get(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("get")
            raise AssertionError("external fetch not authorized")

        def head(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("head")
            raise AssertionError("external fetch not authorized")

        def request(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("request")
            raise AssertionError("external fetch not authorized")

        async def __aenter__(self) -> "_Boom":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    import importlib
    from contextlib import ExitStack
    from unittest.mock import patch as _patch

    stack = ExitStack()
    targets = [
        "urllib.request.urlopen",
        "urllib.request.Request",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "socket.getaddrinfo",
    ]
    optional = [
        "httpx.Client",
        "httpx.AsyncClient",
        "httpx.get",
        "httpx.head",
        "httpx.request",
        "requests.get",
        "requests.head",
        "requests.request",
        "requests.Session",
        "aiohttp.ClientSession",
    ]
    for target in targets:
        stack.enter_context(_patch(target, _Boom))
    for target in optional:
        mod_name = target.split(".", 1)[0]
        try:
            importlib.import_module(mod_name)
        except Exception:
            continue
        stack.enter_context(_patch(target, _Boom))
    return stack


def _simulate_webhook_split(reply: str):
    """Production wire boundary: whatsapp_webhook.py calls split with reply only."""
    from core.wa_link_buttons import split_text_for_cta_buttons as _split_cta

    return _split_cta(reply or "")


def _wire_send_kwargs(reply: str, msgs) -> dict[str, Any]:
    """Mirrors whatsapp_webhook.py single-CTA `_send_cta_url` arguments."""
    if not msgs or msgs[0].cta is None:
        return {"body_text": reply, "btn_url": None}
    msg = msgs[0]
    return {
        "body_text": msg.body or reply,
        "btn_url": msg.cta.url,
        "btn_label": msg.cta.button_title,
    }


class TestTrustValidatorFailClosed:
    def test_import_failure_no_cta(self) -> None:
        import sys
        import types

        fake = types.ModuleType("modules.ai.brain.commerce.storefront_product_url")
        with patch.dict(sys.modules, {"modules.ai.brain.commerce.storefront_product_url": fake}):
            cta = authorized_profile_cta_url(
                topic="store_info",
                message="وش رابط المتجر؟",
                facts=_facts(),
            )
        assert cta == ""

    def test_validator_raises_no_cta(self) -> None:
        with patch(
            "modules.ai.brain.commerce.storefront_product_url.is_trusted_merchant_http_url",
            side_effect=RuntimeError("validator exploded"),
        ):
            cta = authorized_profile_cta_url(
                topic="store_info",
                message="وش رابط المتجر؟",
                facts=_facts(),
            )
        assert cta == ""

    def test_malformed_non_http_no_cta(self) -> None:
        for bad in ("javascript:alert(1)", "not a url", "ftp://files.example/x"):
            cta = authorized_profile_cta_url(
                topic="store_info",
                message="وش رابط المتجر؟",
                facts=_facts(store_url=bad),
            )
            assert cta == "", bad

    def test_customer_url_identical_to_candidate_no_cta(self) -> None:
        facts = _facts(store_url=TIKTOK_LIVE)
        cta = authorized_profile_cta_url(
            topic="store_info",
            message=TIKTOK_LIVE,
            facts=facts,
        )
        assert cta == ""

    def test_valid_tenant_store_url_cta_allowed(self) -> None:
        cta = authorized_profile_cta_url(
            topic="store_info",
            message="وش رابط المتجر؟",
            facts=_facts(store_url=GENERIC_STORE_URL),
        )
        assert cta == GENERIC_STORE_URL


class TestEmptyModelBodyNoDeterministicCtaProse:
    def test_authorized_cta_empty_body_does_not_invent_prose(self) -> None:
        consume_authorized_cta()
        msgs = split_text_for_cta_buttons(
            "",
            authorized_cta_url=GENERIC_STORE_URL,
        )
        assert len(msgs) == 1
        assert msgs[0].cta is None
        assert msgs[0].body == ""
        assert "اضغط" not in (msgs[0].body or "")
        assert "متجرنا" not in (msgs[0].body or "")

    def test_contextvar_empty_body_same_contract(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url=GENERIC_STORE_URL)
        msgs = split_text_for_cta_buttons("")
        assert msgs[0].cta is None
        assert msgs[0].body == ""


class TestConfiguredSocialChannel:
    def test_explicit_tiktok_is_owner_contact_without_channel_cta(self) -> None:
        facts = _facts(social={"tiktok": TIKTOK_CTA, "instagram": INSTAGRAM_CTA})
        decision = _decide("وش حسابكم في تيك توك؟", facts)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "owner_contact"
        assert not decision.args.get("authorized_cta_url")
        assert decision.args.get("authorized_cta_url") != GENERIC_STORE_URL
        social = getattr(facts, "merchant_profile_social_links", {}) or {}
        assert social.get("tiktok") == TIKTOK_CTA
        assert social.get("instagram") == INSTAGRAM_CTA

    def test_configured_instagram_still_no_deterministic_cta(self) -> None:
        facts = _facts(social={"tiktok": TIKTOK_CTA, "instagram": INSTAGRAM_CTA})
        decision = _decide("وش حسابكم في انستقرام؟", facts)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "owner_contact"
        assert not decision.args.get("authorized_cta_url")
        social = getattr(facts, "merchant_profile_social_links", {}) or {}
        assert social.get("instagram") == INSTAGRAM_CTA

    def test_missing_channel_no_store_substitute(self) -> None:
        facts = _facts(social={"instagram": INSTAGRAM_CTA})
        decision = _decide("وش حسابكم في تيك توك؟", facts)
        assert decision.action == ACTION_LLM_REPLY
        assert not decision.args.get("authorized_cta_url")
        assert decision.args.get("authorized_cta_url") != GENERIC_STORE_URL

    def test_generic_contact_no_automatic_store_or_social_cta(self) -> None:
        facts = _facts(social={"tiktok": TIKTOK_CTA})
        decision = _decide("كيف أتواصل معكم؟", facts)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "owner_contact"
        assert not decision.args.get("authorized_cta_url")


class TestResponseGoalProseRemoved:
    def test_contact_and_store_decisions_have_no_new_response_goal(self) -> None:
        contact = _decide("كيف أتواصل معكم؟")
        store = _decide("وش رابط المتجر؟")
        for decision in (contact, store):
            assert "response_goal" not in (decision.args or {})
            blob = str(decision.args)
            assert "Do not invent" not in blob
            assert "customer-supplied URL" not in blob
            assert "Answer the contact" not in blob
            assert "Answer the store URL" not in blob

    def test_compose_goal_does_not_carry_new_english_script(self) -> None:
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import SuggestionSnapshot

        for args in (
            {
                "topic": "owner_contact",
                "question_kind": "owner_contact",
                "authorized_cta_url": TIKTOK_CTA,
            },
            {
                "topic": "store_info",
                "question_kind": "store_url",
                "authorized_cta_url": GENERIC_STORE_URL,
            },
        ):
            goal = _compose_base_response_goal(
                Decision(action=ACTION_LLM_REPLY, args=args, reason="t"),
                SuggestionSnapshot(),
            )
            assert "Do not invent URLs" not in goal
            assert "customer-supplied URL" not in goal
            assert "Answer the contact / social-channel" not in goal


class TestCtaContextVarIsolation:
    def test_consume_occurs_exactly_once(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url=GENERIC_STORE_URL)
        first = split_text_for_cta_buttons(MODEL_BODY)
        second = split_text_for_cta_buttons(MODEL_BODY)
        assert first[0].cta is not None
        assert first[0].cta.url.rstrip("/") == GENERIC_STORE_URL.rstrip("/")
        assert second[0].cta is None

    def test_sequential_turns_do_not_inherit(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url=GENERIC_STORE_URL)
        _simulate_webhook_split(MODEL_BODY)
        leftover = consume_authorized_cta()
        assert leftover is None or not str(getattr(leftover, "url", "") or "")
        bind_authorized_cta(url=TIKTOK_CTA)
        msgs = _simulate_webhook_split("second-turn-body")
        assert msgs[0].cta is not None
        assert msgs[0].cta.url == TIKTOK_CTA
        assert GENERIC_STORE_URL not in (msgs[0].cta.url or "")

    def test_buttons_branch_does_not_leave_cta(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url=GENERIC_STORE_URL)
        consume_authorized_cta()
        msgs = _simulate_webhook_split("later-turn")
        assert all(item.cta is None for item in msgs)

    def test_exception_clears_stale_binding(self) -> None:
        consume_authorized_cta()
        bind_authorized_cta(url=GENERIC_STORE_URL)
        try:
            raise RuntimeError("cancelled send")
        except RuntimeError:
            consume_authorized_cta()
        msgs = _simulate_webhook_split(MODEL_BODY)
        assert all(item.cta is None for item in msgs)

    def test_concurrent_turns_do_not_exchange_cta(self) -> None:
        import asyncio

        async def _turn(url: str, body: str) -> str:
            bind_authorized_cta(url=url)
            await asyncio.sleep(0)
            msgs = split_text_for_cta_buttons(body)
            assert msgs[0].cta is not None
            return str(msgs[0].cta.url)

        async def _run() -> None:
            consume_authorized_cta()
            first, second = await asyncio.gather(
                _turn(GENERIC_STORE_URL, "body-a"),
                _turn(TIKTOK_CTA, "body-b"),
            )
            assert first.rstrip("/") == GENERIC_STORE_URL.rstrip("/")
            assert second == TIKTOK_CTA

        asyncio.run(_run())


class TestNoExternalFetchStrengthened(TestNNoExternalFetch):
    def test_classify_and_decide_do_not_http(self) -> None:
        calls: list[str] = []
        with _http_boom_patches(calls):
            classify_store_profile_topic(TIKTOK_LIVE)
            rules_match(TIKTOK_LIVE)
            _decide(TIKTOK_LIVE)
            authorized_profile_cta_url(
                topic="store_info",
                message=f"{TIKTOK_LIVE} وش رابط المتجر؟",
                facts=_facts(),
            )
        assert calls == []


def _d3_brain_process_stack(brain: Any, *, message: str, decision: Decision, model_body: str):
    import asyncio
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from modules.ai.brain.types import MerchantConversationState

    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        )
    )
    stack.enter_context(
        patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None),
        )
    )
    intent = _intent_for(message)
    state = MerchantConversationState(stage="browsing", greeted=True)
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    facts = _facts()
    merchant_ctx = {
        "tenant_id": 1,
        "merchant_profile": {
            "description": facts.store_description,
            "domain": facts.store_url,
            "email": facts.store_contact_email,
            "phone": facts.store_contact_phone,
            "social_links": getattr(facts, "merchant_profile_social_links", {}) or {},
            "currency": "SAR",
            "status": "active",
        },
    }
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=facts))
    stack.enter_context(
        patch("core.store_knowledge.build_merchant_context", return_value=merchant_ctx)
    )
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    captured: dict[str, Any] = {}

    async def _compose(decision: Decision, result: Any, ctx: Any, *args: Any, **kwargs: Any) -> str:
        rs = getattr(ctx, "reply_state", None)
        kf = dict(getattr(rs, "known_facts", None) or {})
        mc = dict(getattr(rs, "merchant_context", None) or getattr(ctx, "merchant_context", None) or {})
        mp = mc.get("merchant_profile") if isinstance(mc.get("merchant_profile"), dict) else {}
        proj = kf.get("trusted_context_projection")
        proj_mp = {}
        if isinstance(proj, dict):
            raw_mp = proj.get("merchant_profile")
            if isinstance(raw_mp, dict):
                proj_mp = raw_mp
        social = (
            proj_mp.get("social_links")
            or mp.get("social_links")
            or getattr(getattr(ctx, "facts", None), "merchant_profile_social_links", None)
            or {}
        )
        prompt = ""
        if rs is not None:
            try:
                from modules.ai.brain.compose.prompt_builder import (  # noqa: PLC0415
                    build_brain_reply_prompt,
                )

                prompt = build_brain_reply_prompt(rs)
            except Exception:
                prompt = ""
        captured["known_facts"] = kf
        captured["merchant_profile"] = mp
        captured["contact_phone"] = kf.get("contact_phone") or getattr(
            getattr(ctx, "facts", None), "store_contact_phone", ""
        )
        captured["contact_email"] = kf.get("contact_email") or getattr(
            getattr(ctx, "facts", None), "store_contact_email", ""
        )
        captured["store_url"] = kf.get("store_url") or getattr(
            getattr(ctx, "facts", None), "store_url", ""
        )
        captured["social_links"] = social
        captured["prompt"] = prompt
        captured["decision_cta"] = (getattr(decision, "args", None) or {}).get(
            "authorized_cta_url"
        )
        return model_body

    llm_mock = stack.enter_context(
        patch.object(
            brain._composer,
            "compose",
            new_callable=AsyncMock,
            side_effect=_compose,
        )
    )
    faq_contact = stack.enter_context(
        patch(
            "modules.ai.brain.compose.templates.faq_owner_contact",
            side_effect=AssertionError("faq_owner_contact invoked"),
        )
    )
    faq_store = stack.enter_context(
        patch(
            "modules.ai.brain.compose.templates.faq_store_info",
            side_effect=AssertionError("faq_store_info invoked"),
        )
    )
    persona = stack.enter_context(
        patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        )
    )
    return stack, llm_mock, faq_contact, faq_store, persona, asyncio, MagicMock, captured


def _process_then_wire(brain, *, db, message: str):
    async def _run():
        result = await brain.process(
            db=db,
            tenant_id=1,
            customer_phone="966500000001",
            message=message,
            history=[],
            profile={"preferred_language": "ar"},
            conversation_id=42,
        )
        msgs = _simulate_webhook_split(result.get("reply") or "")
        return result, msgs

    import asyncio as _asyncio

    return _asyncio.run(_run())


class TestModelOwnershipComposeOnce:
    def test_contact_and_store_are_model_owned(self) -> None:
        from modules.ai.brain.pipeline import get_brain

        cases = (
            ("كيف أتواصل معكم؟", "owner_contact"),
            ("وش حسابكم في تيك توك؟", "owner_contact"),
            ("وش رابط المتجر؟", "store_info"),
        )
        for message, topic in cases:
            decision = _decide(message)
            assert decision.action == ACTION_LLM_REPLY
            assert decision.args.get("topic") == topic
            consume_authorized_cta()
            brain = get_brain()
            stack, llm_mock, _fc, _fs, _persona, asyncio, MagicMock, captured = _d3_brain_process_stack(
                brain, message=message, decision=decision, model_body=MODEL_BODY
            )
            db = MagicMock()
            with stack:
                result = asyncio.run(
                    brain.process(
                        db=db,
                        tenant_id=1,
                        customer_phone="966500000001",
                        message=message,
                        history=[],
                        profile={"preferred_language": "ar"},
                        conversation_id=42,
                    )
                )
            assert llm_mock.await_count == 1
            assert _persona.await_count == 0
            assert result.get("reply") == MODEL_BODY
            assert result.get("compose_reply_candidate") == MODEL_BODY
            assert GENERIC_STORE_URL not in (result.get("reply") or "")
            assert TIKTOK_LIVE not in (result.get("reply") or "")
            assert captured.get("contact_phone") == "966500000001"
            assert captured.get("contact_email") == "hello@demo.example"
            social = captured.get("social_links") or {}
            assert social.get("instagram") == "https://instagram.com/demo_store"
            prompt = str(captured.get("prompt") or "")
            if prompt:
                assert "966500000001" in prompt
                assert "hello@demo.example" in prompt
                assert "https://instagram.com/demo_store" in prompt
            if topic == "store_info":
                assert result.get("authorized_cta_url") == GENERIC_STORE_URL
                assert captured.get("store_url") == GENERIC_STORE_URL
                assert GENERIC_STORE_URL not in MODEL_BODY
            else:
                assert not result.get("authorized_cta_url")
            follow = DefaultSuggestionEngine().suggest(
                _ctx(message, _intent_for(message)),
                decision,
                __import__(
                    "modules.ai.brain.types", fromlist=["ActionResult"]
                ).ActionResult(success=True, data={"topic": topic}),
            )
            assert "إذا تحب أساعدك هنا مباشرة قبل التواصل" not in str(
                getattr(follow, "follow_up_question", "") or ""
            )


class TestPipelineToWireCta:
    def test_store_request_wire_consumes_authorized_cta(self) -> None:
        from modules.ai.brain.pipeline import get_brain

        message = "وش رابط المتجر؟"
        decision = _decide(message)
        consume_authorized_cta()
        brain = get_brain()
        stack, llm_mock, _fc, _fs, _persona, asyncio, MagicMock, captured = _d3_brain_process_stack(
            brain, message=message, decision=decision, model_body=MODEL_BODY
        )
        db = MagicMock()
        with stack:
            result, msgs = _process_then_wire(brain, db=db, message=message)
        send = _wire_send_kwargs(result.get("reply") or "", msgs)
        assert llm_mock.await_count == 1
        assert result.get("reply") == MODEL_BODY
        assert result.get("compose_reply_candidate") == MODEL_BODY
        assert GENERIC_STORE_URL not in (result.get("reply") or "")
        assert GENERIC_STORE_URL not in MODEL_BODY
        assert captured.get("store_url") == GENERIC_STORE_URL
        assert result.get("authorized_cta_url") == captured.get("store_url")
        assert send["btn_url"].rstrip("/") == GENERIC_STORE_URL.rstrip("/")
        assert send["body_text"] == MODEL_BODY
        assert TIKTOK_LIVE not in str(send["btn_url"] or "")

    def test_bare_external_url_has_no_merchant_cta_on_wire(self) -> None:
        from modules.ai.brain.pipeline import get_brain

        message = TIKTOK_LIVE
        decision = _decide(message)
        consume_authorized_cta()
        brain = get_brain()
        stack, _llm, _fc, _fs, _persona, asyncio, MagicMock, _captured = _d3_brain_process_stack(
            brain, message=message, decision=decision, model_body=MODEL_BODY
        )
        db = MagicMock()
        with stack:
            result, msgs = _process_then_wire(brain, db=db, message=message)
        send = _wire_send_kwargs(result.get("reply") or "", msgs)
        assert not result.get("authorized_cta_url")
        assert send["btn_url"] is None
        assert all(item.cta is None for item in msgs)
        assert send["body_text"] == MODEL_BODY

    def test_handoff_branch_does_not_leave_cta_for_later_split(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        from modules.ai.brain.pipeline import get_brain

        consume_authorized_cta()
        brain = get_brain()
        decision = Decision(
            action=ACTION_HANDOFF,
            args={"authorized_cta_url": GENERIC_STORE_URL, "topic": "store_info"},
            reason="t",
        )
        stack, _llm, _fc, _fs, _persona, asyncio, MagicMock, _captured = _d3_brain_process_stack(
            brain, message="وش رابط المتجر؟", decision=decision, model_body=MODEL_BODY
        )
        db = MagicMock()
        with stack:
            async def _run():
                await brain.process(
                    db=db,
                    tenant_id=1,
                    customer_phone="966500000001",
                    message="وش رابط المتجر؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=42,
                )
                leftover = consume_authorized_cta()
                msgs = _simulate_webhook_split(MODEL_BODY)
                return leftover, msgs

            leftover, msgs = asyncio.run(_run())
        assert leftover is None or not str(getattr(leftover, "url", "") or "")
        assert all(item.cta is None for item in msgs)
