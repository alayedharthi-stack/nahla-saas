"""
Structured payment-evidence hints for inbound PDF/image media.

Extracts bank / amount / date / reference / sender from internal
OCR/vision text only. Never returns raw extraction blobs — callers
use this for metadata and safe merchant-facing hint lines.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from modules.ai.media.document_display import is_readable_document_summary

_PAYMENT_KINDS = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})
_PAYMENT_STATUSES = frozenset({
    "confirmed",
    "needs_confirmation",
    "pre_transfer_review",
})

_BANK_LABELS: tuple[tuple[str, str], ...] = (
    ("الراجحي", "مصرف الراجحي"),
    ("alrajhi", "Al Rajhi Bank"),
    ("rajhi", "Al Rajhi Bank"),
    ("الأهلي", "البنك الأهلي"),
    ("الاهلي", "البنك الأهلي"),
    ("alahli", "Al Ahli Bank"),
    ("الإنماء", "مصرف الإنماء"),
    ("الانماء", "مصرف الإنماء"),
    ("alinma", "Alinma Bank"),
    ("stc pay", "STC Pay"),
    ("stcpay", "STC Pay"),
    ("البلاد", "بنك البلاد"),
    ("albilad", "Bank AlBilad"),
    ("الرياض", "بنك الرياض"),
    ("ساب", "SABB"),
    ("sabb", "SABB"),
    ("العربي الوطني", "البنك العربي الوطني"),
    ("anb", "Arab National Bank"),
)

_AMOUNT_RES = (
    re.compile(
        r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*(?:ريال|SAR|sr|SR|ر\.س)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:المبلغ|مبلغ\s*التحويل|amount|transfer\s*amount)"
        r"[\s:]*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
)
_DATE_RES = (
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b"),
    re.compile(r"\b(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})\b"),
    re.compile(
        r"(\d{1,2}\s+(?:يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|"
        r"يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر)"
        r"\s+\d{4})",
        re.IGNORECASE,
    ),
)
_REF_RES = (
    re.compile(
        r"(?:رقم\s*(?:العملية|المرجع|المرجعي)|reference\s*(?:number|no)?|"
        r"transaction\s*(?:id|ref(?:erence)?))[\s:#]*([A-Za-z0-9\-]{6,32})",
        re.IGNORECASE,
    ),
    re.compile(r"\b(FT\d{8,})\b", re.IGNORECASE),
)
_SENDER_RES = (
    re.compile(
        r"(?:من|from|المحول|sender|beneficiary)[\s:]*([^\n]{3,80})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:إلى|to|المستفيد|beneficiary\s*name)[\s:]*([^\n]{3,80})",
        re.IGNORECASE,
    ),
)
_IBAN_RE = re.compile(r"\b(SA\s?\d{2}(?:\s?\d){20})\b", re.IGNORECASE)


def _first_match(patterns: tuple[re.Pattern[str], ...], blob: str) -> str:
    for pat in patterns:
        m = pat.search(blob)
        if m:
            return (m.group(1) or "").strip()
    return ""


def _safe_field(value: str, *, max_len: int = 80) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) > max_len:
        raw = raw[:max_len].rstrip() + "…"
    if not is_readable_document_summary(raw, max_len=max_len):
        return ""
    return raw


def _detect_bank(blob: str) -> str:
    lower = (blob or "").lower()
    for needle, label in _BANK_LABELS:
        if needle in lower:
            return label
    return ""


def extract_payment_evidence_hints(
    internal_text: str,
    base_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return structured payment hints from internal extraction text."""
    meta = dict(base_meta or {})
    kind = str(meta.get("pdf_kind") or meta.get("image_kind") or "")
    status = str(meta.get("payment_evidence_status") or "")
    if kind not in _PAYMENT_KINDS and status not in _PAYMENT_STATUSES:
        return {}

    blob = (internal_text or "").strip()
    if not blob:
        return {"payment_evidence_status": status} if status else {}

    hints: Dict[str, str] = {}
    if status:
        hints["payment_evidence_status"] = status

    bank = _detect_bank(blob)
    if bank:
        hints["bank_name"] = bank

    amount = _first_match(_AMOUNT_RES, blob)
    amount = _safe_field(amount.replace(",", ""), max_len=32)
    if amount:
        hints["amount"] = amount

    transfer_date = _safe_field(_first_match(_DATE_RES, blob), max_len=40)
    if transfer_date:
        hints["transfer_date"] = transfer_date

    reference = _safe_field(_first_match(_REF_RES, blob), max_len=40)
    if reference:
        hints["reference_number"] = reference

    sender = _safe_field(_first_match(_SENDER_RES, blob), max_len=60)
    if sender:
        hints["sender_name"] = sender

    iban_m = _IBAN_RE.search(blob)
    if iban_m:
        hints["iban_masked"] = iban_m.group(1).replace(" ", "")[:6] + "…"

    return hints


def attach_payment_evidence_hints(
    base_meta: Dict[str, Any],
    *,
    internal_text: str,
) -> None:
    """Populate ``payment_evidence_hints`` on inbound media metadata."""
    hints = extract_payment_evidence_hints(internal_text, base_meta)
    if hints:
        base_meta["payment_evidence_hints"] = hints


def safe_payment_hints_for_display(
    hints: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Sanitize hints for dashboard/API — structured fields only."""
    if not hints:
        return None
    out: Dict[str, str] = {}
    for key in (
        "payment_evidence_status",
        "bank_name",
        "amount",
        "transfer_date",
        "reference_number",
        "sender_name",
        "iban_masked",
    ):
        val = _safe_field(str(hints.get(key) or ""), max_len=80)
        if val:
            out[key] = val
    return out or None
