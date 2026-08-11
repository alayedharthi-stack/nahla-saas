"""Pack A2 final — profile answer ownership vs CatalogNavigator / staff contact."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from modules.ai.brain.commerce.merchant_profile_intents import (
    build_merchant_profile_decision,
    classify_store_profile_topic,
    should_yield_catalog_for_merchant_profile,
)
from modules.ai.brain.commerce.store_inquiry_compose_guard import (
    body_claims_no_store_url,
    reconcile_store_link_body_when_url_found,
)
from modules.ai.brain.compose.templates import (
    MSG_STORE_LINK_NOT_CONFIGURED,
    faq_owner_contact,
    faq_store_info,
)
from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent.rules import match as rules_match
from modules.ai.brain.types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_STORE_INFO,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


def _profile_facts(**kwargs: Any) -> CommerceFacts:
    facts = CommerceFacts(
        store_name=kwargs.get("store_name", "متجر تجريبي عام"),
        has_products=True,
        product_count=12,
        orderable=True,
        store_description=kwargs.get(
            "description",
            "متجر تجريبي عام لمنتجات الملابس والأحذية.",
        ),
        store_url=kwargs.get("domain", "https://demo.example/store-a"),
        store_contact_email=kwargs.get("email", "hello@demo.example"),
        store_contact_phone=kwargs.get("phone", ""),
    )
    setattr(
        facts,
        "merchant_profile_social_links",
        kwargs.get("social", {"instagram": "https://instagram.com/demo_store"}),
    )
    setattr(facts, "merchant_profile_currency", kwargs.get("currency", "SAR"))
    setattr(facts, "merchant_profile_status", kwargs.get("status", "active"))
    return facts


def _ctx(message: str, intent: Intent, facts: CommerceFacts) -> BrainContext:
    return BrainContext(
        tenant_id=1,
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
                "social_links": getattr(facts, "merchant_profile_social_links", {}) or {},
                "currency": getattr(facts, "merchant_profile_currency", ""),
                "status": getattr(facts, "merchant_profile_status", ""),
            }
        },
    )


class TestProfileClassificationExtras:
    def test_about_phrases(self) -> None:
        assert classify_store_profile_topic("حدثني عن المتجر") == "store_about"
        assert classify_store_profile_topic("وش متجركم؟") == "store_about"
        assert classify_store_profile_topic("من أنتم؟") == "store_about"

    def test_social_and_email(self) -> None:
        assert classify_store_profile_topic("وش إيميلكم؟") == "owner_contact"
        assert classify_store_profile_topic("عندكم حسابات تواصل؟") == "owner_contact"


class TestUrlContradictionGuard:
    def test_negative_plus_domain_is_contradiction(self) -> None:
        body = "هذا هو رابط المتجر: لا يوجد رابط متجر متاح حالياً."
        assert body_claims_no_store_url(body) is True
        fixed = reconcile_store_link_body_when_url_found(
            body,
            "https://demo.example/store-a",
        )
        assert "لا يوجد" not in fixed.body
        assert "demo.example/store-a" in fixed.body
        assert MSG_STORE_LINK_NOT_CONFIGURED.split("محفوظ")[0] not in fixed.body or (
            "محفوظ" not in fixed.body
        )

    def test_faq_store_info_known_url_never_negative(self) -> None:
        text = faq_store_info(store_url="https://demo.example/store-a")
        assert "demo.example/store-a" in text
        assert "لا يوجد" not in text
        assert MSG_STORE_LINK_NOT_CONFIGURED not in text


class TestContactFaqGrounding:
    def test_email_and_social_no_phone(self) -> None:
        text = faq_owner_contact(
            contact_phone="",
            contact_email="hello@demo.example",
            social_links={"instagram": "https://instagram.com/demo_store"},
        )
        assert "hello@demo.example" in text
        assert "instagram.com/demo_store" in text
        assert "الجوال:" not in text
        assert "966" not in text

    def test_staff_policy_yields_when_profile_channels_present(self) -> None:
        from core.merchant_profile import ResolvedMerchantProfile
        from modules.ai.brain.commerce.staff_contact_policy import (
            evaluate_staff_contact_policy,
        )

        profile = ResolvedMerchantProfile(
            tenant_id=1,
            email="hello@demo.example",
            social_links={"instagram": "https://instagram.com/demo_store"},
            phone="",
        )
        with patch(
            "core.merchant_profile.resolve_merchant_profile",
            return_value=profile,
        ):
            decision = evaluate_staff_contact_policy(
                MagicMock(),
                tenant_id=1,
                message="كيف أتواصل معكم؟",
                customer_phone="966500000001",
            )
        assert decision is None

    def test_staff_policy_generic_when_no_channels(self) -> None:
        from core.merchant_profile import ResolvedMerchantProfile
        from modules.ai.brain.commerce.entity_extraction_guard import (
            MSG_GENERAL_CONTACT_HOW_TO,
        )
        from modules.ai.brain.commerce.staff_contact_policy import (
            evaluate_staff_contact_policy,
        )

        profile = ResolvedMerchantProfile(tenant_id=1, email="", phone="", social_links={})
        with patch(
            "core.merchant_profile.resolve_merchant_profile",
            return_value=profile,
        ):
            decision = evaluate_staff_contact_policy(
                MagicMock(),
                tenant_id=1,
                message="كيف أتواصل معكم؟",
                customer_phone="966500000001",
            )
        assert decision is not None
        assert decision.reply_text == MSG_GENERAL_CONTACT_HOW_TO


class TestProductionEquivalentProfileRouting:
    """has_products=true + CatalogNavigator enabled must not steal profile turns."""

    def test_about_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "حدثني عن المتجر"
        intent = rules_match(msg) or Intent(
            name=INTENT_ASK_STORE_INFO,
            confidence=0.9,
            slots={},
            raw_message=msg,
        )
        facts = _profile_facts()
        ctx = _ctx(msg, intent, facts)
        ctx._db = object()
        assert should_yield_catalog_for_merchant_profile(
            intent_name=intent.name,
            message=msg,
        )
        assert nav.try_catalog_navigation_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("topic") == "store_about"
        assert "catalog_navigation_top_products_fallback" not in str(
            decision.args.get("chosen_path") or ""
        )

    def test_status_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "هل المتجر نشط؟"
        intent = Intent(
            name=INTENT_ASK_STORE_INFO,
            confidence=0.9,
            slots={},
            raw_message=msg,
        )
        ctx = _ctx(msg, intent, _profile_facts(status="active"))
        ctx._db = object()
        assert nav.try_catalog_navigation_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("question_kind") == "account_status"
        assert "open" not in str(decision.args.get("response_goal") or "").lower() or (
            "open/closed" in str(decision.args.get("response_goal") or "").lower()
        )

    def test_contact_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "كيف أتواصل معكم؟"
        intent = rules_match(msg) or Intent(
            name=INTENT_ASK_OWNER_CONTACT,
            confidence=0.9,
            slots={},
            raw_message=msg,
        )
        ctx = _ctx(msg, intent, _profile_facts())
        ctx._db = object()
        assert nav.try_catalog_navigation_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.args.get("topic") == "owner_contact"

    def test_store_url_not_catalog_top_fallback(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "وش رابط المتجر؟"
        intent = rules_match(msg) or Intent(
            name=INTENT_ASK_STORE_INFO,
            confidence=0.9,
            slots={},
            raw_message=msg,
        )
        ctx = _ctx(msg, intent, _profile_facts())
        ctx._db = object()
        assert nav.try_catalog_navigation_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.args.get("topic") == "store_info"

    def test_genuine_browse_still_catalog(self, monkeypatch: Any) -> None:
        from modules.ai.brain.catalog import navigation as nav

        monkeypatch.setattr(nav, "_load_catalog_groups", lambda ctx: [])
        msg = "وش المنتجات عندكم؟"
        intent = Intent(
            name=INTENT_ASK_PRODUCT,
            confidence=0.9,
            slots={},
            raw_message=msg,
        )
        ctx = _ctx(msg, intent, _profile_facts())
        ctx._db = object()
        assert not should_yield_catalog_for_merchant_profile(
            intent_name=intent.name,
            message=msg,
        )
        decision = nav.try_catalog_navigation_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE


class TestPackBBoundaryPreserved:
    def test_payment_methods_not_stolen_by_profile(self) -> None:
        assert classify_store_profile_topic("وش طرق الدفع عندكم؟") is None
        assert build_merchant_profile_decision(message="وش طرق الدفع عندكم؟") is None

    def test_cod_not_stolen_by_profile(self) -> None:
        assert classify_store_profile_topic("هل عندكم دفع عند الاستلام؟") is None

    def test_shipping_companies_not_stolen_by_profile(self) -> None:
        assert classify_store_profile_topic("وش شركات الشحن عندكم؟") is None


class TestDualTenantFaqIsolation:
    def test_different_descriptions_domains(self) -> None:
        a = faq_store_info(
            store_name="متجر أ",
            store_url="",
            store_description="وصف أ للأحذية الرياضية",
        )
        b = faq_store_info(
            store_name="متجر ب",
            store_url="",
            store_description="وصف ب للعطور",
        )
        assert "وصف أ" in a and "وصف ب" not in a
        assert "وصف ب" in b and "وصف أ" not in b
        ua = faq_store_info(store_url="https://a.example")
        ub = faq_store_info(store_url="https://b.example")
        assert "a.example" in ua and "b.example" not in ua
        assert "b.example" in ub and "a.example" not in ub


class TestNoCtxDbRegression:
    def test_build_decision_no_db_attr(self) -> None:
        facts = _profile_facts()

        class _Ctx:
            message = "حدثني عن المتجر"
            facts = None
            merchant_context = None

        ctx = _Ctx()
        ctx.facts = facts
        assert not hasattr(ctx, "db")
        d = build_merchant_profile_decision(
            message=ctx.message,
            facts=ctx.facts,
            merchant_context={"merchant_profile": {"description": facts.store_description}},
        )
        assert d is not None
        assert d.args["topic"] == "store_about"
