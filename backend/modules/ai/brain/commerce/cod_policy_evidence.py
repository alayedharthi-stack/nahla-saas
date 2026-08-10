"""
COD / cash-on-delivery capability evidence — platform-wide.

Authority for connected Salla storefront general COD questions:
  Salla MERCHANT_ENABLED (checkout_profile / merchant_capabilities)

Nahla-native tenant payment settings are a SEPARATE surface and must not
fabricate Salla storefront method lists.

Tri-state contract:
  known + cod present  → cash_on_delivery_enabled=True
  known + cod absent   → cash_on_delivery_enabled=False
  unknown/forbidden    → cash_on_delivery_enabled=None (cannot confirm)

Never invent runtime payment method defaults.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.commerce.cod_policy")

# Label map only — never a source of truth / never a default list.
_METHOD_LABELS_AR: Dict[str, str] = {
    "alrajhi": "الراجحي",
    "alahli": "الأهلي",
    "stc_pay": "STC Pay",
    "mobily_pay": "موبايلي باي",
    "barq": "برق",
    "bank_transfer": "تحويل بنكي",
    "bank": "تحويل بنكي",
    "cod": "الدفع عند الاستلام",
    "mahally_customer_wallet": "محفظة العميل محلي",
}

STATUS_KNOWN = "known"
STATUS_EMPTY = "empty"
STATUS_UNKNOWN = "unknown"
STATUS_FORBIDDEN = "forbidden"

_COD_CODES = frozenset({"cod", "cash_on_delivery", "cash-on-delivery"})


@dataclass(frozen=True)
class CodPolicyEvidence:
    """Structured COD capability evidence (not customer prose)."""

    status: str = STATUS_UNKNOWN
    cash_on_delivery_enabled: Optional[bool] = None
    available_methods: List[str] = field(default_factory=list)
    source: str = "unknown"


@dataclass(frozen=True)
class CodPolicyReply:
    """Deprecated deterministic prose helper — keep for legacy tests only."""

    reply_text: str
    evidence: CodPolicyEvidence


def method_label(code: str) -> str:
    key = str(code or "").strip().lower()
    return _METHOD_LABELS_AR.get(key) or str(code or "").strip()


def _method_labels(methods: List[str]) -> str:
    labels = [method_label(m) for m in methods if str(m or "").strip()]
    return "، ".join(labels) if labels else ""


def _codes_from_merchant_capabilities(
    merchant_capabilities: Optional[Dict[str, Any]],
) -> tuple[str, List[str]]:
    """Return (payments_status, enabled_codes) from Pack B projection."""
    caps = dict(merchant_capabilities or {})
    payments = caps.get("payments")
    if not isinstance(payments, dict):
        return STATUS_UNKNOWN, []
    status = str(payments.get("status") or "").strip().lower() or STATUS_UNKNOWN
    methods = payments.get("methods") or []
    codes: List[str] = []
    if isinstance(methods, list):
        for item in methods:
            if isinstance(item, dict):
                code = str(item.get("code") or "").strip().lower()
                if code and item.get("enabled", True) is not False:
                    codes.append(code)
            elif isinstance(item, str) and item.strip():
                codes.append(item.strip().lower())
    return status, codes


def _codes_from_flat_methods(raw: Any) -> List[str]:
    if isinstance(raw, list):
        out: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                code = str(item.get("code") or "").strip().lower()
                if code and item.get("enabled", True) is not False:
                    out.append(code)
            elif item is not None and str(item).strip():
                out.append(str(item).strip().lower())
        return out
    if isinstance(raw, dict):
        return [
            str(k).strip().lower()
            for k, enabled in raw.items()
            if enabled and str(k).strip()
        ]
    return []


def load_cod_policy_evidence(
    ai_settings: Optional[Dict[str, Any]] = None,
    *,
    merchant_context: Optional[Dict[str, Any]] = None,
    merchant_capabilities: Optional[Dict[str, Any]] = None,
    payment_methods: Optional[List[str]] = None,
    payment_methods_source: str = "",
    salla_payments_status: str = "",
) -> CodPolicyEvidence:
    """Load COD capability evidence with Pack B ownership for Salla storefront.

    Precedence for connected Salla storefront questions:
      1) merchant_capabilities.payments (MERCHANT_ENABLED)
      2) facts.payment_methods when source=salla_merchant_enabled
      3) otherwise UNKNOWN for Salla-capability questions
         (do NOT invent Nahla-native bank lists as Salla truth)
    """
    ai = dict(ai_settings or {})
    mc = dict(merchant_context or {})
    if not ai and isinstance(mc.get("ai_settings"), dict):
        ai = dict(mc["ai_settings"])

    caps = merchant_capabilities
    if not isinstance(caps, dict) or not caps:
        # Prefer nested known_facts / merchant_context shapes when present.
        facts = mc.get("known_facts") if isinstance(mc.get("known_facts"), dict) else {}
        caps = (
            caps
            or facts.get("merchant_capabilities")
            or mc.get("merchant_capabilities")
            or {}
        )
    if not isinstance(caps, dict):
        caps = {}

    pay_status, cap_codes = _codes_from_merchant_capabilities(caps)
    source_hint = str(payment_methods_source or "").strip().lower()
    flat_status = str(salla_payments_status or "").strip().lower()
    flat_codes = _codes_from_flat_methods(payment_methods)

    # Pack B structured block wins when present.
    if pay_status in (STATUS_KNOWN, STATUS_EMPTY, STATUS_FORBIDDEN):
        if pay_status == STATUS_FORBIDDEN:
            return CodPolicyEvidence(
                status=STATUS_FORBIDDEN,
                cash_on_delivery_enabled=None,
                available_methods=[],
                source="salla_merchant_enabled",
            )
        if pay_status == STATUS_EMPTY:
            return CodPolicyEvidence(
                status=STATUS_EMPTY,
                cash_on_delivery_enabled=False,
                available_methods=[],
                source="salla_merchant_enabled",
            )
        cod_on = any(c in _COD_CODES for c in cap_codes)
        return CodPolicyEvidence(
            status=STATUS_KNOWN,
            cash_on_delivery_enabled=cod_on,
            available_methods=list(cap_codes),
            source="salla_merchant_enabled",
        )

    # Flat Pack B payment_methods with explicit source / status.
    if source_hint == "salla_merchant_enabled" or flat_status in (
        STATUS_KNOWN,
        STATUS_EMPTY,
    ):
        status = flat_status or (STATUS_KNOWN if flat_codes else STATUS_EMPTY)
        if status == STATUS_EMPTY or not flat_codes:
            return CodPolicyEvidence(
                status=STATUS_EMPTY if status != STATUS_UNKNOWN else STATUS_UNKNOWN,
                cash_on_delivery_enabled=False if status == STATUS_EMPTY else None,
                available_methods=[],
                source="salla_merchant_enabled",
            )
        cod_on = any(c in _COD_CODES for c in flat_codes)
        return CodPolicyEvidence(
            status=STATUS_KNOWN,
            cash_on_delivery_enabled=cod_on,
            available_methods=list(flat_codes),
            source="salla_merchant_enabled",
        )

    # Nahla-native settings remain a separate surface — expose only when
    # explicitly configured. Never fabricate a default bank/wallet list.
    native_methods = _codes_from_flat_methods(
        ai.get("available_payment_methods")
        or ai.get("payment_methods")
        or mc.get("available_payment_methods")
        or []
    )
    cod_enabled = ai.get("cash_on_delivery_enabled")
    if cod_enabled is None:
        cod_enabled = ai.get("cod_enabled")
    if cod_enabled is None:
        cod_enabled = mc.get("cod_enabled")
    if cod_enabled is None:
        allow_flow = ai.get("allow_cod_confirmation_flow")
        if allow_flow is None and isinstance(mc.get("brain_profile"), dict):
            allow_flow = mc["brain_profile"].get("allow_cod_confirmation_flow")
        if allow_flow is not None:
            cod_enabled = bool(allow_flow)

    if cod_enabled is not None or native_methods:
        return CodPolicyEvidence(
            status=STATUS_KNOWN if (cod_enabled is not None or native_methods) else STATUS_UNKNOWN,
            cash_on_delivery_enabled=(
                bool(cod_enabled) if cod_enabled is not None else None
            ),
            available_methods=list(native_methods),
            source="nahla_native",
        )

    return CodPolicyEvidence(
        status=STATUS_UNKNOWN,
        cash_on_delivery_enabled=None,
        available_methods=[],
        source="unknown",
    )


def build_cod_policy_reply(
    evidence: CodPolicyEvidence,
    *,
    continue_order: bool = False,
) -> CodPolicyReply:
    """Legacy helper — MUST NOT invent payment methods.

    Primary customer wording is persona/LLM-owned. This returns only a
    minimal emergency factual line from structured evidence.
    """
    if evidence.status in (STATUS_UNKNOWN, STATUS_FORBIDDEN) or (
        evidence.cash_on_delivery_enabled is None
    ):
        body = "ما أقدر أأكد حالياً توفر الدفع عند الاستلام من إعدادات المتجر."
    elif evidence.cash_on_delivery_enabled:
        body = "نعم، الدفع عند الاستلام متاح."
        if continue_order:
            body += " نكمل بيانات التوصيل الآن؟"
    else:
        # Known absent — do not invent alternative banks/wallets.
        body = "حالياً الدفع عند الاستلام غير متاح في المتجر."
    return CodPolicyReply(reply_text=body, evidence=evidence)


def merchant_capability_facts_for_compose(
    evidence: CodPolicyEvidence,
) -> Dict[str, Any]:
    """Structured compose facts for COD / payment capability questions."""
    return {
        "capability_surface": "salla_merchant_enabled"
        if evidence.source == "salla_merchant_enabled"
        else evidence.source,
        "payments_status": evidence.status,
        "cash_on_delivery_enabled": evidence.cash_on_delivery_enabled,
        "payment_methods": list(evidence.available_methods),
        "cannot_confirm": evidence.cash_on_delivery_enabled is None
        or evidence.status in (STATUS_UNKNOWN, STATUS_FORBIDDEN),
    }


__all__ = [
    "CodPolicyEvidence",
    "CodPolicyReply",
    "STATUS_EMPTY",
    "STATUS_FORBIDDEN",
    "STATUS_KNOWN",
    "STATUS_UNKNOWN",
    "build_cod_policy_reply",
    "load_cod_policy_evidence",
    "merchant_capability_facts_for_compose",
    "method_label",
]
