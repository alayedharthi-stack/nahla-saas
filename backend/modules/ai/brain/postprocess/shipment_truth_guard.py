"""
modules/ai/brain/postprocess/shipment_truth_guard.py
────────────────────────────────────────────────────
Block false shipment-completed wording when structured shipment
evidence is missing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from modules.ai.brain.postprocess.shipment_evidence import (
    ShipmentEvidenceResult,
    evaluate_shipment_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.shipment_truth_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

SAFE_PRE_SHIPMENT_REPLY_AR = (
    "طلبك تحت المراجعة/التجهيز، وبنبلغك برابط التتبع أول ما يصدر 🚚"
)

_SHIPMENT_COMPLETED_MARKERS = (
    "تم الشحن",
    "شحناه",
    "تم تسليمها للناقل",
    "في الطريق لشركة الشحن",
    "خرجت مع شركة الشحن",
)


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def reply_contains_shipment_completed_wording(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    markers = (_norm(marker) for marker in _SHIPMENT_COMPLETED_MARKERS)
    return any(marker in norm for marker in markers)


@dataclass(frozen=True)
class ShipmentTruthGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    evidence: Optional[ShipmentEvidenceResult] = None


def log_shipment_truth_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    action: str,
    reason: str,
    evidence_source: str,
    order_status: str,
    tracking_present: bool,
) -> None:
    try:
        logger.info(
            "[SHIPMENT_TRUTH_GUARD] tenant_id=%s conversation_id=%s "
            "action=%s reason=%s evidence_source=%s order_status=%s "
            "tracking_present=%s",
            tenant_id,
            conversation_id,
            action,
            reason or "-",
            evidence_source or "-",
            order_status or "-",
            bool(tracking_present),
        )
    except Exception:  # noqa: BLE001
        pass


def apply_shipment_truth_guard(
    *,
    reply: str,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    payment_receipt_received: bool = False,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> ShipmentTruthGuardResult:
    try:
        original = str(reply or "")
        if not original.strip():
            return ShipmentTruthGuardResult(reply=original, action="allowed")

        if not reply_contains_shipment_completed_wording(original):
            evidence = evaluate_shipment_evidence(
                commerce_bundle=commerce_bundle,
                extra_metadata=extra_metadata,
                inbound_metadata=inbound_metadata,
                payment_receipt_received=payment_receipt_received,
            )
            log_shipment_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason="no_shipment_completed_wording",
                evidence_source=evidence.evidence_source,
                order_status=evidence.order_status,
                tracking_present=evidence.tracking_present,
            )
            return ShipmentTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        evidence = evaluate_shipment_evidence(
            commerce_bundle=commerce_bundle,
            extra_metadata=extra_metadata,
            inbound_metadata=inbound_metadata,
            payment_receipt_received=payment_receipt_received,
        )

        if evidence.evidence_ok:
            log_shipment_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="allowed",
                reason=evidence.reason,
                evidence_source=evidence.evidence_source,
                order_status=evidence.order_status,
                tracking_present=evidence.tracking_present,
            )
            return ShipmentTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        log_shipment_truth_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="blocked_false_shipment",
            reason=evidence.reason,
            evidence_source=evidence.evidence_source,
            order_status=evidence.order_status,
            tracking_present=evidence.tracking_present,
        )
        return ShipmentTruthGuardResult(
            reply=SAFE_PRE_SHIPMENT_REPLY_AR,
            action="blocked_false_shipment",
            replaced=True,
            reason=evidence.reason,
            evidence=evidence,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[SHIPMENT_TRUTH_GUARD] guard failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return ShipmentTruthGuardResult(reply=str(reply or ""), action="allowed")
