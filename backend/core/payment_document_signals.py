"""
payment_document_signals.py
───────────────────────────
Platform-wide hints for e-wallet / transfer PDFs (MobilyPay, STC Pay, …).

Used by normalizer metadata promotion, payment turn routing, and catalog guards.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Clear wallet / payment providers — sufficient on their own in filename/text.
_WALLET_PROVIDER_HINTS = (
    "mobilypay",
    "mobily pay",
    "mobily-pay",
    "stcpay",
    "stc pay",
)

# Strong labelled transfer-receipt phrases (never match bare ``transaction`` alone).
_STRONG_TEXT_HINTS = (
    "mobilypay",
    "mobily pay",
    "transaction number",
    "number transaction",
    "amount transfer",
    "amount transfer:",
    "receiver name",
    "receiver:",
    "transfer to contact",
    "transfer to contacts",
    "مبلغ التحويل",
    "رقم العملية",
    "تحويل لجهات الاتصال",
    "تحويل لجهة الاتصال",
)

# Weaker hints — only count alongside an amount or a wallet provider.
_WEAK_TEXT_HINTS = (
    "transfer",
    "payment",
    " pay",
    "receiver",
    "wallet",
    "iban",
    "beneficiary",
    "تحويل",
    "ايصال",
    "إيصال",
    "حوال",
)

_FILENAME_RECEIPT_HINTS = (
    "transfer",
    "receipt",
    "payment",
    "pay",
    "iban",
    "hawala",
    "تحويل",
    "ايصال",
    "إيصال",
    "حوال",
)

_TRANSACTION_FILENAME_SUPPORT = (
    "transfer",
    "pay",
    "payment",
    "receipt",
    "mobily",
    "stc",
    "wallet",
)

_AMOUNT_RE = re.compile(
    r"(?:\d+(?:[.,]\d{1,2})?\s*(?:ر\.?\s*س|ريال|sar)\b|"
    r"\b(?:sar|ريال)\s*\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)


def _norm_blob(*parts: Optional[str]) -> str:
    return " ".join(p for p in (parts or ()) if p).lower()


def _has_wallet_provider(blob: str) -> bool:
    return any(h in blob for h in _WALLET_PROVIDER_HINTS)


def filename_suggests_transfer_receipt(filename: Optional[str]) -> bool:
    """Filename signal — bare ``transaction`` is never sufficient alone."""
    fn = _norm_blob(filename)
    if not fn:
        return False
    if _has_wallet_provider(fn):
        return True
    if "transaction" in fn:
        return any(h in fn for h in _TRANSACTION_FILENAME_SUPPORT)
    return any(h in fn for h in _FILENAME_RECEIPT_HINTS)


def text_suggests_transfer_receipt(text: Optional[str]) -> bool:
    """Body/caption signal — ``transaction`` alone does not qualify."""
    blob = _norm_blob(text)
    if not blob:
        return False
    if _has_wallet_provider(blob):
        return True
    if any(h in blob for h in _STRONG_TEXT_HINTS):
        return True
    has_amount = bool(_AMOUNT_RE.search(blob))
    if has_amount and any(h in blob for h in _WEAK_TEXT_HINTS):
        return True
    if has_amount and "transaction" in blob:
        return True
    return False


def metadata_has_receipt_amount(md: Optional[Dict[str, Any]]) -> bool:
    meta = dict(md or {})
    receipt = meta.get("receipt_data")
    if isinstance(receipt, dict) and receipt.get("amount") not in (None, ""):
        return True
    hints = meta.get("payment_evidence_hints")
    if isinstance(hints, dict) and hints.get("amount") not in (None, ""):
        return True
    return False


def metadata_has_potential_payment_document(
    md: Optional[Dict[str, Any]],
    *,
    text: Optional[str] = None,
) -> bool:
    """True when inbound looks like a transfer receipt / e-wallet PDF."""
    meta = dict(md or {})
    if metadata_has_receipt_amount(meta):
        return True
    if filename_suggests_transfer_receipt(str(meta.get("filename") or "")):
        return True
    blob = text or ""
    for key in (
        "pdf_text_preview",
        "pdf_text_full",
        "pdf_full_text",
        "vision_text",
        "ocr_text",
    ):
        blob = f"{blob}\n{meta.get(key) or ''}"
    if text_suggests_transfer_receipt(blob):
        return True
    pe = str(meta.get("payment_evidence_status") or "").strip().lower()
    if pe in {
        "needs_confirmation",
        "pre_transfer_review",
        "amount_only_insufficient",
        "confirmed",
    }:
        kind = str(meta.get("pdf_kind") or meta.get("image_kind") or "").lower()
        if kind in {
            "payment_receipt",
            "payment_pre_review",
            "payment_pending_evidence",
        }:
            return True
    return False


def requires_merchant_verification_before_confirm(md: Optional[Dict[str, Any]]) -> bool:
    """E-wallet / partial receipts must stay pending until merchant verifies."""
    meta = dict(md or {})
    pe = str(meta.get("payment_evidence_status") or "").strip().lower()
    if pe == "amount_only_insufficient":
        return True
    blob = _norm_blob(
        str(meta.get("filename") or ""),
        str(meta.get("pdf_text_preview") or ""),
    )
    if _has_wallet_provider(blob) or "transfer to contact" in blob:
        return True
    receipt = meta.get("receipt_data")
    if isinstance(receipt, dict):
        if receipt.get("beneficiary_mobile") or receipt.get("receiver_mobile"):
            return True
        if receipt.get("customer_mobile") and not receipt.get("beneficiary_name"):
            return True
    fn = _norm_blob(str(meta.get("filename") or ""))
    return _has_wallet_provider(fn) and filename_suggests_transfer_receipt(
        str(meta.get("filename") or ""),
    )


def ensure_payment_pending_document_metadata(
    metadata: Dict[str, Any],
    *,
    text: Optional[str] = None,
) -> None:
    """Promote unknown PDFs with transfer signals to payment_pending_evidence."""
    if not metadata_has_potential_payment_document(metadata, text=text):
        return
    kind = str(metadata.get("pdf_kind") or "").strip().lower()
    if kind in {"payment_receipt", "payment_pre_review", "payment_pending_evidence"}:
        if kind == "payment_receipt" and requires_merchant_verification_before_confirm(metadata):
            metadata["pdf_kind"] = "payment_pending_evidence"
            metadata.setdefault("pdf_kind_reasons", [])
            if isinstance(metadata["pdf_kind_reasons"], list):
                metadata["pdf_kind_reasons"].append("ewallet_pending_merchant_verification")
        return
    metadata["pdf_kind"] = "payment_pending_evidence"
    metadata["pdf_kind_confidence"] = metadata.get("pdf_kind_confidence") or "medium"
    reasons = list(metadata.get("pdf_kind_reasons") or [])
    reasons.append("potential_payment_document_promoted")
    metadata["pdf_kind_reasons"] = reasons
    metadata["potential_payment_document"] = True
    pe = str(metadata.get("payment_evidence_status") or "").strip().lower()
    if not pe or pe == "not_payment":
        metadata["payment_evidence_status"] = "needs_confirmation"
        metadata["payment_evidence_reason"] = "potential_payment_document"


__all__ = [
    "ensure_payment_pending_document_metadata",
    "filename_suggests_transfer_receipt",
    "metadata_has_potential_payment_document",
    "metadata_has_receipt_amount",
    "requires_merchant_verification_before_confirm",
    "text_suggests_transfer_receipt",
]
