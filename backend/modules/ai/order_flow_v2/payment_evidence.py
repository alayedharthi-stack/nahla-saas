"""OrderFlowV2 payment evidence guard."""
from __future__ import annotations

from typing import Any, Dict, Optional

RECEIPT_MISSING = "receipt_missing"
RECEIPT_RECEIVED_NEEDS_REVIEW = "receipt_received_needs_review"
RECEIPT_REJECTED_MISMATCH = "receipt_rejected_mismatch"
RECEIPT_VERIFIED_BY_MERCHANT = "receipt_verified_by_merchant"


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _norm_bank(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "rajhi" in text or "راجح" in text:
        return "rajhi"
    if "ahli" in text or "ncb" in text or "snb" in text or "اهلي" in text or "الأهلي" in text:
        return "alahli"
    return text


def evaluate_receipt_status(
    *,
    order_prep: Dict[str, Any],
    receipt_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify receipt status without granting payment confirmation."""
    prep = dict(order_prep or {})
    md = dict(receipt_metadata or prep.get("payment_receipt_metadata") or {})
    if prep.get("receipt_verified_by_merchant") is True:
        return {
            "receipt_status": RECEIPT_VERIFIED_BY_MERCHANT,
            "payment_confirmed_allowed": True,
            "reason": "merchant_verified",
        }
    if not md and not prep.get("payment_receipt_received"):
        return {
            "receipt_status": RECEIPT_MISSING,
            "payment_confirmed_allowed": False,
            "reason": "receipt_missing",
        }

    expected_total = _as_float(
        prep.get("order_flow_v2_catalog_total")
        or prep.get("order_total")
        or prep.get("total")
    )
    receipt_amount = _as_float(
        md.get("amount")
        or md.get("amount_value")
        or md.get("detected_amount")
        or md.get("total")
    )
    if expected_total is not None and receipt_amount is not None:
        if abs(expected_total - receipt_amount) > 0.01:
            return {
                "receipt_status": RECEIPT_REJECTED_MISMATCH,
                "payment_confirmed_allowed": False,
                "reason": "amount_mismatch",
                "expected_total": expected_total,
                "receipt_amount": receipt_amount,
            }

    requested_bank = _norm_bank(prep.get("requested_bank") or prep.get("payment_bank"))
    receipt_bank = _norm_bank(md.get("bank") or md.get("bank_name") or md.get("detected_bank"))
    if requested_bank and receipt_bank and requested_bank != receipt_bank:
        return {
            "receipt_status": RECEIPT_REJECTED_MISMATCH,
            "payment_confirmed_allowed": False,
            "reason": "bank_mismatch",
            "requested_bank": requested_bank,
            "receipt_bank": receipt_bank,
        }

    evidence_status = str(md.get("payment_evidence_status") or "").strip().lower()
    if evidence_status and evidence_status not in {"confirmed", "payment_confirmed"}:
        return {
            "receipt_status": RECEIPT_RECEIVED_NEEDS_REVIEW,
            "payment_confirmed_allowed": False,
            "reason": evidence_status,
        }

    return {
        "receipt_status": RECEIPT_RECEIVED_NEEDS_REVIEW,
        "payment_confirmed_allowed": False,
        "reason": "merchant_review_required",
    }


def payment_confirmation_allowed(order_prep: Dict[str, Any]) -> bool:
    return evaluate_receipt_status(order_prep=order_prep).get("payment_confirmed_allowed") is True
