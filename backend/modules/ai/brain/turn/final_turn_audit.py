"""
turn/final_turn_audit.py
────────────────────────
Phase 3.1 — shadow audit of outbound reply vs FinalTurnContract.

Detects violations and logs ``[FINAL_TURN_VIOLATION]`` — never mutates reply
in shadow mode.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_SEARCH_PRODUCTS,
)
from .final_turn_contract import FinalTurnContract
from .flags import is_final_turn_contract_shadow_enabled

logger = logging.getLogger("nahla.brain.final_turn_audit")

_CATALOG_BRAIN_ACTIONS = frozenset({
    ACTION_SEARCH_PRODUCTS,
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
})

_GENERIC_PROMISE_RE = re.compile(
    r"(?:"
    r"(?:س(?:أ|ا)رسل|ر(?:اح|ح)\s*أ?رسل|ب(?:أ|ا)رسل|"
    r"أ?(?:رسل|رس(?:ل|li)|send)|"
    r"أ?(?:عرض|اعرض|show)|"
    r"أ?(?:جهز|أجهز))"
    r"(?:\s*(?:لك|لـ?\s*ك|لي|الآن|الحين))?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_PROMISE_RE = re.compile(
    r"(?:"
    r"(?:من\s+)?(?:ال)?(?:كتالوج|catalog)|"
    r"(?:ال)?(?:خيارات|options|choices|variants|الأنواع|انواع|types)|"
    r"(?:ال)?(?:منتجات|products)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_VARIANT_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"وش\s+(?:ال)?(?:خيار|خيارات|option|options|variant|variants|كمية|quantity)|"
    r"(?:أ?ي|any)\s+(?:خيار|option|variant)|"
    r"what\s+(?:option|variant|quantity)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_QUESTION_RE = re.compile(
    r"(?:"
    r"وش\s+(?:ال)?(?:منتج|product|عدد|وزن|quantity|qty)|"
    r"do\s+you\s+have|"
    r"هل\s+(?:عند(?:ك|كم)|متوفر)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PHONE_QUESTION_RE = re.compile(
    r"(?:"
    r"(?:رقم|number)\s*(?:ال)?(?:جوال|هاتف|موبايل|phone|mobile)|"
    r"(?:جوال|هاتف|موبايل|phone|mobile)\s*(?:ك|كم|your)?|"
    r"ممكن\s+(?:ترسل|تذكر|تعطيني)\s+(?:رقم|جوال|هاتف)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_RECEIPT_ADDRESS_ASK_RE = re.compile(
    r"(?:"
    r"عنوان\s*(?:ال)?(?:توصيل|بيت|المنزل)|"
    r"احتاج\s*(?:منك)?\s*عنوان|"
    r"أحتاج\s*(?:منك)?\s*عنوان|"
    r"شار(?:ك|كنا)\s*عنوان|"
    r"العنوان\s*الوطني|"
    r"رابط\s*قوقل\s*ماب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_CLAIM_RE = re.compile(
    r"(?:متوفر|available|after\s+several\s+options|بعدة\s+خيارات)",
    re.UNICODE | re.IGNORECASE,
)

_BROWSE_PRODUCT_SHIFT_RE = re.compile(
    r"(?:"
    r"(?:ال)?(?:كتالوج|catalog|منتج|product|متوفر|available|"
    r"خيارات|options|browse|تصفح)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_IDENTITY_COLLAB_SHIFT_RE = re.compile(
    r"(?:تعاون|collaborat|beekeeper|نحال|معلم\s+نحل|خبرة\s+في)",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class FinalTurnAuditResult:
    phase: str
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


def _has_catalog_execution_evidence(result_data: Optional[dict]) -> bool:
    if not result_data:
        return False
    for key in ("products", "pending_candidates", "catalog_products"):
        rows = result_data.get(key)
        if isinstance(rows, list) and rows:
            return True
    if result_data.get("catalog_navigate") or result_data.get("product_card"):
        return True
    return False


def _reply_uses_untrusted_inbound_label(contract: FinalTurnContract, reply: str) -> bool:
    inbound = str(contract.inbound_text or "").strip()
    if not inbound or len(inbound) < 4:
        return False
    if contract.trusted_product_label:
        return False
    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_non_product_label,
        )

        if not is_non_product_label(inbound):
            return False
    except Exception:  # noqa: BLE001
        return False
    blob = str(reply or "")
    if inbound in blob:
        return True
    if _AVAILABILITY_CLAIM_RE.search(blob):
        return True
    return False


def detect_final_turn_violations(
    contract: FinalTurnContract,
    reply: str,
    *,
    result_data: Optional[dict] = None,
) -> List[str]:
    """Return violation type keys — empty when compliant."""
    if not contract or not str(reply or "").strip():
        return []

    violations: List[str] = []
    blob = str(reply or "").strip()
    forbidden_q = set(contract.forbidden_question_types or [])
    action = str(contract.decision_action or "")

    has_generic_promise = bool(_GENERIC_PROMISE_RE.search(blob))
    has_catalog_promise = bool(_CATALOG_PROMISE_RE.search(blob))
    has_catalog_action = action in _CATALOG_BRAIN_ACTIONS
    has_execution = _has_catalog_execution_evidence(result_data)

    if has_generic_promise and action == ACTION_LLM_REPLY and not has_catalog_action:
        if has_catalog_promise or "catalog_promise" in contract.promises_forbidden:
            violations.append("promise_without_action")

    if has_catalog_promise and not has_catalog_action and not has_execution:
        if (
            "catalog_promise" in forbidden_q
            or "catalog_promise" in contract.promises_forbidden
            or not contract.browse_allowed
        ):
            violations.append("catalog_promise_without_catalog_action")

    if "name" in forbidden_q:
        try:
            from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                reply_contains_forbidden_catalog_name_question,
            )

            if reply_contains_forbidden_catalog_name_question(blob):
                violations.append("forbidden_name_question")
        except Exception:  # noqa: BLE001  # noqa: silent-ok — optional catalog name guard import
            pass

    if "phone" in forbidden_q and _PHONE_QUESTION_RE.search(blob):
        violations.append("forbidden_phone_question")

    if "product" in forbidden_q and _PRODUCT_QUESTION_RE.search(blob):
        violations.append("forbidden_product_question")

    if "variant" in forbidden_q and _VARIANT_FOLLOWUP_RE.search(blob):
        violations.append("forbidden_variant_followup")

    if (
        "availability" in forbidden_q or "product" in forbidden_q
    ) and _reply_uses_untrusted_inbound_label(contract, blob):
        if _AVAILABILITY_CLAIM_RE.search(blob) or contract.inbound_text in blob:
            violations.append("unsafe_product_availability_claim")

    known = dict(contract.known_facts or {})
    receipt_ctx = bool(known.get("payment_receipt_turn") or known.get("receipt_received"))
    confirmed_order = bool(known.get("receipt_confirmed_order"))
    if receipt_ctx and not confirmed_order:
        product_label = str(known.get("receipt_product_label") or contract.trusted_product_label or "").strip()
        if product_label and product_label in blob:
            violations.append("payment_receipt_product_claim_without_order_evidence")
        if _RECEIPT_ADDRESS_ASK_RE.search(blob):
            violations.append("payment_receipt_address_request_without_confirmed_order")

    purpose = str(contract.response_purpose or "")
    if purpose in {"shipping_post_order", "shipping", "track_order"}:
        if _BROWSE_PRODUCT_SHIFT_RE.search(blob) and _AVAILABILITY_CLAIM_RE.search(blob):
            violations.append("shipping_context_shifted_to_product")
        elif purpose == "shipping_post_order" and _PRODUCT_QUESTION_RE.search(blob):
            violations.append("shipping_context_shifted_to_product")
        if purpose == "shipping_post_order" and _IDENTITY_COLLAB_SHIFT_RE.search(blob):
            violations.append("shipping_context_shifted_to_product")

    # Dedupe preserving order
    seen: set[str] = set()
    out: List[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def audit_final_turn_reply(
    contract: FinalTurnContract,
    reply: str,
    *,
    phase: str,
    tenant_id: Optional[int] = None,
    result_data: Optional[dict] = None,
) -> FinalTurnAuditResult:
    """
    Shadow audit — log violations, never mutate ``reply``.
    """
    if not is_final_turn_contract_shadow_enabled():
        return FinalTurnAuditResult(phase=phase, violations=())

    violations = tuple(
        detect_final_turn_violations(contract, reply, result_data=result_data)
    )
    if violations:
        logger.warning(
            "[FINAL_TURN_VIOLATION] tenant=%s phase=%s violations=%s "
            "purpose=%s action=%s topic=%s browse_allowed=%s preview=%r reply_preview=%r",
            tenant_id,
            phase,
            list(violations),
            contract.response_purpose,
            contract.decision_action,
            contract.decision_topic,
            contract.browse_allowed,
            (contract.inbound_text or "")[:80],
            (reply or "")[:120],
        )
    return FinalTurnAuditResult(phase=phase, violations=violations)


__all__ = [
    "FinalTurnAuditResult",
    "audit_final_turn_reply",
    "detect_final_turn_violations",
]
