"""Shared conditional-coupon claim classification for persona and general-LLM guards."""
from __future__ import annotations

from typing import Any

COUPON_ISSUANCE_MARKERS = (
    "تم إصدار",
    "صدر الكوبون",
    "أصدرنا الكوبون",
    "coupon issued",
    "issued your coupon",
)

COUPON_APPLIED_MARKERS = (
    "تم تطبيق",
    "طبقنا",
    "فعلنا الكوبون",
    "applied the coupon",
    "coupon applied",
)

COUPON_CODE_DISCLOSURE_MARKERS = (
    "كود الخصم",
    "كود خصم",
    "الكود",
    "كوبون ",
    "coupon code",
    "discount code",
    "promo code",
)

FINAL_COUPON_MARKERS = (
    "الكوبون جاهز",
    "كوبونك جاهز",
    "your coupon is ready",
    "coupon is active",
)

MIN_ORDERS_SATISFIED_MARKERS = (
    "أنت مؤهل",
    "انت مؤهل",
    "مستوفي الشروط",
    "استوفيت الشروط",
    "أكملت الطلبات",
    "اكملت الطلبات",
    "definitely eligible",
    "you are eligible",
    "condition satisfied",
)

CHECKOUT_PRESSURE_MARKERS = (
    "نكمل الطلب",
    "اطلب الآن",
    "أرسل العنوان",
    "طريقة الدفع",
    "كم الكمية",
)


def classify_customer_conditional_coupon_claim_violation(
    text: str,
    facts: dict[str, Any],
) -> str:
    """Return a stable failed_reason when text violates conditional-coupon evidence rules."""
    working = str(text or "").strip()
    if not working:
        return ""

    if any(m in working for m in COUPON_ISSUANCE_MARKERS):
        return "coupon_issued_claim"

    if any(m in working for m in COUPON_APPLIED_MARKERS):
        return "coupon_applied_claim"

    lowered = working.lower()
    if any(m.lower() in lowered for m in COUPON_CODE_DISCLOSURE_MARKERS):
        return "coupon_code_disclosure"

    if any(m in working for m in FINAL_COUPON_MARKERS):
        return "final_coupon_claim"

    allow_min_orders_claim = bool(facts.get("allow_min_orders_condition_claim"))
    min_orders_state = str(facts.get("min_orders_condition_state") or "").strip()
    if not allow_min_orders_claim or min_orders_state != "satisfied":
        if any(m in working for m in MIN_ORDERS_SATISFIED_MARKERS):
            return "final_min_orders_eligibility_claim"

    if any(m in working for m in CHECKOUT_PRESSURE_MARKERS):
        return "checkout_pressure"

    return ""


__all__ = [
    "CHECKOUT_PRESSURE_MARKERS",
    "COUPON_APPLIED_MARKERS",
    "COUPON_CODE_DISCLOSURE_MARKERS",
    "COUPON_ISSUANCE_MARKERS",
    "FINAL_COUPON_MARKERS",
    "MIN_ORDERS_SATISFIED_MARKERS",
    "classify_customer_conditional_coupon_claim_violation",
]
