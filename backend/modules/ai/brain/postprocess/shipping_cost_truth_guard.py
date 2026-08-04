"""
shipping_cost_truth_guard.py
────────────────────────────
Post-compose guard — block invented shipping fees in brain replies.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.checkout_shipping_policy import (
    CheckoutShippingResolution,
    reply_mentions_shipping_fee,
    resolve_verified_shipping_fee,
)

logger = logging.getLogger("nahla.brain.postprocess.shipping_cost_truth_guard")

_HONEST_UNKNOWN_AR = (
    "بخصوص الشحن: ما عندي سياسة شحن واضحة لهذا الطلب الآن. "
    "بأكد لك تكلفة التوصيل قبل إتمام الطلب."
)
_HONEST_FREE_AR = "الشحن مجاني لهذا الطلب حسب سياسة المتجر."
_HONEST_PAID_AR = "تكلفة الشحن: {fee:.0f} ريال حسب سياسة المتجر."

_TOTAL_WITH_SHIPPING_RE = re.compile(
    r"(?:المجموع|الإجمالي|اجمالي).{0,40}?(\d+(?:\.\d+)?)\s*ريال",
    re.I | re.UNICODE,
)
_SHIPPING_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:•\s*)?(?:الشحن|شحن(?:\s*التوصيل)?)\s*[:\-–—]?\s*[^\n]+",
    re.I | re.UNICODE,
)
_SHIPPING_PAREN_RE = re.compile(
    r"\([^)]*(?:شحن|توصيل)[^)]*\)",
    re.I | re.UNICODE,
)
_INLINE_SHIPPING_FEE_RE = re.compile(
    r"شحن\s+توصيل\s+\d+(?:\.\d+)?\s*(?:ريال|r\.?s\.?|sar)",
    re.I | re.UNICODE,
)
_TOTAL_WITH_INVENTED_SHIPPING_RE = re.compile(
    r"((?:المجموع|الإجمالي|اجمالي)\s+)(\d+(?:\.\d+)?)(\s*ريال)\s*"
    r"\([^)]*(?:شحن|توصيل)[^)]*?(\d+(?:\.\d+)?)[^)]*\)",
    re.I | re.UNICODE,
)
_FEE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ريال|r\.?s\.?|sar)", re.I | re.UNICODE)


@dataclass(frozen=True)
class ShippingCostTruthGuardResult:
    reply: str
    replaced: bool = False
    reason: str = ""


def _strip_invented_shipping_lines(reply: str) -> str:
    text = str(reply or "")
    cleaned = _SHIPPING_LINE_RE.sub("", text)
    cleaned = _SHIPPING_PAREN_RE.sub("", cleaned)
    cleaned = _INLINE_SHIPPING_FEE_RE.sub("", cleaned)

    def _fix_total(match: re.Match[str]) -> str:
        prefix, total_raw, suffix, shipping_raw = match.groups()
        try:
            total_val = float(total_raw)
            shipping_val = float(shipping_raw)
        except (TypeError, ValueError):
            return match.group(0)
        adjusted = total_val - shipping_val
        if adjusted > 0:
            return f"{prefix}{adjusted:.2f}{suffix}".rstrip("0").rstrip(".")
        return f"{prefix}{total_raw}{suffix}"

    cleaned = _TOTAL_WITH_INVENTED_SHIPPING_RE.sub(_fix_total, cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _reply_has_unverified_shipping_amount(
    reply: str,
    *,
    verified_fee: Optional[float],
) -> bool:
    text = str(reply or "")
    if not reply_mentions_shipping_fee(text):
        return False
    if verified_fee is None:
        return True
    if verified_fee <= 0:
        return bool(re.search(r"\b29\b", text)) or any(
            float(m.group(1)) > 0
            for m in _FEE_RE.finditer(text)
            if any(tok in text[max(0, m.start() - 30): m.end() + 10] for tok in ("شحن", "توصيل"))
        )
    for match in _FEE_RE.finditer(text):
        window = text[max(0, match.start() - 30): match.end() + 10]
        if not any(tok in window for tok in ("شحن", "توصيل", "الشحن")):
            continue
        try:
            val = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if abs(val - verified_fee) > 0.01 and val > 0:
            return True
    return False


def _append_honest_shipping(reply: str, resolution: CheckoutShippingResolution) -> str:
    base = _strip_invented_shipping_lines(reply)
    if resolution.merchant_review_required:
        suffix = _HONEST_UNKNOWN_AR
    elif resolution.free_shipping:
        suffix = _HONEST_FREE_AR
    elif resolution.shipping_fee_sar is not None:
        suffix = _HONEST_PAID_AR.format(fee=resolution.shipping_fee_sar)
    else:
        suffix = _HONEST_UNKNOWN_AR
    if not base:
        return suffix
    return f"{base}\n\n{suffix}".strip()


def apply_shipping_cost_truth_guard(
    reply: str,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[int] = None,
    message: str = "",
) -> ShippingCostTruthGuardResult:
    original = str(reply or "")
    if not original.strip():
        return ShippingCostTruthGuardResult(reply=original, replaced=False)

    if not reply_mentions_shipping_fee(original):
        return ShippingCostTruthGuardResult(reply=original, replaced=False)

    prep = dict(order_prep or {})
    verified_fee, resolution = resolve_verified_shipping_fee(
        db,
        tenant_id=int(tenant_id or 0),
        order_prep=prep,
        brain_state=brain_state,
        message=str(message or ""),
    )

    if _reply_has_unverified_shipping_amount(original, verified_fee=verified_fee):
        reason = "unknown_shipping_policy"
        if resolution.free_shipping:
            reason = "free_shipping_violation"
        elif verified_fee is not None and verified_fee > 0:
            reason = "shipping_fee_mismatch"
        elif resolution.merchant_review_required:
            reason = "merchant_review_required"
        logger.info(
            "[SHIPPING_COST_TRUTH_GUARD] replaced_%s tenant=%s conversation=%s",
            reason,
            tenant_id,
            conversation_id,
        )
        return ShippingCostTruthGuardResult(
            reply=_append_honest_shipping(original, resolution),
            replaced=True,
            reason=reason,
        )

    return ShippingCostTruthGuardResult(reply=original, replaced=False)


__all__ = [
    "ShippingCostTruthGuardResult",
    "apply_shipping_cost_truth_guard",
]
