"""Merchant-capability FAQ detectors — routing only, no truth invention."""
from __future__ import annotations

import re

# General storefront payment-method list questions (not bank/IBAN/barcode).
_PAYMENT_METHODS_RE = re.compile(
    r"("
    r"طرق\s*الدفع|"
    r"وسائل\s*الدفع|"
    r"طريقة\s*الدفع|"
    r"خيارات\s*الدفع|"
    r"كيف\s*(?:أ?دفع|الدفع)|"
    r"وش\s*(?:طرق|وسائل)\s*الدفع|"
    r"ما\s*(?:هي\s*)?(?:طرق|وسائل)\s*الدفع|"
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
    r"shipping\s*compan(?:y|ies)|"
    r"which\s*carrier|"
    r"couriers?"
    r")",
    re.IGNORECASE,
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


__all__ = [
    "is_merchant_payment_methods_question",
    "is_merchant_shipping_companies_question",
]
