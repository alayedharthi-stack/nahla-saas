"""
intent_priority/compose_hints.py
────────────────────────────────
Goal-bound compose directives derived from intent priority verdicts.
"""
from __future__ import annotations

from typing import Optional

from .types import (
    GOAL_GREETING_ONLY,
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SHIPPING_INQUIRY,
    GOAL_SOCIAL_ONLY,
    GOAL_STAFF_CONTACT,
    IntentPriorityVerdict,
)


def intent_priority_compose_directive(
    verdict: Optional[IntentPriorityVerdict],
) -> str:
    """
    Short behavioral directive for response_goal / prompt overlay.

    Returns empty string when no actionable priority signal exists.
    """
    if verdict is None:
        return ""
    if not verdict.recommended_focus:
        return ""

    lines = [
        f"intent_priority — primary_goal={verdict.primary_customer_goal}.",
        verdict.recommended_focus,
    ]

    if verdict.secondary_elements:
        lines.append(
            "secondary_elements="
            + ",".join(verdict.secondary_elements)
            + " (افتتاحية فقط — ليست موضوع الرد)."
        )
        if verdict.has_secondary_social and verdict.has_commercial_primary:
            lines.append(
                "ممنوع جعل عبارة المجاملة/التحية موضوع الرد أو إعادة صياغتها "
                "كعنوان. لا تكرري عبارات العميل الاجتماعية."
            )

    if verdict.requires_clarification:
        lines.append(
            f"requires_clarification=true reason={verdict.clarification_reason}. "
            "التوضيح يجب أن يكون مربوطاً بهدف العميل — ليس سؤالاً عاماً "
            "عن النوع أو الصفة بلا سياق."
        )

    return " ".join(lines)


def contextual_clarify_priority_hint(
    verdict: Optional[IntentPriorityVerdict],
    *,
    ambiguity_class: str = "",
) -> str:
    """
    Extra goal-bound hint for contextual_clarify compose turns.
    """
    if verdict is None:
        return ""

    if verdict.primary_customer_goal == GOAL_PRICE_INQUIRY:
        if verdict.clarification_reason == "image_product_uncertain":
            return (
                "اسألي عن نوع المنتج المقصود لإعطاء سعر الكيلو/الوحدة الصحيح "
                "— اعترفي بعدم القدرة على تحديد المنتج من الصورة بدقة."
            )
        return (
            "اسألي عن المنتج المقصود لإعطاء السعر الصحيح — "
            "ليس سؤالاً عاماً عن الصفة أو النوع."
        )

    if verdict.primary_customer_goal == GOAL_PRODUCT_AVAILABILITY:
        return "اسألي عن المنتج المطلوب للتحقق من توفره."

    if verdict.primary_customer_goal in {
        GOAL_SHIPPING_INQUIRY,
        GOAL_STAFF_CONTACT,
        GOAL_SOCIAL_ONLY,
        GOAL_GREETING_ONLY,
    }:
        return ""

    _cls = str(ambiguity_class or "").strip()
    if _cls == "missing_product_ref":
        return (
            "اسألي عن المنتج المقصود — اربطي السؤال بهدف العميل "
            "(سعر/توفر/كمية) وليس قائمة مواصفات عامة."
        )
    return ""


__all__ = [
    "intent_priority_compose_directive",
    "contextual_clarify_priority_hint",
]
