"""
core/wa_payment_submission.py
─────────────────────────────
PR-2 — attach WhatsApp payment claims / receipts to active Nahla orders
without promoting to ``paid`` until explicit verification.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.wa_order_linking import MSG_WA_PAYMENT_UNLINKED, find_linkable_wa_order

logger = logging.getLogger("nahla.wa_payment_submission")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_payment_submission_prep_patch(
    *,
    submission_type: str,
    receipt_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Brain ``order_prep`` patch for a submitted (unverified) payment."""
    now = _utcnow_iso()
    patch: Dict[str, Any] = {
        "payment_method":              "bank_transfer",
        "payment_status":              "pending_verification",
        "payment_confirmed":           False,
        "payment_verification_status": "pending",
        "payment_submission_source":   "whatsapp",
        "payment_submission_type":   submission_type,
        "order_status":                "payment_submitted",
        "awaiting_payment_receipt":    False,
    }
    if submission_type == "text_claim":
        patch.update({
            "payment_submission_received": True,
            "payment_submission_at":       now,
        })
    else:
        patch.update({
            "payment_receipt_received": True,
            "payment_receipt_at":       now,
            "payment_submission_received": True,
            "payment_submission_at":       now,
        })
        if receipt_metadata:
            patch["payment_receipt_metadata"] = dict(receipt_metadata)
    return patch


def build_payment_submission_order_metadata(
    *,
    submission_type: str,
    trigger: str,
) -> Dict[str, Any]:
    return {
        "payment_method":                  "bank_transfer",
        "payment_status":                "pending_verification",
        "payment_receipt_received":        submission_type != "text_claim",
        "payment_confirmed":               False,
        "payment_verification_status":     "pending",
        "payment_submission_source":       "whatsapp",
        "payment_submission_type":         submission_type,
        "payment_submission_trigger":      trigger,
        "payment_submission_at":           _utcnow_iso(),
        "payment_provider":                None,
    }


def record_unlinked_payment_claim(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    submission_type: str,
    preview: str = "",
) -> None:
    """Persist an orphan payment claim on the conversation — never creates a paid order."""
    if conversation is None or db is None:
        return
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        meta = dict(getattr(conversation, "extra_metadata", None) or {})
        claims = list(meta.get("unlinked_payment_claims") or [])
        claims.append({
            "at":                _utcnow_iso(),
            "submission_type":   submission_type,
            "preview":           (preview or "")[:200],
            "payment_confirmed": False,
        })
        meta["unlinked_payment_claims"] = claims[-20:]
        meta["last_unlinked_payment_claim_at"] = _utcnow_iso()
        conversation.extra_metadata = meta
        try:
            flag_modified(conversation, "extra_metadata")
        except Exception:
            pass
        db.add(conversation)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[WA_PAYMENT_SUBMISSION] unlinked claim persist failed tenant=%s: %s",
            tenant_id, exc,
        )


def apply_wa_payment_submission(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    submission_type: str,
    conversation: Any = None,
    receipt_metadata: Optional[Dict[str, Any]] = None,
    trigger: str = "payment_submission",
) -> Dict[str, Any]:
    """
    Link payment evidence to the active WA order when possible.

    Returns ``{"linked": bool, "order_id": ..., "reply_text": ...}``.
    Never raises.
    """
    result: Dict[str, Any] = {
        "linked":     False,
        "order_id":   None,
        "reply_text": "",
    }
    if db is None or not tenant_id or not phone:
        result["reply_text"] = MSG_WA_PAYMENT_UNLINKED
        return result

    try:
        from core.order_flow import _load_brain_state, apply_state_patch  # noqa: PLC0415

        conv = conversation
        bs: Dict[str, Any] = {}
        if conv is None:
            conv, bs = _load_brain_state(db, tenant_id=int(tenant_id), phone=phone)
        elif isinstance(getattr(conv, "extra_metadata", None), dict):
            bs = dict((conv.extra_metadata or {}).get("brain_state") or {})

        cust = getattr(conv, "customer", None) if conv is not None else None
        order = find_linkable_wa_order(
            db,
            tenant_id=int(tenant_id),
            conversation=conv,
            customer=cust,
            phone_candidates=(phone,),
        )

        if order is None:
            record_unlinked_payment_claim(
                db,
                tenant_id=int(tenant_id),
                conversation=conv,
                submission_type=submission_type,
                preview=str(receipt_metadata.get("caption") if receipt_metadata else ""),
            )
            result["reply_text"] = MSG_WA_PAYMENT_UNLINKED
            logger.info(
                "[WA_PAYMENT_SUBMISSION] unlinked tenant=%s phone=*%s type=%s",
                tenant_id, (phone or "")[-4:], submission_type,
            )
            return result

        patch = build_payment_submission_prep_patch(
            submission_type=submission_type,
            receipt_metadata=receipt_metadata,
        )
        apply_state_patch(
            db,
            tenant_id=int(tenant_id),
            phone=phone,
            state_patch=patch,
        )
        result["linked"] = True
        result["order_id"] = getattr(order, "id", None)
        logger.info(
            "[WA_PAYMENT_SUBMISSION] linked tenant=%s phone=*%s order=%s type=%s",
            tenant_id, (phone or "")[-4:], result["order_id"], submission_type,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WA_PAYMENT_SUBMISSION] apply failed tenant=%s phone=*%s err=%s",
            tenant_id, (phone or "")[-4:], exc,
        )
        result["reply_text"] = MSG_WA_PAYMENT_UNLINKED
        return result


__all__ = [
    "MSG_WA_PAYMENT_UNLINKED",
    "apply_wa_payment_submission",
    "build_payment_submission_order_metadata",
    "build_payment_submission_prep_patch",
    "record_unlinked_payment_claim",
]
