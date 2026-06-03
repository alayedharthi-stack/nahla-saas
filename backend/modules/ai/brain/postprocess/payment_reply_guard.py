"""
modules/ai/brain/postprocess/payment_reply_guard.py
───────────────────────────────────────────────────
Block false receipt/payment confirmation wording when classifiers
rejected the inbound evidence or the customer only promised a future
transfer — not a completed one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.postprocess.payment_reply_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

REJECTED_EVIDENCE_REPLY_AR = (
    "وصلني الملف، لكن لا أقدر أؤكد التحويل من هذه النسخة. "
    "فضلاً أرسل صورة أوضح للإيصال أو اكتب مبلغ التحويل واسم المحوّل."
)

FUTURE_TRANSFER_REPLY_AR = (
    "تمام، بعد التحويل أرسل الإيصال هنا عشان نراجعه ونكمل الطلب."
)

_RECEIPT_CONFIRMATION_REPLY_MARKERS = (
    "وصل الايصال",
    "وصلنا ايصال التحويل",
    "تم استلام الايصال",
    "تم استلام التحويل",
    "تم تأكيد الدفع",
    "سيتم تجهيز الطلب",
    "تم استلام الطلب",
    "وصلنا ايصال",
    "استلمنا الايصال",
    "تم التحقق من التحويل",
    "وسيتم متابعه الطلب",
    "وسيتم متابعة الطلب",
    "وتجهيزه",
)

_FUTURE_TRANSFER_PHRASES = (
    "احول لك الان",
    "انا احول لك",
    "انا احول الان",
    "بحول لك",
    "بحول الان",
    "احول وارسل",
    "احول والارسل",
    "راح احول",
    "ساحول",
    "انا بدفع الحين",
    "بدفع الحين",
    "بسرع وقت ارسل",
    "في اسرع وقت ارسل",
)

_PAST_TRANSFER_MARKERS = (
    "تم التحويل",
    "تم الدفع",
    "\u062d\u0648\u0644\u062a \u0644\u0643",
    "\u062d\u0648\u0644\u062a \u0627\u0644\u0645\u0628\u0644\u063a",
    "\u062f\u0641\u0639\u062a",
    "\u0633\u062f\u062f\u062a",
)

_DETERMINISTIC_ALLOW_PATHS = frozenset({
    "payment_receipt_ack",
    "payment_claim_ack",
    "payment_evidence_soft_ack",
    "variant_pricing",
})


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


def reply_contains_receipt_confirmation(reply: Optional[str]) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    return any(marker in norm for marker in _RECEIPT_CONFIRMATION_REPLY_MARKERS)


def detect_future_transfer_intent(text: Optional[str]) -> bool:
    if not text or not isinstance(text, str):
        return False
    try:
        from core.payment_intent import detect_payment_confirmation_text  # noqa: PLC0415

        if detect_payment_confirmation_text(text):
            return False
    except Exception:  # noqa: BLE001
        pass
    norm = _norm(text)
    if not norm:
        return False
    if any(p in norm for p in _PAST_TRANSFER_MARKERS):
        return False
    for phrase in _FUTURE_TRANSFER_PHRASES:
        if phrase in norm:
            return True
    if ("احول" in norm or "بحول" in norm) and (
        "الان" in norm or "الحين" in norm or "ارسل" in norm
    ):
        return True
    return False


def inbound_metadata_blocks_receipt_confirmation(
    metadata: Optional[Dict[str, Any]],
) -> tuple[bool, str]:
    md = metadata or {}
    pe = str(md.get("payment_evidence_status") or "").strip()
    if pe == "not_payment":
        return True, "payment_evidence_status=not_payment"
    reason = str(md.get("payment_evidence_reason") or "").strip()
    if reason == "semantic_rejected_document":
        return True, "payment_evidence_reason=semantic_rejected_document"
    pdf_kind = str(md.get("pdf_kind") or "").strip()
    if pdf_kind == "payment_pending_evidence":
        return True, "pdf_kind=payment_pending_evidence"
    image_kind = str(md.get("image_kind") or "").strip()
    if image_kind == "payment_pending_evidence":
        return True, "image_kind=payment_pending_evidence"
    return False, ""


def payment_evidence_allows_receipt_ack(
    metadata: Optional[Dict[str, Any]],
    *,
    payment_receipt_received: bool = False,
) -> bool:
    if payment_receipt_received:
        return True
    md = metadata or {}
    if str(md.get("payment_evidence_status") or "").strip() == "confirmed":
        return True
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "").strip()
    pe = str(md.get("payment_evidence_status") or "").strip()
    return kind == "payment_receipt" and pe == "confirmed"


@dataclass(frozen=True)
class PaymentReplyGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""


def log_payment_reply_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    payment_evidence_status: str,
    pdf_kind: str,
    image_kind: str,
    reason: str,
    action: str,
) -> None:
    try:
        logger.info(
            "[PAYMENT_REPLY_GUARD] tenant_id=%s conversation_id=%s "
            "payment_evidence_status=%s pdf_kind=%s image_kind=%s "
            "reason=%s action=%s",
            tenant_id,
            conversation_id,
            payment_evidence_status or "-",
            pdf_kind or "-",
            image_kind or "-",
            reason or "-",
            action,
        )
    except Exception:  # noqa: BLE001
        pass


def apply_payment_reply_guard(
    *,
    reply: str,
    inbound_text: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
    payment_receipt_received: bool = False,
    chosen_path: str = "",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> PaymentReplyGuardResult:
    try:
        original = str(reply or "")
        md = inbound_metadata or {}
        pe = str(md.get("payment_evidence_status") or "").strip()
        pdf_kind = str(md.get("pdf_kind") or "").strip()
        image_kind = str(md.get("image_kind") or "").strip()

        if not original.strip():
            return PaymentReplyGuardResult(reply=original, action="allowed")
        if str(chosen_path or "").strip() in _DETERMINISTIC_ALLOW_PATHS:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                reason="deterministic_path",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        if payment_evidence_allows_receipt_ack(
            md, payment_receipt_received=payment_receipt_received,
        ):
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                reason="confirmed_evidence",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        if not reply_contains_receipt_confirmation(original):
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                reason="no_receipt_wording_in_reply",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        blocked, block_reason = inbound_metadata_blocks_receipt_confirmation(md)
        future_intent = detect_future_transfer_intent(inbound_text)
        if not blocked and not future_intent:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                reason="no_block_signal",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        replacement = (
            REJECTED_EVIDENCE_REPLY_AR if blocked else FUTURE_TRANSFER_REPLY_AR
        )
        reason = block_reason if blocked else "future_transfer_intent"
        log_payment_reply_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            payment_evidence_status=pe,
            pdf_kind=pdf_kind,
            image_kind=image_kind,
            reason=reason,
            action="blocked_receipt_confirmation",
        )
        return PaymentReplyGuardResult(
            reply=replacement,
            action="blocked_receipt_confirmation",
            replaced=True,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PAYMENT_REPLY_GUARD] guard failed err=%s", exc)
        return PaymentReplyGuardResult(reply=str(reply or ""), action="allowed")


__all__ = [
    "PaymentReplyGuardResult",
    "REJECTED_EVIDENCE_REPLY_AR",
    "FUTURE_TRANSFER_REPLY_AR",
    "apply_payment_reply_guard",
    "detect_future_transfer_intent",
    "inbound_metadata_blocks_receipt_confirmation",
    "log_payment_reply_guard",
    "payment_evidence_allows_receipt_ack",
    "reply_contains_receipt_confirmation",
]
