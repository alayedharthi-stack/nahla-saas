"""
FactGuard — Grounded Commerce Response Checker
───────────────────────────────────────────────
Scans the AI-generated reply for factual commerce claims and verifies each
one against real system data (products, coupons, orders, delivery config).

Strategy:
  1. Detect which claim TYPES are present in the reply text.
  2. For each detected claim type, check the GroundingData.
  3. If a claim is NOT verifiable → replace with a safe neutral phrase.

"Replacement" operates at the claim-type level, not at the sentence level.
When an unverified claim is detected the entire reply is post-fixed with a
grounding override statement. This is conservative by design: safety over
verbosity.

Design decisions:
  - Keyword matching is intentionally liberal (catch more, not less).
  - Arabic and English patterns are checked equally.
  - Unknown claim types are passed through (we only intercept known claim types).
  - The raw_reply and all detected claims are returned for audit logging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .data_fetcher import GroundingData


# ── Safe neutral phrases (bilingual) ─────────────────────────────────────────

_SAFE = {
    "stock_availability": (
        "سأتحقق من توفر المنتج لك. ✅\n"
        "Let me verify the current availability for you. ✅"
    ),
    "low_stock": (
        "سأتأكد من الكمية المتبقية وأعلمك. ✅\n"
        "Let me confirm the remaining stock for you. ✅"
    ),
    "restock": (
        "سأتحقق من موعد إعادة توفر المنتج وأعلمك. 📦\n"
        "I'll check on the restock timeline and get back to you. 📦"
    ),
    "active_discount": (
        "سأتأكد من العروض والتخفيضات المتاحة حالياً. 🏷️\n"
        "Let me confirm the currently active discounts for you. 🏷️"
    ),
    "coupon_validity": (
        "سأتأكد من صلاحية الكوبون وأؤكد لك. 🏷️\n"
        "Let me confirm this coupon is still active for you. 🏷️"
    ),
    "order_status": (
        "سأتحقق من حالة طلبك الآن. 📦\n"
        "I'll check your order status right now. 📦"
    ),
    "same_day_delivery": (
        "سأتحقق من إمكانية التوصيل السريع في منطقتك. 🚚\n"
        "Let me verify same-day delivery availability for your area. 🚚"
    ),
    "delivery_timing": (
        "سأتأكد من مدة التوصيل المتاحة حالياً. 🚚\n"
        "I'll confirm the current delivery timeframe for you. 🚚"
    ),
    "delivery_zones": (
        "سأتأكد من مناطق التوصيل المتاحة. 🗺️\n"
        "Let me verify which delivery zones are currently active. 🗺️"
    ),
    "pickup_availability": (
        "سأتحقق من إمكانية الاستلام من المتجر. 🏪\n"
        "Let me confirm whether in-store pickup is available. 🏪"
    ),
    "payment_link_validity": (
        "سأتحقق من رابط الدفع وأرسله لك. 🔗\n"
        "Let me verify and send you the payment link. 🔗"
    ),
}


# ── Claim detection patterns ──────────────────────────────────────────────────

_PATTERNS = {
    "stock_availability": re.compile(
        r"\b(in stock|available|متاح|موجود|يتوفر|متوفر)\b",
        re.IGNORECASE,
    ),
    "low_stock": re.compile(
        r"\b(low stock|almost out|last (few|units?|items?)|limited (stock|quantity|units?)"
        r"|كمية محدودة|قطعات قليلة|آخر قطعة|كمية قليلة|ينفد قريباً)\b",
        re.IGNORECASE,
    ),
    "restock": re.compile(
        r"\b(restock|back in stock|will be available|coming soon|سيتوفر|قريباً|ستتوفر)\b",
        re.IGNORECASE,
    ),
    "active_discount": re.compile(
        r"\b(on sale|% off|\d+% discount|sale price|discounted|special (price|offer)"
        r"|خصم \d+%|\d+% خصم|سعر مخفض|عرض خاص|تخفيض)\b",
        re.IGNORECASE,
    ),
    "coupon_validity": re.compile(
        # Matches only when a coupon code (uppercase word with digits) is nearby
        r"\b([A-Z]{2,}[0-9]+|[0-9]+[A-Z]{2,})\b.*?\b(valid|active|صالح|فعال|ساري)\b"
        r"|\b(valid|active|صالح|فعال|ساري)\b.*?\b([A-Z]{2,}[0-9]+|[0-9]+[A-Z]{2,})\b",
        re.IGNORECASE | re.DOTALL,
    ),
    "order_status": re.compile(
        r"\b(your order|order status|طلبك|حالة الطلب|تم شحن|has been shipped|on the way|في الطريق)\b",
        re.IGNORECASE,
    ),
    "same_day_delivery": re.compile(
        r"\b(same.?day|today|اليوم|خلال ساعات|في نفس اليوم)\b",
        re.IGNORECASE,
    ),
    "delivery_timing": re.compile(
        r"\b(\d+\s*(day|days|يوم|أيام))\b",
        re.IGNORECASE,
    ),
    "delivery_zones": re.compile(
        r"\b(we deliver to|deliver(y)? (to|in) your|your area|zone|منطقتك|نوصل إلى|نوصل لمنطقة|مناطق التوصيل)\b",
        re.IGNORECASE,
    ),
    "pickup_availability": re.compile(
        r"\b(pick.?up|in.?store pickup|store pickup|الاستلام|استلام من المتجر|استلام من الفرع)\b",
        re.IGNORECASE,
    ),
    "payment_link_validity": re.compile(
        r"\b(payment link|pay here|رابط الدفع|ادفع هنا|رابط للدفع|اضغط للدفع)\b",
        re.IGNORECASE,
    ),
}


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ClaimVerification:
    claim_type: str
    detected: bool
    verified: bool
    safe_phrase_injected: bool
    evidence: Optional[str] = None   # what data was found (or not found)


@dataclass
class VettedReply:
    original_reply: str
    vetted_text: str
    was_modified: bool
    claims: List[ClaimVerification] = field(default_factory=list)


# ── Main checker ──────────────────────────────────────────────────────────────

def vet_reply(
    reply: str,
    grounding: GroundingData,
    mentioned_coupon_codes: Optional[List[str]] = None,
) -> VettedReply:
    """
    Scan the AI reply for factual commerce claims and verify each one.

    Args:
        reply:                  The raw AI-generated reply text.
        grounding:              Pre-fetched verified system data.
        mentioned_coupon_codes: Coupon codes mentioned in the reply (extracted by caller).

    Returns:
        VettedReply with either the original or a safety-amended version.
    """
    if not reply or not reply.strip():
        return VettedReply(original_reply=reply, vetted_text=reply, was_modified=False)

    safe_injections: List[str] = []
    claim_results: List[ClaimVerification] = []

    # ── stock_availability ────────────────────────────────────────────────────
    if _PATTERNS["stock_availability"].search(reply):
        # We consider stock availability verified if there is AT LEAST ONE
        # product explicitly marked in_stock. Otherwise we hedge.
        verified = len(grounding.explicitly_in_stock_ids) > 0
        if not verified:
            safe_injections.append(_SAFE["stock_availability"])
        claim_results.append(ClaimVerification(
            claim_type="stock_availability",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="has_explicit_stock_data" if verified else "no stock metadata in catalog",
        ))

    # ── low_stock ─────────────────────────────────────────────────────────────
    if _PATTERNS["low_stock"].search(reply):
        verified = len(grounding.low_stock_product_ids) > 0
        if not verified:
            safe_injections.append(_SAFE["low_stock"])
        claim_results.append(ClaimVerification(
            claim_type="low_stock",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence=f"low_stock_ids={grounding.low_stock_product_ids}" if verified
                     else "no products with stock_count metadata found",
        ))

    # ── restock ───────────────────────────────────────────────────────────────
    if _PATTERNS["restock"].search(reply):
        verified = grounding.restock_events_exist
        if not verified:
            safe_injections.append(_SAFE["restock"])
        claim_results.append(ClaimVerification(
            claim_type="restock",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="restock_event_in_sync_logs" if verified else "no restock events found",
        ))

    # ── active_discount ───────────────────────────────────────────────────────
    if _PATTERNS["active_discount"].search(reply):
        verified = len(grounding.discounted_product_ids) > 0
        if not verified:
            safe_injections.append(_SAFE["active_discount"])
        claim_results.append(ClaimVerification(
            claim_type="active_discount",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence=f"discounted_ids={grounding.discounted_product_ids}" if verified
                     else "no products with sale_price or discount_pct metadata",
        ))

    # ── coupon_validity ───────────────────────────────────────────────────────
    if _PATTERNS["coupon_validity"].search(reply) or (
        mentioned_coupon_codes and _any_coupon_in_text(reply, mentioned_coupon_codes)
    ):
        # At least one mentioned coupon must be in the verified valid set
        verified = bool(
            mentioned_coupon_codes
            and any(c.upper() in grounding.valid_coupon_codes for c in mentioned_coupon_codes)
        )
        if not verified:
            safe_injections.append(_SAFE["coupon_validity"])
        claim_results.append(ClaimVerification(
            claim_type="coupon_validity",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence=f"valid codes: {grounding.valid_coupon_codes}" if verified else "coupon not found or expired",
        ))

    # ── order_status ──────────────────────────────────────────────────────────
    if _PATTERNS["order_status"].search(reply):
        verified = grounding.customer_last_order_status is not None
        if not verified:
            safe_injections.append(_SAFE["order_status"])
        claim_results.append(ClaimVerification(
            claim_type="order_status",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence=f"last_status={grounding.customer_last_order_status}" if verified else "no orders found for customer",
        ))

    # ── same_day_delivery ─────────────────────────────────────────────────────
    if _PATTERNS["same_day_delivery"].search(reply):
        verified = grounding.same_day_delivery_enabled
        if not verified:
            safe_injections.append(_SAFE["same_day_delivery"])
        claim_results.append(ClaimVerification(
            claim_type="same_day_delivery",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="same_day_enabled" if verified else "same_day_delivery_enabled=False on tenant",
        ))

    # ── delivery_timing ───────────────────────────────────────────────────────
    if _PATTERNS["delivery_timing"].search(reply):
        verified = grounding.has_configured_shipping
        if not verified:
            safe_injections.append(_SAFE["delivery_timing"])
        claim_results.append(ClaimVerification(
            claim_type="delivery_timing",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="shipping_fees_configured" if verified else "no shipping fees configured",
        ))

    # ── delivery_zones ────────────────────────────────────────────────────────
    if _PATTERNS["delivery_zones"].search(reply):
        verified = grounding.has_delivery_zones
        if not verified:
            safe_injections.append(_SAFE["delivery_zones"])
        claim_results.append(ClaimVerification(
            claim_type="delivery_zones",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="delivery_zones_configured" if verified else "no delivery zones in database",
        ))

    # ── pickup_availability ───────────────────────────────────────────────────
    if _PATTERNS["pickup_availability"].search(reply):
        verified = grounding.pickup_enabled
        if not verified:
            safe_injections.append(_SAFE["pickup_availability"])
        claim_results.append(ClaimVerification(
            claim_type="pickup_availability",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="pickup_enabled=True on tenant" if verified else "pickup_enabled=False on tenant",
        ))

    # ── payment_link_validity ─────────────────────────────────────────────────
    if _PATTERNS["payment_link_validity"].search(reply):
        verified = grounding.customer_has_pending_payment_link
        if not verified:
            safe_injections.append(_SAFE["payment_link_validity"])
        claim_results.append(ClaimVerification(
            claim_type="payment_link_validity",
            detected=True,
            verified=verified,
            safe_phrase_injected=not verified,
            evidence="order_with_checkout_url found" if verified
                     else "no orders with checkout_url for this customer",
        ))

    # ── Compose output ────────────────────────────────────────────────────────
    was_modified = bool(safe_injections)

    if was_modified:
        # Deduplicate injections (in case multiple claim types produce the same phrase)
        unique_injections = list(dict.fromkeys(safe_injections))
        vetted_text = reply.rstrip() + "\n\n" + "\n".join(unique_injections)
    else:
        vetted_text = reply

    return VettedReply(
        original_reply=reply,
        vetted_text=vetted_text,
        was_modified=was_modified,
        claims=claim_results,
    )


def extract_coupon_codes_from_text(text: str) -> List[str]:
    """
    Extract likely coupon code patterns from text.
    Coupon codes are typically uppercase alphanumeric, 4-20 chars.
    """
    pattern = re.compile(r"\b([A-Z]{2,}[0-9]{1,}|[0-9]{1,}[A-Z]{2,}|[A-Z]{4,20})\b")
    return pattern.findall(text)


def _any_coupon_in_text(text: str, codes: List[str]) -> bool:
    text_upper = text.upper()
    return any(code.upper() in text_upper for code in codes)
