"""
modules/ai/brain/postprocess/payment_reply_guard.py
───────────────────────────────────────────────────
Block false receipt/payment confirmation wording when structured
payment evidence is missing, classifiers rejected the inbound, or the
customer only promised a future transfer — not a completed one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from modules.ai.brain.postprocess.payment_evidence import evaluate_payment_evidence

logger = logging.getLogger("nahla.brain.postprocess.payment_reply_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

REJECTED_EVIDENCE_REPLY_AR = (
    "وصلني الملف، لكن لا أقدر أؤكد التحويل من هذه النسخة. "
    "فضلاً أرسل صورة أوضح للإيصال أو اكتب مبلغ التحويل واسم المحوّل."
)

FUTURE_TRANSFER_REPLY_AR = (
    "تمام، بعد التحويل أرسل الإيصال هنا عشان نراجعه ونكمل الطلب 🌷"
)

TEXT_CLAIM_NO_EVIDENCE_REPLY_AR = (
    "إذا أرسلت الإيصال أو صورة التحويل أراجعها لك مباشرة 🌷"
)

_RECEIPT_CONFIRMATION_REPLY_MARKERS = (
    "وصل الإيصال",
    "وصلنا إيصال التحويل",
    "تم استلام الإيصال",
    "تم استلام التحويل",
    "تم تأكيد الدفع",
    "تم استلام المبلغ",
    "تم التحقق من الحوالة",
    "سيتم تجهيز الطلب",
    "تم استلام الطلب",
    "وصلنا إيصال",
    "استلمنا الإيصال",
    "تم التحقق من التحويل",
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
    "بعد شوي احول",
    "بعد شوي بحول",
    "بعد قليل احول",
)

_NAME_SLOT_KEYS = frozenset({
    "customer_name",
    "customer_first_name",
    "customer_last_name",
})

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
    markers = (_norm(marker) for marker in _RECEIPT_CONFIRMATION_REPLY_MARKERS)
    return any(marker in norm for marker in markers)


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
    if "بعد" in norm and ("شوي" in norm or "قليل" in norm):
        if "احول" in norm or "بحول" in norm or "حول" in norm:
            return True
    if ("احول" in norm or "بحول" in norm) and (
        "الان" in norm or "الحين" in norm or "ارسل" in norm or "لك" in norm
    ):
        return True
    return False


def strip_customer_name_slots_when_future_transfer(
    message: str,
    slots: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Drop name slots when the inbound is a future-transfer promise."""
    out = dict(slots or {})
    if not detect_future_transfer_intent(message):
        return out
    for key in _NAME_SLOT_KEYS:
        out.pop(key, None)
    return out


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


def rejected_media_requires_soft_rejection(
    metadata: Optional[Dict[str, Any]],
) -> tuple[bool, str]:
    """Use the rejected-document reply only for current-turn media rejection."""
    blocked, reason = inbound_metadata_blocks_receipt_confirmation(metadata)
    if not blocked:
        return False, ""
    md = metadata or {}
    has_media = bool(str(md.get("pdf_kind") or md.get("image_kind") or "").strip())
    if has_media:
        return True, reason
    if str(md.get("payment_evidence_reason") or "").strip() == "semantic_rejected_document":
        return True, reason
    return False, ""


def payment_evidence_allows_receipt_ack(
    metadata: Optional[Dict[str, Any]],
    *,
    payment_receipt_received: bool = False,
    inbound_text: str = "",
    chosen_path: str = "",
) -> bool:
    """Allow receipt-confirmation wording only on confirmed structured evidence."""
    return evaluate_payment_evidence(
        inbound_metadata=metadata,
        chosen_path=chosen_path,
        inbound_text=inbound_text,
        payment_receipt_received=payment_receipt_received,
    ).evidence_ok


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
    evidence_source: str = "",
) -> None:
    try:
        logger.info(
            "[PAYMENT_REPLY_GUARD] tenant_id=%s conversation_id=%s "
            "payment_evidence_status=%s pdf_kind=%s image_kind=%s "
            "evidence_source=%s reason=%s action=%s",
            tenant_id,
            conversation_id,
            payment_evidence_status or "-",
            pdf_kind or "-",
            image_kind or "-",
            evidence_source or "-",
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
        path = str(chosen_path or "").strip()

        if not original.strip():
            return PaymentReplyGuardResult(reply=original, action="allowed")
        if path in _DETERMINISTIC_ALLOW_PATHS:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                evidence_source="deterministic_path",
                reason="deterministic_path",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        has_receipt_wording = reply_contains_receipt_confirmation(original)
        future_intent = detect_future_transfer_intent(inbound_text)
        attachment_summary = {
            "awaiting_payment_receipt": bool(md.get("awaiting_payment_receipt")),
            "payment_receipt_received": payment_receipt_received
            or bool(md.get("payment_receipt_received")),
            "selected_product": md.get("selected_product"),
            "order_status": md.get("order_status"),
            "payment_method": md.get("payment_method"),
        }
        evidence_md = dict(md)
        evidence_md.setdefault(
            "awaiting_payment_receipt",
            attachment_summary["awaiting_payment_receipt"],
        )
        evidence_md.setdefault(
            "payment_receipt_received",
            attachment_summary["payment_receipt_received"],
        )
        evidence = evaluate_payment_evidence(
            inbound_metadata=evidence_md,
            chosen_path=path,
            inbound_text=inbound_text,
            payment_receipt_received=payment_receipt_received,
        )

        try:
            from core.payment_receipt_attachment_gate import (  # noqa: PLC0415
                PAYMENT_RECEIPT_DUPLICATE_ACK_AR,
                has_inbound_attachment,
                is_likely_payment_receipt_attachment,
                reply_asks_to_send_receipt,
            )
            from core.payment_receipt_submission import parse_inbound_receipt  # noqa: PLC0415

            attachment_present = has_inbound_attachment(
                str(md.get("normalized_type") or md.get("inbound_type") or ""),
                md,
            )
            if attachment_present and is_likely_payment_receipt_attachment(
                str(md.get("normalized_type") or md.get("inbound_type") or ""),
                md,
                summary=attachment_summary,
            ):
                ack = (
                    PAYMENT_RECEIPT_DUPLICATE_ACK_AR
                    if payment_receipt_received
                    else parse_inbound_receipt(md).reply_ar
                )
                if (
                    not evidence.evidence_ok
                    or reply_asks_to_send_receipt(original)
                    or original.strip() == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR
                    or (has_receipt_wording and not payment_receipt_received)
                ):
                    log_payment_reply_guard(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        payment_evidence_status=pe,
                        pdf_kind=pdf_kind,
                        image_kind=image_kind,
                        evidence_source="attachment_metadata",
                        reason="attachment_present_ack",
                        action="blocked_receipt_confirmation",
                    )
                    return PaymentReplyGuardResult(
                        reply=ack,
                        action="blocked_receipt_confirmation",
                        replaced=True,
                        reason="attachment_present_ack",
                    )

            if payment_receipt_received and reply_asks_to_send_receipt(original):
                log_payment_reply_guard(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    payment_evidence_status=pe,
                    pdf_kind=pdf_kind,
                    image_kind=image_kind,
                    evidence_source=evidence.evidence_source,
                    reason="receipt_already_received",
                    action="blocked_receipt_confirmation",
                )
                return PaymentReplyGuardResult(
                    reply=PAYMENT_RECEIPT_DUPLICATE_ACK_AR,
                    action="blocked_receipt_confirmation",
                    replaced=True,
                    reason="receipt_already_received",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — attachment gate must not break guard
            pass

        if not has_receipt_wording:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                evidence_source=evidence.evidence_source,
                reason="no_receipt_wording_in_reply",
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        if evidence.evidence_ok:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                evidence_source=evidence.evidence_source,
                reason=evidence.reason,
                action="allowed",
            )
            return PaymentReplyGuardResult(reply=original, action="allowed")

        if future_intent:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                evidence_source=evidence.evidence_source,
                reason="future_transfer_intent",
                action="blocked_receipt_confirmation",
            )
            return PaymentReplyGuardResult(
                reply=FUTURE_TRANSFER_REPLY_AR,
                action="blocked_receipt_confirmation",
                replaced=True,
                reason="future_transfer_intent",
            )

        rejected, reject_reason = rejected_media_requires_soft_rejection(md)
        if rejected:
            log_payment_reply_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                payment_evidence_status=pe,
                pdf_kind=pdf_kind,
                image_kind=image_kind,
                evidence_source=evidence.evidence_source,
                reason=reject_reason,
                action="blocked_receipt_confirmation",
            )
            return PaymentReplyGuardResult(
                reply=REJECTED_EVIDENCE_REPLY_AR,
                action="blocked_receipt_confirmation",
                replaced=True,
                reason=reject_reason,
            )

        log_payment_reply_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            payment_evidence_status=pe,
            pdf_kind=pdf_kind,
            image_kind=image_kind,
            evidence_source=evidence.evidence_source,
            reason=evidence.reason,
            action="blocked_receipt_confirmation",
        )
        return PaymentReplyGuardResult(
            reply=TEXT_CLAIM_NO_EVIDENCE_REPLY_AR,
            action="blocked_receipt_confirmation",
            replaced=True,
            reason=evidence.reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PAYMENT_REPLY_GUARD] guard failed err=%s", exc)
        return PaymentReplyGuardResult(reply=str(reply or ""), action="allowed")


__all__ = [
    "PaymentReplyGuardResult",
    "REJECTED_EVIDENCE_REPLY_AR",
    "FUTURE_TRANSFER_REPLY_AR",
    "TEXT_CLAIM_NO_EVIDENCE_REPLY_AR",
    "apply_payment_reply_guard",
    "detect_future_transfer_intent",
    "inbound_metadata_blocks_receipt_confirmation",
    "log_payment_reply_guard",
    "payment_evidence_allows_receipt_ack",
    "reply_contains_receipt_confirmation",
    "strip_customer_name_slots_when_future_transfer",
]
