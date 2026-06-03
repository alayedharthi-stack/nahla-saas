"""
modules/ai/brain/postprocess/payment_evidence.py
────────────────────────────────────────────────
Structured payment evidence only — never infer from customer text
claims (تم الدفع / حولت), stale brain state, or LLM wording alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

_DETERMINISTIC_EVIDENCE_PATHS = frozenset({
    "payment_receipt_ack",
    "payment_claim_ack",
    "payment_evidence_soft_ack",
})


@dataclass(frozen=True)
class PaymentEvidenceResult:
    evidence_ok: bool
    evidence_source: str
    payment_evidence_status: str
    receipt_media_present: bool
    reason: str


def _receipt_media_present(metadata: Optional[Dict[str, Any]]) -> bool:
    md = metadata or {}
    pdf_kind = str(md.get("pdf_kind") or "").strip()
    image_kind = str(md.get("image_kind") or "").strip()
    return pdf_kind == "payment_receipt" or image_kind == "payment_receipt"


def evaluate_payment_evidence(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    chosen_path: str = "",
    inbound_text: str = "",
    payment_receipt_received: bool = False,
) -> PaymentEvidenceResult:
    """Return whether trusted payment evidence exists for this turn."""
    del payment_receipt_received  # stale flag never grants evidence alone

    path = str(chosen_path or "").strip()
    if path in _DETERMINISTIC_EVIDENCE_PATHS:
        return PaymentEvidenceResult(
            evidence_ok=True,
            evidence_source="deterministic_path",
            payment_evidence_status=str(
                (inbound_metadata or {}).get("payment_evidence_status") or ""
            ),
            receipt_media_present=_receipt_media_present(inbound_metadata),
            reason=f"chosen_path={path}",
        )

    md = inbound_metadata or {}
    pe = str(md.get("payment_evidence_status") or "").strip()
    media_present = _receipt_media_present(md)

    if pe == "confirmed":
        source = "payment_evidence_status_confirmed"
        if media_present:
            source = "payment_receipt_media_confirmed"
        return PaymentEvidenceResult(
            evidence_ok=True,
            evidence_source=source,
            payment_evidence_status=pe,
            receipt_media_present=media_present,
            reason="payment_evidence_status=confirmed",
        )

    try:
        from core.payment_intent import detect_payment_confirmation_text  # noqa: PLC0415

        if detect_payment_confirmation_text(inbound_text):
            return PaymentEvidenceResult(
                evidence_ok=False,
                evidence_source="none",
                payment_evidence_status=pe,
                receipt_media_present=media_present,
                reason="customer_text_claim_without_evidence",
            )
    except Exception:  # noqa: BLE001
        pass

    return PaymentEvidenceResult(
        evidence_ok=False,
        evidence_source="none",
        payment_evidence_status=pe,
        receipt_media_present=media_present,
        reason="no_structured_payment_evidence",
    )
