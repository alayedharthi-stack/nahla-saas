"""
COD / cash-on-delivery policy evidence — platform-wide, settings-driven.

Operational replies must reflect merchant config, not LLM invention.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.commerce.cod_policy")

_METHOD_LABELS_AR: Dict[str, str] = {
    "alrajhi": "الراجحي",
    "alahli": "الأهلي",
    "stc_pay": "STC Pay",
    "mobily_pay": "موبايلي باي",
    "barq": "برق",
    "bank_transfer": "تحويل بنكي",
}


@dataclass(frozen=True)
class CodPolicyEvidence:
    cash_on_delivery_enabled: bool
    available_methods: List[str]
    source: str = "tenant_settings"


@dataclass(frozen=True)
class CodPolicyReply:
    reply_text: str
    evidence: CodPolicyEvidence


def _method_labels(methods: List[str]) -> str:
    labels = []
    for key in methods:
        label = _METHOD_LABELS_AR.get(str(key).strip().lower())
        labels.append(label or str(key).strip())
    return "، ".join(labels) if labels else "الطرق المفعّلة في المتجر"


def load_cod_policy_evidence(
    ai_settings: Optional[Dict[str, Any]] = None,
    *,
    merchant_context: Optional[Dict[str, Any]] = None,
) -> CodPolicyEvidence:
    """Read COD availability from tenant ai_settings / merchant_context."""
    ai = dict(ai_settings or {})
    mc = dict(merchant_context or {})
    if not ai and isinstance(mc.get("ai_settings"), dict):
        ai = dict(mc["ai_settings"])

    cod_enabled = ai.get("cash_on_delivery_enabled")
    if cod_enabled is None:
        cod_enabled = ai.get("cod_enabled")
    if cod_enabled is None:
        cod_enabled = mc.get("cod_enabled")
    if cod_enabled is None:
        allow_flow = ai.get("allow_cod_confirmation_flow")
        if allow_flow is None and isinstance(mc.get("brain_profile"), dict):
            allow_flow = mc["brain_profile"].get("allow_cod_confirmation_flow")
        cod_enabled = bool(allow_flow) if allow_flow is not None else False

    methods_raw = (
        ai.get("available_payment_methods")
        or ai.get("payment_methods")
        or mc.get("available_payment_methods")
        or []
    )
    methods: List[str] = []
    if isinstance(methods_raw, list):
        methods = [str(m).strip().lower() for m in methods_raw if str(m).strip()]
    elif isinstance(methods_raw, dict):
        methods = [str(k).strip().lower() for k, enabled in methods_raw.items() if enabled]

    if not methods:
        methods = ["alrajhi", "alahli", "stc_pay", "mobily_pay", "barq"]

    return CodPolicyEvidence(
        cash_on_delivery_enabled=bool(cod_enabled),
        available_methods=methods,
        source="tenant_settings",
    )


def build_cod_policy_reply(
    evidence: CodPolicyEvidence,
    *,
    continue_order: bool = False,
) -> CodPolicyReply:
    """Deterministic COD answer from evidence — style left to persona elsewhere."""
    if evidence.cash_on_delivery_enabled:
        body = "نعم، الدفع عند الاستلام متاح."
        if continue_order:
            body += " نكمل بيانات التوصيل الآن؟"
    else:
        methods = _method_labels(evidence.available_methods)
        body = (
            "حاليًا الدفع عند الاستلام غير متاح. "
            f"المتوفر الدفع مسبقًا عبر: {methods}. "
            "أي وسيلة تفضل؟"
        )
    return CodPolicyReply(reply_text=body, evidence=evidence)


__all__ = [
    "CodPolicyEvidence",
    "CodPolicyReply",
    "build_cod_policy_reply",
    "load_cod_policy_evidence",
]
