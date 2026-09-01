"""
core/wa_payment_submission.py
─────────────────────────────
PR-2 — attach WhatsApp payment claims / receipts to active Nahla orders
without promoting to ``paid`` until explicit verification.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.wa_order_linking import MSG_WA_PAYMENT_UNLINKED, find_linkable_wa_order

logger = logging.getLogger("nahla.wa_payment_submission")

_ACTIVE_PAYMENT_KEYS = (
    "payment_method",
    "payment_status",
    "payment_confirmed",
    "payment_verified",
    "payment_settled",
    "payment_verification_status",
    "payment_submission_source",
    "payment_submission_type",
    "payment_submission_received",
    "payment_submission_at",
    "awaiting_payment_receipt",
    "payment_receipt_received",
    "payment_receipt_at",
    "payment_receipt_metadata",
    "payment_claim_unverified",
    "payment_claim_unverified_at",
    "payment_claim_text_preview",
    "payment_claim_at",
    "payment_resolution_state",
    "payment_review_state",
    "payment_evidence_received",
    "payment_destination",
    "requested_bank",
    "payment_bank",
)

_PAYMENT_FUNNEL_STATUSES = frozenset({
    "awaiting_receipt",
    "awaiting_payment",
    "under_review",
    "payment_submitted",
    "pending_payment",
    "payment_pending",
    "complete",
    "paid",
})

_ACTIVE_PAYMENT_RESET: Dict[str, Any] = {
    "payment_method": "",
    "payment_status": "",
    "payment_confirmed": False,
    "payment_verified": False,
    "payment_settled": False,
    "payment_verification_status": "",
    "payment_submission_source": "",
    "payment_submission_type": "",
    "payment_submission_received": False,
    "payment_submission_at": "",
    "awaiting_payment_receipt": False,
    "payment_receipt_received": False,
    "payment_receipt_at": "",
    "payment_receipt_metadata": {},
    "payment_claim_unverified": False,
    "payment_claim_unverified_at": "",
    "payment_claim_text_preview": "",
    "payment_claim_at": "",
    "payment_resolution_state": "",
    "payment_review_state": "not_started",
    "payment_evidence_received": False,
    "payment_destination": {},
    "requested_bank": "",
    "payment_bank": "",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prep_get(prep: Any, key: str, default: Any = None) -> Any:
    if prep is None:
        return default
    if isinstance(prep, dict):
        return prep.get(key, default)
    return getattr(prep, key, default)


def _prep_set(prep: Any, key: str, value: Any) -> None:
    if prep is None:
        return
    if isinstance(prep, dict):
        prep[key] = value
        return
    try:
        setattr(prep, key, value)
    except Exception:
        pass


def _copy_payment_value(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return [dict(x) if isinstance(x, dict) else x for x in value]
    return value


def _has_active_payment_evidence(snapshot: Dict[str, Any]) -> bool:
    if snapshot.get("payment_receipt_received") or snapshot.get("payment_evidence_received"):
        return True
    if snapshot.get("payment_claim_unverified"):
        return True
    if snapshot.get("payment_method"):
        return True
    if snapshot.get("payment_receipt_metadata"):
        return True
    if snapshot.get("payment_destination"):
        return True
    status = str(snapshot.get("order_status") or "").strip().lower()
    return status in _PAYMENT_FUNNEL_STATUSES


def isolate_active_payment_for_new_checkout(
    prep: Any,
    *,
    reason: str = "new_catalog_checkout",
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Unbind prior-order payment evidence from a new active checkout.

    Historical fields are appended to ``payment_evidence_history`` and
    remain tied to the previous ``checkout_payment_id`` / product when
    known. Active payment slots are reset. Conversation-level
    ``last_payment_confirmed_at`` is never deleted.
    """
    result: Dict[str, Any] = {
        "archived": False,
        "new_checkout_payment_id": "",
        "old_checkout_payment_id": str(_prep_get(prep, "checkout_payment_id", "") or ""),
    }
    if prep is None:
        return result

    snapshot: Dict[str, Any] = {}
    for key in _ACTIVE_PAYMENT_KEYS:
        snapshot[key] = _copy_payment_value(_prep_get(prep, key))
    snapshot["order_status"] = _prep_get(prep, "order_status", "")
    snapshot["product_id"] = _prep_get(prep, "product_id", "")
    snapshot["checkout_payment_id"] = result["old_checkout_payment_id"]

    if _has_active_payment_evidence(snapshot):
        history = list(_prep_get(prep, "payment_evidence_history", None) or [])
        history.append({
            "archived_at": _utcnow_iso(),
            "reason": str(reason or "new_checkout"),
            "tenant_id": int(tenant_id) if tenant_id else None,
            "checkout_payment_id": result["old_checkout_payment_id"] or None,
            "product_id": snapshot.get("product_id") or None,
            "evidence": snapshot,
        })
        _prep_set(prep, "payment_evidence_history", history[-20:])
        result["archived"] = True
        result["history_len"] = len(history[-20:])

    for key, value in _ACTIVE_PAYMENT_RESET.items():
        _prep_set(prep, key, _copy_payment_value(value))

    old_status = str(snapshot.get("order_status") or "").strip().lower()
    if old_status in _PAYMENT_FUNNEL_STATUSES or not old_status:
        _prep_set(prep, "order_status", "awaiting_address")

    new_id = uuid.uuid4().hex
    _prep_set(prep, "checkout_payment_id", new_id)
    result["new_checkout_payment_id"] = new_id
    return result


def resolve_verified_payment_destinations(
    db: Any,
    *,
    tenant_id: int,
) -> List[Dict[str, Any]]:
    """Tenant-scoped complete IBANs only. Never invents or truncates."""
    if db is None or not tenant_id:
        return []
    try:
        from core.tenant_payment_accounts import (  # noqa: PLC0415
            canonical_iban,
            load_tenant_payment_accounts,
        )
    except Exception:
        return []
    try:
        accounts = load_tenant_payment_accounts(db, tenant_id=int(tenant_id))
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in accounts.ibans or ():
        canon = canonical_iban(str(raw or ""))
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append({
            "iban": canon,
            "source": "tenant_payment_accounts",
            "tenant_id": int(tenant_id),
            "complete": True,
            "verified_or_eligible": True,
        })
    return out


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
        except Exception:  # noqa: silent-ok — SQLAlchemy optional when metadata unchanged
            pass
        db.add(conversation)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[WA_PAYMENT_SUBMISSION] unlinked claim persist failed tenant=%s",
            tenant_id,
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
    "isolate_active_payment_for_new_checkout",
    "record_unlinked_payment_claim",
    "resolve_verified_payment_destinations",
]
