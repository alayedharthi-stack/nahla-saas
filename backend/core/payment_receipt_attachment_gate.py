"""
core/payment_receipt_attachment_gate.py
────────────────────────────────────────
Deterministic payment-receipt detection from inbound attachment metadata.

No OCR dependency — uses mime type, normalized inbound type, filename hints,
and active bank-transfer / awaiting-receipt order context only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.payment_media_metadata import flatten_inbound_payment_metadata

logger = logging.getLogger("nahla.payment_receipt_attachment_gate")

ROUTE_PAYMENT_RECEIPT_RECEIVED = "PAYMENT_RECEIPT_RECEIVED"

PAYMENT_RECEIPT_ATTACHMENT_ACK_AR = (
    "وصل الإيصال، أراجعه لك وأأكد الطلب بعد التحقق من التحويل."
)

PAYMENT_RECEIPT_DUPLICATE_ACK_AR = (
    "سبق واستلمنا الإيصال، فريقنا يراجع التحويل وسيأكد لك الطلب."
)

_ASK_FOR_RECEIPT_MARKERS = (
    "اذا ارسلت الايصال",
    "إذا أرسلت الإيصال",
    "ارسل الايصال",
    "أرسل الإيصال",
    "ابعث الايصال",
    "ارسل صورة التحويل",
    "أرسل صورة التحويل",
)

_RECEIPT_FILENAME_HINTS = (
    "receipt",
    "transfer",
    "bank",
    "iban",
    "hawl",
    "hawala",
    "invoice",
    "payment",
    "ايصال",
    "إيصال",
    "تحويل",
    "حوال",
    "حواله",
    "حوالة",
    "فاتور",
)

_EXCLUDED_KINDS = frozenset({
    "payment_pre_review",
    "payment_pending_evidence",
})

_PAYMENT_FLOW_STATUSES = frozenset({
    "awaiting_payment",
    "awaiting_payment_receipt",
    "awaiting_receipt",
    "payment_submitted",
    "under_review",
    "pending_review",
    "awaiting_address",
    "awaiting_product",
})


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670]", "", t)
    return (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )


def reply_asks_to_send_receipt(reply: Optional[str]) -> bool:
    norm = _norm(reply or "")
    if not norm:
        return False
    return any(_norm(marker) in norm for marker in _ASK_FOR_RECEIPT_MARKERS)


def has_inbound_attachment(
    inbound_normalized_type: str,
    metadata: Optional[Dict[str, Any]],
) -> bool:
    md = flatten_inbound_payment_metadata(metadata or {})
    ntype = str(inbound_normalized_type or md.get("normalized_type") or "").strip().lower()
    if ntype in {"document", "image"}:
        return True
    if md.get("has_attached_media") or md.get("storage_url") or md.get("storage_sha256"):
        return True
    mime = str(md.get("mime_type") or "").strip().lower()
    if mime.startswith("image/") or mime == "application/pdf":
        return True
    return False


def filename_suggests_receipt(metadata: Optional[Dict[str, Any]]) -> bool:
    md = flatten_inbound_payment_metadata(metadata or {})
    blob = _norm(
        " ".join(
            str(md.get(key) or "")
            for key in ("filename", "caption", "document_filename")
        )
    )
    if not blob:
        return False
    return any(hint in blob for hint in _RECEIPT_FILENAME_HINTS)


def mime_suggests_receipt(metadata: Optional[Dict[str, Any]]) -> bool:
    md = flatten_inbound_payment_metadata(metadata or {})
    mime = str(md.get("mime_type") or "").strip().lower()
    if mime.startswith("image/") or mime in {"application/pdf", "application/x-pdf"}:
        return True
    return False


def payment_context_active(summary: Optional[Dict[str, Any]]) -> bool:
    s = summary or {}
    if bool(s.get("awaiting_payment_receipt")):
        return True
    if bool(s.get("payment_receipt_received")):
        return True
    if bool(s.get("selected_product")):
        return True
    status = str(s.get("order_status") or "").strip().lower()
    if status in _PAYMENT_FLOW_STATUSES:
        return True
    if str(s.get("payment_method") or "").strip().lower() in {
        "bank_transfer",
        "transfer",
        "iban",
    }:
        return True
    return False


def is_excluded_non_receipt_attachment(
    metadata: Optional[Dict[str, Any]],
    *,
    payment_context: bool,
    summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True when attachment should NOT flip receipt-received state."""
    if not payment_context:
        return True
    s = summary or {}
    if bool(s.get("awaiting_payment_receipt")):
        return False
    md = flatten_inbound_payment_metadata(metadata or {})
    pe = str(md.get("payment_evidence_status") or "").strip().lower()
    reason = str(md.get("payment_evidence_reason") or "").strip().lower()
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "").strip().lower()

    if reason == "semantic_rejected_document":
        return True
    if kind in _EXCLUDED_KINDS and not filename_suggests_receipt(md):
        return True
    if pe == "not_payment" and not filename_suggests_receipt(md):
        if mime_suggests_receipt(md) and payment_context:
            return False
        return True
    return False


def is_likely_payment_receipt_attachment(
    inbound_normalized_type: str,
    metadata: Optional[Dict[str, Any]],
    *,
    summary: Optional[Dict[str, Any]] = None,
) -> bool:
    if not has_inbound_attachment(inbound_normalized_type, metadata):
        return False
    if not payment_context_active(summary):
        return False
    if is_excluded_non_receipt_attachment(
        metadata,
        payment_context=True,
        summary=summary,
    ):
        return False

    md = flatten_inbound_payment_metadata(metadata or {})
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "").strip().lower()
    if kind == "payment_receipt":
        return True
    if filename_suggests_receipt(md):
        return True
    if mime_suggests_receipt(md):
        return True
    if bool(summary and summary.get("awaiting_payment_receipt")):
        return True
    return False


def build_receipt_received_state_patch(
    *,
    inbound_metadata: Optional[Dict[str, Any]],
    source: str = "attachment_metadata",
) -> Dict[str, Any]:
    md = flatten_inbound_payment_metadata(inbound_metadata or {})
    now_iso = datetime.now(timezone.utc).isoformat()
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "payment_receipt").strip()
    patch: Dict[str, Any] = {
        "payment_method": "bank_transfer",
        "payment_status": "pending_verification",
        "awaiting_payment_receipt": False,
        "payment_receipt_received": True,
        "payment_receipt_at": now_iso,
        "payment_confirmed": False,
        "payment_verification_status": "pending",
        "payment_submission_received": True,
        "payment_submission_at": now_iso,
        "payment_submission_type": "receipt",
        "payment_submission_source": "whatsapp",
        "order_status": "payment_submitted",
        "payment_receipt_metadata": {
            "kind": kind or "payment_receipt",
            "source": source,
            "route": ROUTE_PAYMENT_RECEIPT_RECEIVED,
            "wa_message_id": md.get("wa_message_id"),
            "filename": md.get("filename"),
            "mime_type": md.get("mime_type"),
            "storage_url": md.get("storage_url"),
            "storage_sha256": md.get("storage_sha256"),
            "received_at": now_iso,
            "manual_verification_required": True,
        },
    }
    for key in (
        "vision_text",
        "ocr_text",
        "pdf_text_preview",
        "pdf_text_full",
        "caption",
    ):
        val = md.get(key)
        if val not in (None, ""):
            patch["payment_receipt_metadata"][key] = val
    return patch


@dataclass(frozen=True)
class PaymentReceiptAttachmentAssessment:
    route: str
    reply_ar: str
    state_patch: Dict[str, Any]
    reason: str
    duplicate: bool = False


def assess_payment_receipt_attachment(
    *,
    inbound_normalized_type: str,
    inbound_metadata: Optional[Dict[str, Any]],
    summary: Optional[Dict[str, Any]],
) -> Optional[PaymentReceiptAttachmentAssessment]:
    """Return receipt-received routing when attachment metadata is strong enough."""
    s = summary or {}
    if not payment_context_active(s):
        return None
    if not has_inbound_attachment(inbound_normalized_type, inbound_metadata):
        return None

    if bool(s.get("payment_receipt_received")):
        return PaymentReceiptAttachmentAssessment(
            route=ROUTE_PAYMENT_RECEIPT_RECEIVED,
            reply_ar=PAYMENT_RECEIPT_DUPLICATE_ACK_AR,
            state_patch={},
            reason="duplicate_receipt_attachment",
            duplicate=True,
        )

    if not is_likely_payment_receipt_attachment(
        inbound_normalized_type,
        inbound_metadata,
        summary=s,
    ):
        return None

    patch = build_receipt_received_state_patch(
        inbound_metadata=inbound_metadata,
        source="attachment_metadata",
    )
    logger.info(
        "[PAYMENT_RECEIPT_ATTACHMENT] route=%s reason=metadata_attachment "
        "product=%r awaiting_was=%s",
        ROUTE_PAYMENT_RECEIPT_RECEIVED,
        s.get("selected_product"),
        bool(s.get("awaiting_payment_receipt")),
    )
    return PaymentReceiptAttachmentAssessment(
        route=ROUTE_PAYMENT_RECEIPT_RECEIVED,
        reply_ar=PAYMENT_RECEIPT_ATTACHMENT_ACK_AR,
        state_patch=patch,
        reason="attachment_metadata_gate",
    )


def try_metadata_receipt_short_circuit(
    *,
    inbound_normalized_type: str,
    inbound_metadata: Optional[Dict[str, Any]],
    summary: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return order_flow short-circuit payload or None."""
    assessment = assess_payment_receipt_attachment(
        inbound_normalized_type=inbound_normalized_type,
        inbound_metadata=inbound_metadata,
        summary=summary,
    )
    if assessment is None:
        return None
    return {
        "reply_text": assessment.reply_ar,
        "summary": dict(summary or {}),
        "state_patch": dict(assessment.state_patch),
        "route": assessment.route,
        "duplicate": assessment.duplicate,
    }


__all__ = [
    "PAYMENT_RECEIPT_ATTACHMENT_ACK_AR",
    "PAYMENT_RECEIPT_DUPLICATE_ACK_AR",
    "ROUTE_PAYMENT_RECEIPT_RECEIVED",
    "PaymentReceiptAttachmentAssessment",
    "assess_payment_receipt_attachment",
    "build_receipt_received_state_patch",
    "filename_suggests_receipt",
    "has_inbound_attachment",
    "is_likely_payment_receipt_attachment",
    "payment_context_active",
    "reply_asks_to_send_receipt",
    "try_metadata_receipt_short_circuit",
]
