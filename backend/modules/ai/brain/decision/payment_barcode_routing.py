"""
Payment barcode image request detection + outbound media routing.

Distinguishes explicit barcode/QR/image asks (``PAYMENT_BARCODE_IMAGE_REQUEST``)
from generic payment-info questions (``ask_payment_info``). The webhook uses
this to queue ``payment_rajhi_barcode`` (or the tenant's single payment
barcode) before text-only artifact fallbacks run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.payment_barcode_routing")

# Distinct from generic ``ask_payment_info`` — customer wants the image/QR asset.
PAYMENT_BARCODE_IMAGE_REQUEST = "payment_barcode_image_request"
ASK_PAYMENT_INFO = "ask_payment_info"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Explicit barcode / QR / "send the image" phrasing only — not bare IBAN/transfer.
_BARCODE_IMAGE_REQUEST_RE = re.compile(
    r"("
    r"الباركود|باركود|بار\s*كود|"
    r"\bqr\b|qr\s*code|"
    r"كيو\s*ار|كيو\s*آر|كيوار|"
    r"صورة\s*(?:ال)?(?:باركود|تحويل|qr|كيو\s*ار)|"
    r"(?:ارسل|أرسل|ابعث|ابعت|ابي|أبي|ابغى|أبغى|ودي|حاب)\s*"
    r"(?:لي\s+|لـ?\s*)?(?:صورة\s*)?(?:باركود|الباركود|qr|كيو\s*ار|"
    r"باركود\s*الراجحي|باركود\s*الأهلي|باركود\s*الاهلي)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_PHONE_ONLY_REPLY_MARKERS: tuple[str, ...] = (
    "المتوفر حالياً رقم التحويل",
    "المتوفر حاليا رقم التحويل",
    "رقم التحويل",
    "ما عندي بيانات الحساب",
    "ما عندي معلومات",
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = _NORM_RE.sub("", str(text).lower())
    return _WS_RE.sub(" ", t).strip()


def is_payment_barcode_image_request(message: str) -> bool:
    """True when the customer is asking for a payment barcode/QR *image*."""
    if not (message or "").strip():
        return False
    return bool(_BARCODE_IMAGE_REQUEST_RE.search(_norm(message)))


def classify_payment_request(message: str) -> str:
    if is_payment_barcode_image_request(message):
        return PAYMENT_BARCODE_IMAGE_REQUEST
    try:
        from core.ai_libraries import is_payment_query  # noqa: PLC0415
        if is_payment_query(message):
            return ASK_PAYMENT_INFO
    except Exception:  # noqa: BLE001
        pass
    return ""


def _reply_looks_like_phone_only_fallback(reply: str) -> bool:
    if not (reply or "").strip():
        return True
    norm = _norm(reply)
    return any(_norm(m) in norm for m in _PHONE_ONLY_REPLY_MARKERS)


@dataclass
class PaymentBarcodeRouteResult:
    request_kind: str = ""
    barcode_request_detected: bool = False
    asset_found: bool = False
    media_key: str = ""
    attachment: Optional[Dict[str, Any]] = None
    media_send_attempted: bool = False
    fallback_used: bool = False
    queued_attachment: bool = False
    rewrote_reply: bool = False
    skipped_reason: str = ""


def _attachment_already_present(
    media_attachments: Sequence[Any],
    media_key: str,
) -> bool:
    key = (media_key or "").strip().lower()
    for att in media_attachments or []:
        if not isinstance(att, dict):
            continue
        if (att.get("media_key") or "").strip().lower() == key:
            return True
        if key and key in str(att.get("title") or "").lower():
            return True
    return False


def apply_payment_barcode_image_route(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    media_attachments: List[Dict[str, Any]],
    reply_text: str,
    conversation_id: Optional[int] = None,
) -> PaymentBarcodeRouteResult:
    """Queue outbound barcode media before text-only payment fallbacks.

    Runs after marker extraction / media-key safety net and *before*
    ``apply_outbound_artifact_guard`` so a phone-number prose reply
    cannot preempt the image send.
    """
    result = PaymentBarcodeRouteResult()
    if not is_payment_barcode_image_request(customer_msg):
        result.skipped_reason = "not_barcode_image_request"
        return result

    result.barcode_request_detected = True
    result.request_kind = PAYMENT_BARCODE_IMAGE_REQUEST

    try:
        from services.media_resolver import resolve_for_query  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        result.skipped_reason = f"import_failure:{exc}"
        result.fallback_used = True
        log_payment_barcode_decision(tenant_id=tenant_id, result=result, conversation_id=conversation_id)
        return result

    try:
        resolution, inferred_key = resolve_for_query(db, tenant_id, customer_msg or "")
    except Exception as exc:  # noqa: BLE001
        result.skipped_reason = f"resolve_failed:{exc}"
        result.fallback_used = True
        log_payment_barcode_decision(tenant_id=tenant_id, result=result, conversation_id=conversation_id)
        return result

    result.media_key = (inferred_key or "").strip()
    if not resolution:
        result.asset_found = False
        result.fallback_used = True
        result.skipped_reason = "asset_missing"
        log_payment_barcode_decision(tenant_id=tenant_id, result=result, conversation_id=conversation_id)
        return result

    result.asset_found = True
    attachment = resolution.to_attachment()
    attachment["safety_net"] = True
    attachment["payment_barcode_route"] = True
    result.attachment = attachment

    if not _attachment_already_present(media_attachments, result.media_key):
        media_attachments.append(attachment)
        result.queued_attachment = True
        result.media_send_attempted = True

    if _reply_looks_like_phone_only_fallback(reply_text) or not (reply_text or "").strip():
        result.rewrote_reply = True
        result.fallback_used = False

    log_payment_barcode_decision(
        tenant_id=tenant_id,
        result=result,
        conversation_id=conversation_id,
    )
    return result


def payment_barcode_intro_text(media_key: str = "") -> str:
    key = (media_key or "").lower()
    if "rajhi" in key or "راجح" in key:
        return "أكيد 🌷 تفضل، هذا باركود التحويل للراجحي."
    return "أكيد 🌷 تفضل، هذا باركود التحويل."


def log_payment_barcode_decision(
    *,
    tenant_id: int,
    result: PaymentBarcodeRouteResult,
    conversation_id: Optional[int] = None,
) -> None:
    logger.info(
        "[PAYMENT_BARCODE] tenant=%s conversation_id=%s "
        "barcode_request_detected=%s asset_found=%s media_key=%s "
        "media_send_attempted=%s fallback_used=%s queued_attachment=%s "
        "skipped_reason=%s",
        tenant_id,
        conversation_id if conversation_id is not None else "-",
        str(result.barcode_request_detected).lower(),
        str(result.asset_found).lower(),
        result.media_key or "-",
        str(result.media_send_attempted).lower(),
        str(result.fallback_used).lower(),
        str(result.queued_attachment).lower(),
        result.skipped_reason or "-",
    )
