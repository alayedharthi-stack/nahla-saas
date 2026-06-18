"""Regression tests — generic stub replies must not fire outside their lane."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_short_honey_order_clarify_reply,
    is_short_honey_order_request,
)
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_ORDER_REQUEST, GOAL_SOCIAL_ONLY  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    _FALLBACK_SOCIAL_AR,
    apply_commerce_reply_quality_guard,
    select_arabic_commerce_fallback,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from services.fallback_policy import (  # noqa: E402
    FALLBACK_REASON_OUTER_EXCEPTION,
    choose_intent_aware_fallback,
)


def _order_state_with_cart() -> SimpleNamespace:
    return SimpleNamespace(
        order_prep=SimpleNamespace(
            cart_items=[{"product_name": "عسل طلح", "variant": "500g", "quantity": 1}],
            line_items=[{"product_name": "عسل طلح", "variant": "500g", "quantity": 1}],
            missing_fields=["customer_first_name"],
            order_status="awaiting_address",
        ),
        awaiting_option_confirmation=False,
        last_question_asked="",
    )


class TestCommerceReplyQualityGuardRegression:
    def test_bare_naim_during_active_order_not_receipt_stub(self) -> None:
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text="نعم",
            intent_name="general",
            primary_customer_goal=GOAL_ORDER_REQUEST,
            state=_order_state_with_cart(),
        )
        assert "وصلت رسالتك" not in out.reply
        assert out.reply != _FALLBACK_SOCIAL_AR

    def test_sticker_social_turn_not_receipt_stub(self) -> None:
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text="",
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
            inbound_metadata={"normalized_type": "sticker"},
        )
        assert out.reply != _FALLBACK_SOCIAL_AR
        assert "وصلت رسالتك" not in out.reply

    def test_empty_social_select_fallback_not_receipt_stub(self) -> None:
        text, kind = select_arabic_commerce_fallback(
            intent_name="social",
            primary_customer_goal=GOAL_SOCIAL_ONLY,
            inbound_text="🌷",
        )
        assert text != _FALLBACK_SOCIAL_AR
        assert kind in {"social_mirror", "social_suppressed"}

    def test_short_honey_order_empty_reply_uses_order_clarify(self) -> None:
        msg = "ابغى ربع كيلو عسل"
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text=msg,
            intent_name="ask_product",
            primary_customer_goal=GOAL_ORDER_REQUEST,
        )
        assert "وصلت رسالتك" not in out.reply
        assert "أبشر" in out.reply
        assert "ربع كيلo" in out.reply or "ربع كيلو" in out.reply
        assert "عسل" in out.reply

    def test_prefixed_honey_order_clarify(self) -> None:
        msg = "اتصلت عليك قبل شوي ابغى ربع كيلو عسل"
        assert is_short_honey_order_request(msg)
        reply = build_short_honey_order_clarify_reply(msg)
        assert reply == "أبشر، تبي ربع كيلو من أي نوع عسل؟"


class TestStaffEscalationGuardRegression:
    def test_false_escalation_during_active_order_not_receipt_stub(self) -> None:
        llm_reply = "تم تحويلك لفريق الدعم، أكمل الطلب الآن"
        result = apply_staff_escalation_truth_guard(
            reply=llm_reply,
            inbound_text="نعم",
            state=_order_state_with_cart(),
            conversation_flags={},
        )
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "وصلت رسالتك" not in result.reply
        assert "تحويل" not in result.reply

    def test_false_escalation_on_honey_order_uses_clarify(self) -> None:
        msg = "ابغى ربع كيلو عسل"
        result = apply_staff_escalation_truth_guard(
            reply="سيتم تحويلك للفريق لمتابعة طلبك",
            inbound_text=msg,
            conversation_flags={},
        )
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "ربع كيلo" in result.reply or "ربع كيلو" in result.reply


class TestFallbackPolicyRegression:
    def test_honey_order_outer_exception_not_neutral_retry(self) -> None:
        msg = "اتصلت عليك قبل شوي ابغى ربع كيلo عسل"
        decision = choose_intent_aware_fallback(
            msg,
            reason=FALLBACK_REASON_OUTER_EXCEPTION,
        )
        assert "حصل خطأ مؤقت" not in decision.text
        assert "ربع كيلo" in decision.text or "ربع كيلو" in decision.text
        assert decision.kind == "intent_deterministic"


class TestCartIntentRegression:
    def test_generic_honey_quarter_kilo_extracts(self) -> None:
        intents = extract_cart_intents("ابغى ربع كيلo عسل")
        assert intents
        assert intents[0]["action"] == "add_item"
        assert intents[0]["product_name"] == "عسل"
        assert intents[0]["variant"] == "250g"
