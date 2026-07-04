"""Merchant Assistant Constitution — automated regression suite.

Locks platform policy in docs/architecture/nahla-ai-merchant-assistant-policy.md.
Tests marked constitution_target document required behavior not yet implemented.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CUSTOMER_LEDGER_REPLY,
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: E402
    PAYMENT_BARCODE_IMAGE_REQUEST,
    payment_barcode_intro_text,
)
from modules.ai.checkout_authority import (  # noqa: E402
    LocalDraftEvidence,
    brain_payment_paths_should_defer_to_checkout_owner,
)
from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: E402
    detect_explicit_non_checkout_intent,
    evaluate_stale_checkout_suppression,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from tests.constitution_helpers import (  # noqa: E402
    CONSTITUTION_BANNED_CUSTOMER_OPENERS,
    NON_SAUDI_ARABIC_DIALECT_TERMS,
    assert_no_non_saudi_arabic,
    contains_banned_template_opener,
    find_non_saudi_arabic_terms,
    is_generic_placeholder_product_name,
    line_items_contain_only_generic_placeholders,
    looks_like_invented_payment_credential,
    rejects_checkout_pressure_after_social,
    rejects_social_support_bot_phrase,
    social_replies_are_non_deterministic,
    try_compose_persona_samples,
)

_GENERIC_GROUNDED_ITEM = {
    "product_id": "sku-generic-shoe-001",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "catalog_price": 199.0,
}

_GENERIC_MERCHANT = "متجر تجريبي عام"
_GENERIC_CUSTOMER = "966500000001"


@pytest.fixture(autouse=True)
def _v2_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)


def _draft_evidence(
    *,
    line_items: list | None = None,
    ref: str = "NHL-1-000099",
) -> LocalDraftEvidence:
    return LocalDraftEvidence(
        order_id=88,
        external_id="nahla-wa-1-42",
        external_order_number=ref,
        status="draft",
        line_items=list(line_items or [_GENERIC_GROUNDED_ITEM]),
        total=199.0,
        currency="SAR",
        missing_fields=["customer_name", "city", "delivery_address", "payment_method"],
    )


def _conversation(conv_id: int = 42):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=1,
        extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
    )


def _run_v2(message: str, *, prep: dict | None = None, draft=None, conv_id: int = 42):
    draft = draft if draft is not None else _draft_evidence()
    with patch(
        "modules.ai.order_flow_v2.owner.operational_tuple",
        return_value=(True, False, "test_mode_canary_enforcement"),
    ), patch(
        "modules.ai.order_flow_v2.owner.load_local_draft_evidence",
        return_value=draft,
    ), patch(
        "modules.ai.order_flow_v2.owner._load_brain_state",
        return_value=(_conversation(conv_id), {"order_prep": dict(prep or {})}),
    ):
        return try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone=_GENERIC_CUSTOMER,
            message=message,
        )


# ─── A. Social/persona should not become checkout ───────────────────────────


class TestSocialPersonaNotCheckout:
    """Phase A — social/phatic bypass over stale checkout."""

    @pytest.mark.parametrize(
        "message",
        [
            "كيف الحال",
            "انت وش اخبارك؟",
            "شكراً",
            "الله يعطيك العافية",
        ],
    )
    def test_social_with_active_draft_not_handled_by_v2(self, message: str) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
        }
        result = _run_v2(message, prep=prep)
        assert not result.handled, (
            f"OrderFlowV2 must not own social turn {message!r} with stale draft"
        )
        assert "explicit_intent_suppressed" in (result.reason or "")

    def test_social_intent_detected_for_suppression(self) -> None:
        from modules.ai.order_flow_v2.explicit_intent_checkout_suppression import (  # noqa: PLC0415
            detect_social_phatic_intent,
        )

        assert detect_social_phatic_intent("كيف الحال") == "social_greeting"
        assert detect_social_phatic_intent("انت وش اخبارك؟") == "social_greeting"
        assert detect_social_phatic_intent("شكراً") == "social_thanks"
        assert detect_social_phatic_intent("الله يعطيك العافية") == "social_dua"

    def test_question_mark_after_social_not_order_finalization(self) -> None:
        prep = {"order_flow_v2_active": True, "line_items": [dict(_GENERIC_GROUNDED_ITEM)]}
        result = _run_v2("؟", prep=prep)
        assert not result.handled or "order" not in (result.reason or "").lower()


# ─── A.1 Social context bleed cleanup ───────────────────────────────────────


class TestSocialContextBleedCleanup:
    """Phase A.1 — pure phatic turns must not append checkout slot pressure."""

    @pytest.mark.parametrize(
        "message",
        [
            "شكراً",
            "الله يعطيك العافية",
            "كيف الحال",
            "السلام عليكم",
        ],
    )
    def test_active_draft_phatic_not_handled_by_v2(self, message: str) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
        }
        result = _run_v2(message, prep=prep)
        assert not result.handled, (
            f"OrderFlowV2 must not own phatic turn {message!r} with stale checkout"
        )
        assert "explicit_intent_suppressed" in (result.reason or "")

    def test_should_not_resume_checkout_on_pure_salaam(self) -> None:
        from modules.ai.order_flow_v2.state import should_resume_checkout_on_greeting  # noqa: PLC0415

        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
        }
        assert not should_resume_checkout_on_greeting(
            prep,
            {},
            message="السلام عليكم",
        )

    def test_guard_strips_checkout_pressure_from_mixed_social_reply(self) -> None:
        from modules.ai.brain.postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
            apply_social_checkout_pressure_guard,
        )

        result = apply_social_checkout_pressure_guard(
            "العفو 🌷\nأرسل عنوانك",
            inbound_text="شكراً",
        )
        assert result.stripped
        assert not rejects_checkout_pressure_after_social(result.reply, "شكراً")

    def test_guard_all_pressure_uses_fallback_not_empty(self) -> None:
        from modules.ai.brain.postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
            apply_social_checkout_pressure_guard,
        )

        result = apply_social_checkout_pressure_guard(
            "أرسل عنوانك",
            inbound_text="شكراً",
        )
        assert result.reply.strip()
        assert result.empty_fallback
        assert not rejects_checkout_pressure_after_social(result.reply, "شكراً")

    def test_checkout_continuation_yes_still_owned_with_active_draft(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
            "customer_first_name": "أحمد",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
        }
        result = _run_v2("نعم", prep=prep)
        assert result.handled

    def test_payment_method_answer_still_owned_at_payment_prompt(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
            "customer_first_name": "أحمد",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
            "order_flow_v2_last_field": "payment_method",
        }
        decision = evaluate_stale_checkout_suppression(
            message="تحويل بنكي",
            order_prep=prep,
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is False


# ─── B. Checkout still owns true continuation ─────────────────────────────────


class TestCheckoutContinuationOwned:
    @pytest.mark.parametrize(
        "message",
        ["نعم", "اعتمد نفس العنوان", "تحويل بنكي", "1"],
    )
    def test_checkout_continuation_still_owned(self, message: str) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
            "customer_first_name": "أحمد",
            "customer_last_name": "سالم",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
            "order_flow_v2_last_field": "payment_method",
        }
        if message == "1":
            prep["order_flow_v2_last_field"] = "quantity"
        decision = evaluate_stale_checkout_suppression(
            message=message,
            order_prep=prep,
            missing_fields=["payment_method"] if message != "1" else ["quantity"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is False

    def test_yes_handled_by_v2_with_active_draft(self) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_GROUNDED_ITEM)],
            "customer_first_name": "أحمد",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
        }
        result = _run_v2("نعم", prep=prep)
        assert result.handled


# ─── C. Generic line item guard ─────────────────────────────────────────────


class TestGenericLineItemGuard:
    @pytest.mark.parametrize(
        "name",
        [
            "منتج",
            "product",
            "item",
            "شيء",
            "غير محدد",
            "المطلوب",
            "صنف",
            "سلعة",
        ],
    )
    def test_placeholder_names_detected(self, name: str) -> None:
        assert is_generic_placeholder_product_name(name)

    def test_grounded_name_not_placeholder(self) -> None:
        assert not is_generic_placeholder_product_name("حذاء رياضي أبيض")

    def test_only_generic_line_items_flagged(self) -> None:
        items = [{"product_name": "منتج", "quantity": 1}, {"product_name": "منتج"}]
        assert line_items_contain_only_generic_placeholders(items)

    def test_order_sync_rejects_generic_only_cart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

        monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
        monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", "33")

        db = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        conv = SimpleNamespace(
            id=9063,
            tenant_id=33,
            customer_id=1,
            customer=SimpleNamespace(
                id=1,
                tenant_id=33,
                phone="966500000001",
                name="Customer",
                extra_metadata={},
            ),
            extra_metadata={},
        )

        result = sync_nahla_wa_order(
            db,
            tenant_id=33,
            conversation=conv,
            brain_state={"stage": "ordering"},
            order_prep={
                "line_items": [{"product_name": "منتج", "quantity": 2}],
                "customer_first_name": "أحمد",
                "city": "الرياض",
            },
            trigger="constitution_generic_guard",
        )
        assert result is None


# ─── D. KB questions should not become checkout ─────────────────────────────


class TestKBQuestionsNotCheckout:
    @pytest.mark.parametrize(
        "message,expected_fragment",
        [
            ("كيف الدفع؟", "ask_payment_info"),
            ("وش عندكم منتجات؟", "catalog_browse"),
        ],
    )
    def test_informational_intents_suppress_stale_checkout(
        self, message: str, expected_fragment: str
    ) -> None:
        decision = evaluate_stale_checkout_suppression(
            message=message,
            order_prep={"order_flow_v2_active": True, "line_items": [_GENERIC_GROUNDED_ITEM]},
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert expected_fragment in (decision.detected_intent or "")

    @pytest.mark.constitution_target
    @pytest.mark.parametrize(
        "message,expected_fragment",
        [
            ("وين موقعكم؟", "ask_location"),
            ("كيف الشحن؟", "ask_shipping"),
        ],
    )
    @pytest.mark.xfail(
        reason="Phase D pending: location/shipping KB intents need stale-checkout suppression",
        strict=False,
    )
    def test_kb_location_shipping_suppress_stale_checkout(
        self, message: str, expected_fragment: str
    ) -> None:
        decision = evaluate_stale_checkout_suppression(
            message=message,
            order_prep={"order_flow_v2_active": True, "line_items": [_GENERIC_GROUNDED_ITEM]},
            missing_fields=["payment_method"],
            checkout_active=True,
            draft_active=True,
        )
        assert decision.suppress is True
        assert expected_fragment in (decision.detected_intent or "")

    @pytest.mark.constitution_target
    @pytest.mark.parametrize(
        "message",
        [
            "كيف أحفظ العسل؟",
            "هل العسل أصلي؟",
            "ما الفرق بين السدر والسمر؟",
        ],
    )
    @pytest.mark.xfail(
        reason="Phase D pending: KB FAQ intents + V2 bypass for product-policy questions",
        strict=False,
    )
    def test_kb_product_questions_route_to_brain_not_v2(self, message: str) -> None:
        from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: PLC0415
        from modules.ai.brain.intent import rules  # noqa: PLC0415
        from modules.ai.brain.types import BrainContext, CommerceFacts, MerchantConversationState  # noqa: PLC0415

        intent = rules.match(message)
        assert intent is not None
        state = MerchantConversationState()
        state.order_prep.line_items = [dict(_GENERIC_GROUNDED_ITEM)]
        ctx = BrainContext(
            tenant_id=1,
            customer_phone=_GENERIC_CUSTOMER,
            message=message,
            intent=intent,
            state=state,
            facts=CommerceFacts(store_name=_GENERIC_MERCHANT),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {
            ACTION_FAQ_REPLY,
            ACTION_LLM_REPLY,
            ACTION_TRACK_ORDER,
        }
        result = _run_v2(message)
        assert not result.handled


# ─── E. Product/catalog questions ─────────────────────────────────────────────


class TestCatalogQuestions:
    @pytest.mark.parametrize(
        "message",
        [
            "وش عندكم منتجات؟",
            "أرسل المنتجات",
            "وش الأنواع؟",
        ],
    )
    def test_catalog_browse_bypasses_v2_with_draft(self, message: str) -> None:
        result = _run_v2(message)
        assert not result.handled
        assert "catalog_browse" in (result.reason or "") or "ask_product" in (
            result.reason or ""
        )

    @pytest.mark.constitution_target
    @pytest.mark.xfail(
        reason="Phase E pending: product-specific browse must not claim unavailable SKU",
        strict=False,
    )
    def test_specific_product_question_grounded(self) -> None:
        intent = detect_explicit_non_checkout_intent("عندكم حذاء رياضي أبيض؟")
        assert intent in {"ask_product", "catalog_browse", ""}


# ─── F. Payment/media facts ───────────────────────────────────────────────────


class TestPaymentMediaFacts:
    def test_rajhi_barcode_bypasses_stale_checkout(self) -> None:
        result = _run_v2("أرسل باركود الراجحي")
        assert not result.handled
        assert PAYMENT_BARCODE_IMAGE_REQUEST in (result.reason or "")

    def test_ahli_barcode_detected(self) -> None:
        intent = detect_explicit_non_checkout_intent("باركود الأهلي")
        assert intent in {PAYMENT_BARCODE_IMAGE_REQUEST, "ask_payment_info"}

    def test_payment_defer_false_for_explicit_barcode(self) -> None:
        conv = _conversation()
        with patch(
            "modules.ai.checkout_authority.load_local_draft_evidence",
            return_value=_draft_evidence(),
        ):
            assert not brain_payment_paths_should_defer_to_checkout_owner(
                MagicMock(),
                tenant_id=1,
                conversation=conv,
                message="أرسل باركود الراجحي",
            )

    def test_intro_fallback_does_not_invent_credentials(self) -> None:
        for key in ("payment_rajhi_barcode", "payment_alahli_barcode", ""):
            text = payment_barcode_intro_text(key)
            assert text
            assert not looks_like_invented_payment_credential(text)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="Phase 2 pending: payment intro must use FactBoundPersonaComposer not static template",
        strict=False,
    )
    def test_payment_intro_not_primary_static_template(self) -> None:
        """Target: normal path must not be payment_barcode_intro_text hardcoded."""
        text = payment_barcode_intro_text("payment_rajhi_barcode")
        assert not contains_banned_template_opener(text)


# ─── G. No silence ────────────────────────────────────────────────────────────


class TestNoSilence:
    @pytest.mark.parametrize(
        "message",
        [
            "وين موقعكم؟",
            "وش عندكم منتجات؟",
            "أرسل باركود الراجحي",
            "وين طلبي؟",
        ],
    )
    def test_understandable_messages_have_brain_or_bypass_route(
        self, message: str
    ) -> None:
        from modules.ai.brain.intent import rules  # noqa: PLC0415

        intent = rules.match(message)
        assert intent is not None, f"No intent for understandable message: {message}"
        bypass = detect_explicit_non_checkout_intent(message)
        assert intent.name or bypass, "Must route somewhere — not silent drop"

    @pytest.mark.constitution_target
    @pytest.mark.xfail(
        reason="Phase A pending: كيف الحال needs social intent classification in rules.match",
        strict=False,
    )
    def test_social_how_are_you_has_route(self) -> None:
        from modules.ai.brain.intent import rules  # noqa: PLC0415

        message = "كيف الحال"
        intent = rules.match(message)
        bypass = detect_explicit_non_checkout_intent(message)
        assert intent is not None or bypass


# ─── H. Anti-template policy ────────────────────────────────────────────────


class TestAntiTemplatePolicy:
    def test_banned_openers_documented(self) -> None:
        assert "أكيد 🌷 تفضل" in CONSTITUTION_BANNED_CUSTOMER_OPENERS

    def test_current_payment_intro_uses_banned_opener(self) -> None:
        """Documents current gap — static template is primary path today."""
        text = payment_barcode_intro_text("payment_rajhi_barcode")
        assert contains_banned_template_opener(text)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="Phase 2: persona layer must replace banned opener as primary path",
        strict=False,
    )
    def test_payment_intro_must_not_use_banned_opener_primary(self) -> None:
        text = payment_barcode_intro_text("payment_rajhi_barcode")
        assert not contains_banned_template_opener(text)

    def test_policy_doc_exists(self) -> None:
        from pathlib import Path

        policy = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "architecture"
            / "nahla-ai-merchant-assistant-policy.md"
        )
        assert policy.is_file()
        body = policy.read_text(encoding="utf-8")
        assert "template engine" in body.lower()
        assert "FactBoundPersonaComposer" in body


# ─── I. Saudi dialect policy ──────────────────────────────────────────────────


class TestSaudiDialectPolicyHelpers:
    def test_non_saudi_terms_catalog_matches_policy(self) -> None:
        assert "إزاي" in NON_SAUDI_ARABIC_DIALECT_TERMS
        assert "شو" in NON_SAUDI_ARABIC_DIALECT_TERMS

    def test_find_non_saudi_arabic_detects_egyptian(self) -> None:
        assert "إزاي" in find_non_saudi_arabic_terms("إزاي أطلب؟")
        assert "عايز" in find_non_saudi_arabic_terms("عايز حذاء")

    def test_find_non_saudi_arabic_detects_levantine(self) -> None:
        assert "شو" in find_non_saudi_arabic_terms("شو عندكم؟")
        assert "كيفك" in find_non_saudi_arabic_terms("كيفك اليوم")

    def test_assert_no_non_saudi_passes_saudi_sample(self) -> None:
        assert_no_non_saudi_arabic("أبشر يا غالي، عندنا حذاء رياضي أبيض")
        assert_no_non_saudi_arabic("تمام، وش تحتاج؟")

    def test_english_not_scanned_for_saudi_dialect_terms(self) -> None:
        # English policy is separate — dialect helper is Arabic-surface only.
        assert_no_non_saudi_arabic("Sure, how can I help with your order?")

    def test_policy_doc_has_language_and_dialect_section(self) -> None:
        from pathlib import Path

        policy = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "architecture"
            / "nahla-ai-merchant-assistant-policy.md"
        )
        body = policy.read_text(encoding="utf-8")
        assert "## 11.1 Language and Dialect Policy" in body
        assert "Saudi Arabic" in body
        assert "## 11.2 Social Persona Policy" in body


class TestSaudiDialectPolicyTargets:
    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_arabic_social_output_no_non_saudi_dialect(self) -> None:
        replies = try_compose_persona_samples("social_greeting", "كيف الحال")
        for text in replies:
            assert_no_non_saudi_arabic(text)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_arabic_operational_output_no_non_saudi_dialect(self) -> None:
        replies = try_compose_persona_samples(
            "payment_media_intro",
            "أرسل باركود الراجحي",
        )
        for text in replies:
            assert_no_non_saudi_arabic(text)


# ─── J. Social non-determinism targets ────────────────────────────────────────


class TestSocialNonDeterminismHelpers:
    def test_banned_support_bot_openers_flagged(self) -> None:
        assert rejects_social_support_bot_phrase("كيف أقدر أساعدك اليوم؟")
        assert rejects_social_support_bot_phrase("تم استلام رسالتك، شكراً")

    def test_saudi_social_reply_not_flagged_as_support_bot(self) -> None:
        assert not rejects_social_support_bot_phrase("بخير الله يسعدك، وش تحتاج؟")

    @pytest.mark.parametrize(
        ("inbound", "bad_reply"),
        [
            ("كيف الحال", "بخير، وش طريقة الدفع المناسبة لك؟"),
            ("الله يعطيك العافية", "الله يعافيك، أرسل عنوانك"),
        ],
    )
    def test_checkout_pressure_after_pure_social_detected(
        self, inbound: str, bad_reply: str
    ) -> None:
        assert rejects_checkout_pressure_after_social(bad_reply, inbound)

    def test_non_determinism_helper_requires_variation(self) -> None:
        assert social_replies_are_non_deterministic(
            ["أبشر، بخير", "تمام الحمد لله"]
        )
        assert not social_replies_are_non_deterministic(
            ["بخير الله يسعدك", "بخير الله يسعدك"]
        )

    def test_composer_design_doc_has_language_policy(self) -> None:
        from pathlib import Path

        design = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "architecture"
            / "fact-bound-persona-composer-design.md"
        )
        body = design.read_text(encoding="utf-8")
        assert "language_policy" in body
        assert 'dialect: str = "saudi_arabic"' in body
        assert "Non-determinism requirement" in body


class TestSocialNonDeterminismTargets:
    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_social_checkin_not_always_same_phrase(self) -> None:
        replies = try_compose_persona_samples("social_greeting", "كيف الحال")
        assert social_replies_are_non_deterministic(replies)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_thanks_dua_not_fixed_global_string(self) -> None:
        thanks = try_compose_persona_samples("thanks", "شكراً")
        dua = try_compose_persona_samples("dua", "الله يعطيك العافية")
        assert social_replies_are_non_deterministic(thanks)
        assert social_replies_are_non_deterministic(dua)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_social_output_rejects_banned_support_bot_openers(self) -> None:
        for inbound in ("كيف الحال", "شكراً", "الله يعطيك العافية"):
            for text in try_compose_persona_samples("social_greeting", inbound):
                assert not rejects_social_support_bot_phrase(text)

    @pytest.mark.persona_policy
    @pytest.mark.xfail(
        reason="pending FactBoundPersonaComposer runtime",
        strict=False,
    )
    def test_social_output_rejects_checkout_pressure(self) -> None:
        cases = (
            ("كيف الحال", "social_greeting"),
            ("الله يعطيك العافية", "dua"),
        )
        for inbound, surface in cases:
            for text in try_compose_persona_samples(surface, inbound):
                assert not rejects_checkout_pressure_after_social(text, inbound)
