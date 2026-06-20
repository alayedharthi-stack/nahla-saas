"""
core/payment_receipt_submission.py
────────────────────────────────────
Receipt submission state + optional text parse for bank-transfer attachments.

Separates receipt received, receipt parsed, payment submitted, and merchant
verification. Never auto-confirms payment or enables shipping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.bank_transfer_receipt_resolver import (
    build_receipt_data,
    extract_bank_receipt_fields,
)
from core.payment_media_metadata import flatten_inbound_payment_metadata, payment_text_blob

PAYMENT_VERIFICATION_PENDING_MERCHANT_REVIEW = "pending_merchant_review"
SHIPPING_BLOCKED_PAYMENT_PENDING_MERCHANT_VERIFICATION = (
    "payment_pending_merchant_verification"
)

PAYMENT_RECEIPT_UNPARSED_ACK_AR = (
    "وصل الإيصال، أراجعه لك وأأكد الطلب بعد التحقق من التحويل."
)

PAYMENT_RECEIPT_PARSED_ACK_AR = (
    "وصل الإيصال وتم تسجيل الطلب، وبانتظار مراجعة التحويل من التاجر قبل الشحن."
)


def compose_parsed_receipt_ack(*, amount: str = "", currency: str = "SAR") -> str:
    amt = str(amount or "").strip()
    if amt:
        unit = "ريال" if str(currency or "SAR").upper() == "SAR" else currency
        return (
            f"وصل الإيصال وتم تسجيل الطلب بمبلغ {amt} {unit}، "
            "وبانتظار مراجعة التحويل من التاجر قبل الشحن."
        )
    return PAYMENT_RECEIPT_PARSED_ACK_AR


def _mask_iban(iban: str) -> Optional[str]:
    raw = str(iban or "").replace(" ", "").upper()
    if len(raw) < 8:
        return raw or None
    return f"{raw[:4]}****{raw[-4:]}"


@dataclass(frozen=True)
class ParsedReceiptResult:
    parsed: bool
    fields: Dict[str, Any] = field(default_factory=dict)
    parse_errors: List[str] = field(default_factory=list)
    raw_text: str = ""
    reply_ar: str = PAYMENT_RECEIPT_UNPARSED_ACK_AR


def parse_inbound_receipt(
    metadata: Optional[Dict[str, Any]],
) -> ParsedReceiptResult:
    """Extract receipt fields when OCR/PDF/vision text is available."""
    md = flatten_inbound_payment_metadata(metadata or {})
    blob = payment_text_blob(md).strip()
    if not blob:
        return ParsedReceiptResult(
            parsed=False,
            parse_errors=["no_extractable_text"],
            reply_ar=PAYMENT_RECEIPT_UNPARSED_ACK_AR,
        )

    extraction = extract_bank_receipt_fields(
        blob,
        filename=str(md.get("filename") or md.get("document_filename") or ""),
    )
    errors: List[str] = []
    if extraction.receipt_type == "pre_transfer_review":
        errors.append("pre_transfer_review_screen")
        return ParsedReceiptResult(
            parsed=False,
            fields=_build_parsed_fields(extraction, blob, errors),
            parse_errors=errors,
            raw_text=blob[:4000],
            reply_ar=PAYMENT_RECEIPT_UNPARSED_ACK_AR,
        )

    parsed = _is_receipt_clearly_parsed(extraction)
    if not parsed:
        if extraction.confidence < 0.4:
            errors.append("low_confidence")
        if not extraction.amount:
            errors.append("amount_missing")

    fields = _build_parsed_fields(extraction, blob, errors)
    reply = (
        compose_parsed_receipt_ack(
            amount=str(extraction.amount or ""),
            currency=str(extraction.currency or "SAR"),
        )
        if parsed
        else PAYMENT_RECEIPT_UNPARSED_ACK_AR
    )
    return ParsedReceiptResult(
        parsed=parsed,
        fields=fields,
        parse_errors=errors,
        raw_text=blob[:4000],
        reply_ar=reply,
    )


def _is_receipt_clearly_parsed(extraction: Any) -> bool:
    if getattr(extraction, "receipt_type", "") == "final_receipt":
        return True
    if getattr(extraction, "has_pre_review_imperative", False):
        return False
    confidence = float(getattr(extraction, "confidence", 0.0) or 0.0)
    amount = str(getattr(extraction, "amount", "") or "").strip()
    bank = str(getattr(extraction, "bank_name", "") or "").strip()
    reference = str(getattr(extraction, "reference_number", "") or "").strip()
    if confidence >= 0.55 and amount:
        return True
    if confidence >= 0.4 and amount and (bank or reference):
        return True
    return False


def _build_parsed_fields(
    extraction: Any,
    raw_text: str,
    parse_errors: List[str],
) -> Dict[str, Any]:
    receipt_data = build_receipt_data(extraction)
    iban = str(getattr(extraction, "beneficiary_iban", "") or "")
    return {
        "amount": receipt_data.get("amount"),
        "currency": receipt_data.get("currency") or "SAR",
        "transfer_date": receipt_data.get("transfer_datetime"),
        "bank_name": receipt_data.get("bank_name"),
        "beneficiary_name": receipt_data.get("beneficiary_name"),
        "account_iban_masked": _mask_iban(iban),
        "account_last_digits": receipt_data.get("account_last_digits"),
        "reference_number": receipt_data.get("reference_number"),
        "receipt_type": receipt_data.get("receipt_type"),
        "confidence": float(getattr(extraction, "confidence", 0.0) or 0.0),
        "parse_errors": list(parse_errors),
        "raw_text_preview": raw_text[:500] if raw_text else None,
        "bank_receipt_extraction": extraction.to_dict(),
        "receipt_data": receipt_data,
    }


def build_payment_submitted_state_patch(
    *,
    inbound_metadata: Optional[Dict[str, Any]],
    source: str = "attachment_metadata",
    parse_result: Optional[ParsedReceiptResult] = None,
) -> Dict[str, Any]:
    """Persist receipt submission without payment verification or shipping unlock."""
    md = flatten_inbound_payment_metadata(inbound_metadata or {})
    parsed = parse_result or parse_inbound_receipt(md)
    now_iso = datetime.now(timezone.utc).isoformat()
    kind = str(md.get("pdf_kind") or md.get("image_kind") or "payment_receipt").strip()

    patch: Dict[str, Any] = {
        "payment_method": "bank_transfer",
        "payment_status": "pending_verification",
        "awaiting_payment_receipt": False,
        "payment_receipt_received": True,
        "payment_receipt_parsed": bool(parsed.parsed),
        "payment_receipt_at": now_iso,
        "payment_confirmed": False,
        "payment_verification_status": PAYMENT_VERIFICATION_PENDING_MERCHANT_REVIEW,
        "payment_submission_received": True,
        "payment_submission_at": now_iso,
        "payment_submission_type": "receipt",
        "payment_submission_source": "whatsapp",
        "order_status": "payment_submitted",
        "manual_verification_required": True,
        "shipping_blocked_reason": SHIPPING_BLOCKED_PAYMENT_PENDING_MERCHANT_VERIFICATION,
        "payment_receipt_metadata": {
            "kind": kind or "payment_receipt",
            "source": source,
            "route": "PAYMENT_RECEIPT_RECEIVED",
            "wa_message_id": md.get("wa_message_id"),
            "filename": md.get("filename"),
            "mime_type": md.get("mime_type"),
            "storage_url": md.get("storage_url"),
            "storage_sha256": md.get("storage_sha256"),
            "received_at": now_iso,
            "manual_verification_required": True,
            "payment_receipt_parsed": bool(parsed.parsed),
            "parsed_receipt_fields": dict(parsed.fields),
            "parse_errors": list(parsed.parse_errors),
        },
    }
    if parsed.raw_text:
        patch["payment_receipt_metadata"]["raw_text_preview"] = parsed.raw_text[:500]

    for key in (
        "vision_text",
        "ocr_text",
        "pdf_text_preview",
        "pdf_text_full",
        "caption",
        "bank_receipt_extraction",
        "receipt_data",
    ):
        val = md.get(key) or parsed.fields.get(key)
        if val not in (None, ""):
            patch["payment_receipt_metadata"][key] = val

    if parsed.fields.get("bank_receipt_extraction"):
        patch["payment_receipt_metadata"]["bank_receipt_extraction"] = (
            parsed.fields["bank_receipt_extraction"]
        )
    if parsed.fields.get("receipt_data"):
        patch["payment_receipt_metadata"]["receipt_data"] = parsed.fields["receipt_data"]

    return patch


__all__ = [
    "PAYMENT_RECEIPT_PARSED_ACK_AR",
    "PAYMENT_RECEIPT_UNPARSED_ACK_AR",
    "PAYMENT_VERIFICATION_PENDING_MERCHANT_REVIEW",
    "ParsedReceiptResult",
    "SHIPPING_BLOCKED_PAYMENT_PENDING_MERCHANT_VERIFICATION",
    "build_payment_submitted_state_patch",
    "compose_parsed_receipt_ack",
    "parse_inbound_receipt",
]
