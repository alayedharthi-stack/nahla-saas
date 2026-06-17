"""Education / teacher-student context must not route to product availability."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.intent.education_context_classifier import (  # noqa: E402
    classify_education_context,
    is_education_non_commerce_context,
)
from modules.ai.brain.intent.non_commerce_classifier import classify_non_commerce  # noqa: E402
from modules.ai.brain.intent.rules import match  # noqa: E402
from modules.ai.brain.intent_priority.analyzer import compute_customer_intent_priority  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
    select_arabic_commerce_fallback,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402


STUDENT_MSG = (
    "السلام عليكم\n"
    "كيفك استاذ\n"
    "فيه تحديد الله يسعدك"
)


class TestEducationContextClassifier:
    @pytest.mark.parametrize(
        "message",
        [
            STUDENT_MSG,
            "كيفك استاذ فيه تحديد",
            "فيه تحديد المنهج؟",
        ],
    )
    def test_teacher_student_messages_detected(self, message: str) -> None:
        assert is_education_non_commerce_context(message) is True

    def test_honey_availability_still_commerce(self) -> None:
        assert is_education_non_commerce_context("فيه عسل طلح؟") is False

    def test_explicit_product_availability_still_commerce(self) -> None:
        assert is_education_non_commerce_context("هل المنتج متوفر؟") is False

    def test_store_offer_still_commerce(self) -> None:
        assert is_education_non_commerce_context("فيه عرض؟") is False


class TestRoutingAndFallback:
    def test_production_student_message_not_product_availability_goal(self) -> None:
        intent = match(STUDENT_MSG)
        assert intent is not None
        verdict = compute_customer_intent_priority(
            message=STUDENT_MSG,
            intent=intent,
            state=MerchantConversationState(),
            profile={},
        )
        assert verdict.primary_customer_goal != GOAL_PRODUCT_AVAILABILITY
        assert intent.slots.get("block_commerce_escalation") is True

    def test_empty_reply_does_not_use_availability_fallback(self) -> None:
        intent = match(STUDENT_MSG)
        verdict = compute_customer_intent_priority(
            message=STUDENT_MSG,
            intent=intent,
            state=MerchantConversationState(),
            profile={},
        )
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text=STUDENT_MSG,
            intent_name=intent.name,
            primary_customer_goal=verdict.primary_customer_goal,
        )
        assert out.reply != "التوفر قيد التحقق."
        assert "مادة" in out.reply or "وعليكم السلام" in out.reply

    def test_stripped_english_does_not_become_availability_for_education(self) -> None:
        intent = match("كيفك استاذ فيه تحديد")
        verdict = compute_customer_intent_priority(
            message="كيفك استاذ فيه تحديد",
            intent=intent,
            state=MerchantConversationState(),
            profile={},
        )
        out = apply_commerce_reply_quality_guard(
            "Let me verify the current availability for you.",
            inbound_text="كيفك استاذ فيه تحديد",
            intent_name=intent.name,
            primary_customer_goal=verdict.primary_customer_goal,
        )
        assert "التوفر قيد التحقق." not in out.reply
        assert "متوفر" not in out.reply

    def test_honey_availability_fallback_still_works(self) -> None:
        msg = "هل عندكم عسل طلح؟"
        fb, kind = select_arabic_commerce_fallback(
            intent_name="ask_product",
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            inbound_text=msg,
        )
        assert fb == "التوفر قيد التحقق."
        assert kind == "availability"

    def test_non_commerce_classifier_blocks_commerce_for_education(self) -> None:
        nc = classify_non_commerce(STUDENT_MSG)
        assert nc is not None
        assert nc.block_commerce is True
        assert classify_education_context(STUDENT_MSG) is not None
