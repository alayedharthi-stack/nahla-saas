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
    contains_banned_template_opener,
    is_generic_placeholder_product_name,
    line_items_contain_only_generic_placeholders,
    looks_like_invented_payment_credential,
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


@pytest.mark.constitution_target
class TestSocialPersonaNotCheckout:
    """Phase A — social/phatic bypass over stale checkout (target behavior)."""

    @pytest.mark.parametrize(
        "message",
        [
            "كيف الحال",
            "انت وش اخبارك؟",
            "شكراً",
            "الله يعطيك العافية",
        ],
    )
    @pytest.mark.xfail(
        reason="Phase A pending: social/phatic not in explicit checkout suppression",
        strict=False,
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
        assert "explicit_intent_suppressed" in (result.reason or "") or not result.handled

    @pytest.mark.xfail(
        reason="Phase A pending: social intent detection for suppression",
        strict=False,
    )
    def test_social_intent_detected_for_suppression(self) -> None:
        for message in ("كيف الحال", "انت وش اخبارك؟", "شكراً"):
            intent = detect_explicit_non_checkout_intent(message)
            assert intent in {"social_greeting", "social_thanks", "social_dua", "social"}, (
                message
            )

    @pytest.mark.xfail(
        reason="Phase A pending: question mark after social should not finalize order",
        strict=False,
    )
    def test_question_mark_after_social_not_order_finalization(self) -> None:
        prep = {"order_flow_v2_active": True, "line_items": [dict(_GENERIC_GROUNDED_ITEM)]}
        result = _run_v2("؟", prep=prep)
        assert not result.handled or "order" not in (result.reason or "").lower()


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
        ["منتج", "product", "item", "شيء", "غير محدد", "المطلوب"],
    )
    def test_placeholder_names_detected(self, name: str) -> None:
        assert is_generic_placeholder_product_name(name)

    def test_grounded_name_not_placeholder(self) -> None:
        assert not is_generic_placeholder_product_name("حذاء رياضي أبيض")

    def test_only_generic_line_items_flagged(self) -> None:
        items = [{"product_name": "منتج", "quantity": 1}, {"product_name": "منتج"}]
        assert line_items_contain_only_generic_placeholders(items)

    @pytest.mark.constitution_target
    @pytest.mark.xfail(
        reason="Phase B pending: sync_nahla_wa_order must reject all-generic line items",
        strict=False,
    )
    def test_order_sync_rejects_generic_only_cart(self) -> None:
        from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

        # Target: calling sync with only placeholder names raises or returns blocked.
        items = [{"product_name": "منتج", "quantity": 2}]
        assert line_items_contain_only_generic_placeholders(items)
        pytest.fail("Phase B guard not wired — sync must block generic-only carts")


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
        reason="Phase A pending: كيف الحال needs social intent classification",
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
