"""Merchant-capability FAQ detectors — routing only, no truth invention."""
from __future__ import annotations

import re
from typing import Optional

from modules.ai.brain.types import (
    INTENT_ASK_COD,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_SHIPPING,
)

# General storefront payment-method list questions (not bank/IBAN/barcode).
_PAYMENT_METHODS_RE = re.compile(
    r"("
    r"طرق\s*الدفع|"
    r"وسائل\s*الدفع|"
    r"طريقة\s*الدفع|"
    r"خيارات\s*الدفع|"
    r"كيف\s*(?:أقدر\s*)?(?:أ?دفع|الدفع)|"
    r"وش\s*(?:طرق|وسائل)\s*الدفع|"
    r"ما\s*(?:هي\s*)?(?:طرق|وسائل)\s*الدفع|"
    r"إذا\s*(?:بي?)?طلب.{0,24}(?:أ?ستخدم|أ?دفع)|"
    r"وش\s*أقدر\s*(?:أ?ستخدم|أ?دفع)|"
    r"طريقة\s*(?:أ?دفع|الدفع)|"
    r"payment\s*methods?|"
    r"how\s*(?:can|do)\s*i\s*pay|"
    r"ways?\s*to\s*pay"
    r")",
    re.IGNORECASE,
)

# Carrier-list questions distinct from fee/duration.
_SHIPPING_COMPANIES_RE = re.compile(
    r"("
    r"شركات\s*(?:الشحن|التوصيل)|"
    r"شركة\s*(?:الشحن|التوصيل)|"
    r"ناقل(?:ين)?\s*(?:الشحن|التوصيل)|"
    r"مع\s*أي\s*شركة\s*(?:تشحن|توصل|التوصيل|الشحن)|"
    r"بأي\s*شركة\s*(?:تشحن|توصل)|"
    r"أي\s*شركة\s*(?:توصل|تشحن)|"
    r"مين\s*(?:اللي\s*)?(?:يشحن|يوصل|ماسك)|"
    r"shipping\s*compan(?:y|ies)|"
    r"which\s*carrier|"
    r"couriers?"
    r")",
    re.IGNORECASE,
)

_CAPABILITY_INTENTS = frozenset(
    {
        INTENT_ASK_COD,
        INTENT_ASK_PAYMENT_INFO,
        INTENT_ASK_SHIPPING,
    }
)

_CAPABILITY_DECISION_TOPICS = frozenset(
    {
        "cash_on_delivery",
        "merchant_payment_methods",
        "payment_methods",
        "shipping_companies",
        "shipping_inquiry",
    }
)


def is_merchant_payment_methods_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    return bool(_PAYMENT_METHODS_RE.search(text))


def is_merchant_shipping_companies_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    return bool(_SHIPPING_COMPANIES_RE.search(text))


def should_yield_catalog_navigator_for_capability(
    *,
    intent_name: str = "",
    message: str = "",
) -> bool:
    """Specific merchant-capability FAQ outranks generic catalog browse."""
    name = str(intent_name or "").strip()
    if name in _CAPABILITY_INTENTS:
        return True
    if is_merchant_payment_methods_question(message):
        return True
    if is_merchant_shipping_companies_question(message):
        return True
    return False


def is_merchant_capability_compose_turn(
    *,
    decision_topic: str = "",
    question_kind: str = "",
    intent_name: str = "",
    message: str = "",
) -> bool:
    """True when compose/guards must treat MERCHANT_CAPABILITIES as factual owner."""
    topic = str(decision_topic or "").strip().lower()
    kind = str(question_kind or "").strip().lower()
    if topic in _CAPABILITY_DECISION_TOPICS or kind in {
        "cash_on_delivery",
        "payment_methods",
        "shipping_companies",
    }:
        return True
    return should_yield_catalog_navigator_for_capability(
        intent_name=intent_name,
        message=message,
    )


def capability_topic_from_turn(
    *,
    intent_name: str = "",
    message: str = "",
    decision_topic: str = "",
) -> Optional[str]:
    topic = str(decision_topic or "").strip()
    if topic in _CAPABILITY_DECISION_TOPICS:
        return topic
    name = str(intent_name or "").strip()
    if name == INTENT_ASK_COD:
        return "cash_on_delivery"
    if is_merchant_payment_methods_question(message):
        return "merchant_payment_methods"
    if is_merchant_shipping_companies_question(message):
        return "shipping_companies"
    if name == INTENT_ASK_PAYMENT_INFO:
        return "payment_info"
    if name == INTENT_ASK_SHIPPING:
        return "shipping_inquiry"
    return None


__all__ = [
    "capability_topic_from_turn",
    "is_merchant_capability_compose_turn",
    "is_merchant_payment_methods_question",
    "is_merchant_shipping_companies_question",
    "should_yield_catalog_navigator_for_capability",
]
