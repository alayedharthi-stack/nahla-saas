"""
Post-compose guard: reject invented Salla storefront payment/carrier claims.

When Pack B MERCHANT_ENABLED status is known, outbound must not invent
payment methods or carriers outside the evidenced set. Guards may scrub
false claims; they must not inject canned conversational prose as the
primary reply.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.postprocess.merchant_capability_truth_guard")

_KNOWN = {"known", "empty"}

# Phrase → canonical code/name probes (detection only).
_PAYMENT_PROBES: List[tuple[str, re.Pattern[str]]] = [
    ("cod", re.compile(r"عند\s*الاستلام|\bcod\b|cash\s*on\s*delivery", re.I)),
    ("mada", re.compile(r"\bmada\b|مدى", re.I)),
    ("apple_pay", re.compile(r"apple\s*pay|آبل\s*باي|ابل\s*باي", re.I)),
    ("tabby", re.compile(r"\btabby\b|تابي", re.I)),
    ("tamara", re.compile(r"\btamara\b|تمارا", re.I)),
    ("stc_pay", re.compile(r"stc\s*pay|إس\s*تي\s*سي", re.I)),
    ("mobily_pay", re.compile(r"موبايلي\s*باي|mobily\s*pay", re.I)),
    ("barq", re.compile(r"\bبرق\b|\bbarq\b", re.I)),
    ("alrajhi", re.compile(r"الراجحي|\bralrajhi\b", re.I)),
    ("alahli", re.compile(r"الأهلي|الاهلي|\balahli\b", re.I)),
    ("bank", re.compile(r"تحويل\s*بنكي|حساب\s*بنكي|\bbank\b", re.I)),
    ("visa", re.compile(r"\bvisa\b|فيزا", re.I)),
    ("mastercard", re.compile(r"mastercard|ماستركارد", re.I)),
]

_CARRIER_PROBES: List[tuple[str, re.Pattern[str]]] = [
    ("smsa", re.compile(r"\bsmsa\b|سمسا", re.I)),
    ("aramex", re.compile(r"\baramex\b|ارامكس|أرامكس|اراميكس", re.I)),
    ("dhl", re.compile(r"\bdhl\b", re.I)),
    ("spl", re.compile(r"\bspl\b|سبل|البريد\s*السعودي", re.I)),
]


@dataclass
class MerchantCapabilityTruthGuardResult:
    text: str
    scrubbed: bool = False
    reasons: List[str] = field(default_factory=list)
    invented_payment_methods: List[str] = field(default_factory=list)
    invented_carriers: List[str] = field(default_factory=list)


def _norm_codes(values: Sequence[Any]) -> set[str]:
    out: set[str] = set()
    for v in values or []:
        if isinstance(v, dict):
            code = str(v.get("code") or v.get("name") or "").strip().lower()
        else:
            code = str(v or "").strip().lower()
        if code:
            out.add(code)
            if code in {"cash_on_delivery", "cash-on-delivery"}:
                out.add("cod")
            if code == "bank_transfer":
                out.add("bank")
    return out


def _extract_answer(known_facts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    facts = dict(known_facts or {})
    answer = facts.get("merchant_capability_answer")
    if isinstance(answer, dict) and answer:
        return answer
    caps = facts.get("merchant_capabilities")
    if not isinstance(caps, dict):
        caps = {}
    payments = caps.get("payments") if isinstance(caps.get("payments"), dict) else {}
    shipping = caps.get("shipping") if isinstance(caps.get("shipping"), dict) else {}
    methods = payments.get("methods") or facts.get("payment_methods") or []
    companies = shipping.get("companies") or facts.get("shipping_methods") or []
    return {
        "payments_status": str(
            payments.get("status")
            or facts.get("salla_payments_status")
            or ""
        ).lower(),
        "payment_methods": list(methods),
        "shipping_companies_status": str(
            shipping.get("companies_status")
            or facts.get("salla_shipping_companies_status")
            or ""
        ).lower(),
        "shipping_companies": list(companies),
        "question_kind": str(facts.get("question_kind") or ""),
    }


def apply_merchant_capability_truth_guard(
    text: str,
    *,
    known_facts: Optional[Dict[str, Any]] = None,
    decision_topic: str = "",
) -> MerchantCapabilityTruthGuardResult:
    """Scrub invented methods/carriers when Pack B status is known."""
    original = str(text or "")
    result = MerchantCapabilityTruthGuardResult(text=original)
    if not original.strip():
        return result

    answer = _extract_answer(known_facts)
    topic = str(decision_topic or answer.get("question_kind") or "").strip().lower()
    pay_status = str(answer.get("payments_status") or "").strip().lower()
    ship_status = str(answer.get("shipping_companies_status") or "").strip().lower()
    allowed_pay = _norm_codes(answer.get("payment_methods") or [])
    allowed_ship = _norm_codes(answer.get("shipping_companies") or [])

    invented_pay: List[str] = []
    if pay_status in _KNOWN and topic in {
        "cash_on_delivery",
        "merchant_payment_methods",
        "payment_methods",
        "payment_info",
    }:
        for code, pat in _PAYMENT_PROBES:
            if pat.search(original) and code not in allowed_pay:
                # bank probe is broad; only flag when bank-like codes absent
                if code == "bank" and (
                    "bank" in allowed_pay or "bank_transfer" in allowed_pay
                ):
                    continue
                invented_pay.append(code)

    invented_ship: List[str] = []
    if ship_status in _KNOWN and topic in {
        "shipping",
        "ask_shipping",
        "shipping_companies",
        "shipping_post_order",
    }:
        for name, pat in _CARRIER_PROBES:
            if pat.search(original) and name not in allowed_ship:
                # Also allow exact company names from allowed set (already lower).
                if any(name in a for a in allowed_ship):
                    continue
                invented_ship.append(name)

    result.invented_payment_methods = invented_pay
    result.invented_carriers = invented_ship
    if not invented_pay and not invented_ship:
        return result

    # Soft scrub: remove invented tokens when possible; if COD falsely denied
    # while enabled, replace with honest known-capability line is deferred to
    # recompose — here we only strip invented lists.
    scrubbed = original
    for code, pat in _PAYMENT_PROBES:
        if code in invented_pay:
            scrubbed = pat.sub("", scrubbed)
    for name, pat in _CARRIER_PROBES:
        if name in invented_ship:
            scrubbed = pat.sub("", scrubbed)
    scrubbed = re.sub(r"[،,]\s*[،,]", "،", scrubbed)
    scrubbed = re.sub(r"\s{2,}", " ", scrubbed).strip(" ،,\n")
    if scrubbed != original:
        result.text = scrubbed
        result.scrubbed = True
        result.reasons.append("invented_merchant_capability_claim_scrubbed")
        logger.info(
            "[MERCHANT_CAPABILITY_TRUTH_GUARD] scrubbed invented=%s carriers=%s",
            invented_pay,
            invented_ship,
        )
    return result


__all__ = [
    "MerchantCapabilityTruthGuardResult",
    "apply_merchant_capability_truth_guard",
]
