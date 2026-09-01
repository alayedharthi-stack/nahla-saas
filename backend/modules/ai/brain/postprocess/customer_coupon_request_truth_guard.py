"""Operational truth guard for customer-request coupon compose.

If a coupon was allocated/reused, the generic LLM path must not deny it.
If deterministic truth says none was issued, the model must not fabricate a code.

Does not inject canned customer-facing Arabic. Strips false operational claims.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.postprocess.customer_coupon_request_truth_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")
_COUPON_CODE_RE = re.compile(r"\bNH[A-Z0-9]{3}\b", re.IGNORECASE)

_FALSE_DENIAL_MARKERS = (
    "لا يوجد كوبون",
    "لا يوجد كوبونا",
    "ما عندنا كوبون",
    "ما عندنا خصم",
    "مافي كوبون",
    "ما في كوبون",
    "ما فيه كوبون",
    "ما عندي كوبون",
    "ما نقدر نعطيك كوبون",
    "لا يوجد خصم",
    "ما عندنا كود",
    "لا يتوفر كوبون",
    "ما يتوفر كوبون",
)


@dataclass(frozen=True)
class CustomerCouponRequestTruthGuardResult:
    reply: str
    changed: bool
    false_denial_blocked: bool
    fabricated_code_blocked: bool


def _norm(text: str) -> str:
    return _NORMALISE_AR_RE.sub("", str(text or "")).strip()


def _issued_code(facts: Optional[Dict[str, Any]]) -> str:
    if not isinstance(facts, dict):
        return ""
    if not bool(facts.get("issued")):
        return ""
    return str(facts.get("coupon_code") or "").strip()


def apply_customer_coupon_request_truth_guard(
    reply: str,
    *,
    customer_request_coupon_facts: Optional[Dict[str, Any]] = None,
) -> CustomerCouponRequestTruthGuardResult:
    facts = dict(customer_request_coupon_facts or {})
    text = str(reply or "")
    if not facts:
        return CustomerCouponRequestTruthGuardResult(
            reply=text,
            changed=False,
            false_denial_blocked=False,
            fabricated_code_blocked=False,
        )

    issued = bool(facts.get("issued"))
    authorized_code = _issued_code(facts)
    denial_blocked = False
    fabricated_blocked = False
    out = text

    if issued:
        lowered = _norm(out)
        for marker in _FALSE_DENIAL_MARKERS:
            if marker in lowered or marker in out:
                denial_blocked = True
                break
        if denial_blocked:
            for marker in _FALSE_DENIAL_MARKERS:
                out = out.replace(marker, "")
            out = re.sub(r"\n{3,}", "\n\n", out).strip()

    found_codes = {m.group(0).upper() for m in _COUPON_CODE_RE.finditer(out)}
    if issued and authorized_code:
        allowed = authorized_code.upper()
        for code in found_codes:
            if code != allowed:
                fabricated_blocked = True
                out = re.sub(re.escape(code), "", out, flags=re.IGNORECASE)
    elif not issued:
        if found_codes:
            fabricated_blocked = True
            out = _COUPON_CODE_RE.sub("", out)

    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    changed = out != text
    if changed:
        logger.info(
            "[CUSTOMER_COUPON_REQUEST_TRUTH] denial=%s fabricated=%s issued=%s",
            denial_blocked,
            fabricated_blocked,
            issued,
        )
    return CustomerCouponRequestTruthGuardResult(
        reply=out,
        changed=changed,
        false_denial_blocked=denial_blocked,
        fabricated_code_blocked=fabricated_blocked,
    )


__all__ = [
    "CustomerCouponRequestTruthGuardResult",
    "apply_customer_coupon_request_truth_guard",
]
