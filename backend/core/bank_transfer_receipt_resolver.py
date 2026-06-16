"""
core/bank_transfer_receipt_resolver.py
──────────────────────────────────────
Evidence-based bank transfer receipt understanding.

Combines structured field extraction, tenant account matching,
customer confirmation boost, and receipt-type discrimination
(final receipt vs pre-transfer review screen).

Operational rule: never treat a completed Rajhi/AlAhli receipt
(with amount + beneficiary + merchant account match) as
``pre_transfer_review`` just because the OCR also captured a passive
header like ``تأكيد التحويل``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from core.payment_evidence import (
    PAYMENT_EVIDENCE_CONFIRMED,
    PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
    PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
    _body_has_pre_review_imperative,
    _normalise,
)
from core.tenant_payment_accounts import (
    TenantPaymentAccounts,
    extract_beneficiaries,
    extract_ibans,
    receipt_matches_tenant_accounts,
)

logger = logging.getLogger("nahla.bank_transfer_receipt_resolver")

PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
PAYMENT_EVIDENCE_RECEIVED = "PAYMENT_EVIDENCE_RECEIVED"
PAYMENT_PENDING_CONFIRMATION = "PAYMENT_PENDING_CONFIRMATION"
PAYMENT_PENDING_EVIDENCE = "PAYMENT_PENDING_EVIDENCE"
PAYMENT_REVIEW_REQUIRED = "PAYMENT_REVIEW_REQUIRED"

_RECEIPT_TYPE_FINAL = "final_receipt"
_RECEIPT_TYPE_PRE_REVIEW = "pre_transfer_review"
_RECEIPT_TYPE_UNCLEAR = "unclear"

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
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}(?:\s+\d{1,2}:\d{2})?)\b"),
    re.compile(r"\b(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}(?:\s+\d{1,2}:\d{2})?)\b"),
    re.compile(
        r"(\d{1,2}\s+(?:يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|"
        r"يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر)"
        r"\s+\d{4}(?:\s+\d{1,2}:\d{2})?)",
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
_BENEFICIARY_RES = (
    re.compile(
        r"(?:المستفيد|beneficiary(?:\s*name)?|to\s*account\s*holder|"
        r"transfer\s*to)[\s:]*([^\n\r]{3,80})",
        re.IGNORECASE,
    ),
)
_ACCOUNT_TAIL_RE = re.compile(r"(?:\d[\d\s\-]{3,}\d)")
_IBAN_RE = re.compile(r"\b(SA\s?\d{2}(?:\s?\d){20})\b", re.IGNORECASE)

_BANK_NEEDLES: tuple[tuple[str, str], ...] = (
    ("الراجحي", "Al Rajhi Bank"),
    ("al rajhi", "Al Rajhi Bank"),
    ("rajhi", "Al Rajhi Bank"),
    ("الأهلي", "Al Ahli Bank"),
    ("alahli", "Al Ahli Bank"),
    ("الإنماء", "Alinma Bank"),
    ("alinma", "Alinma Bank"),
    ("stc pay", "STC Pay"),
    ("stcpay", "STC Pay"),
)

_SUCCESS_MARKERS = (
    "transfer successful",
    "transaction successful",
    "successful transfer",
    "تم التحويل",
    "تمت العملية",
    "تم بنجاح",
    "ناجحة",
    "successful",
)


@dataclass
class BankReceiptExtraction:
    bank_name: str = ""
    amount: str = ""
    currency: str = "SAR"
    beneficiary_name: str = ""
    beneficiary_iban: str = ""
    account_last_digits: str = ""
    transfer_datetime: str = ""
    reference_number: str = ""
    receipt_type: str = _RECEIPT_TYPE_UNCLEAR
    has_pre_review_imperative: bool = False
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BankReceiptResolution:
    payment_state: str = PAYMENT_PENDING_CONFIRMATION
    extraction: BankReceiptExtraction = field(default_factory=BankReceiptExtraction)
    tenant_account_match: bool = False
    matched_iban: str = ""
    matched_beneficiary: str = ""
    customer_confirmation_boost: bool = False
    payment_evidence_status: str = PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
    reason: str = ""
    reply_ar: Optional[str] = None

    def to_metadata_patch(self) -> Dict[str, Any]:
        confidence = ""
        if self.payment_state == PAYMENT_RECEIVED:
            confidence = "high"
        elif self.payment_state == PAYMENT_EVIDENCE_RECEIVED:
            confidence = "medium" if not self.customer_confirmation_boost else "high"
        elif self.payment_state == PAYMENT_REVIEW_REQUIRED:
            confidence = "low"
        patch: Dict[str, Any] = {
            "payment_resolution_state": self.payment_state,
            "bank_receipt_extraction": self.extraction.to_dict(),
            "receipt_data": build_receipt_data(self.extraction),
            "payment_evidence_status": self.payment_evidence_status,
            "payment_resolution_reason": self.reason,
            "tenant_account_match": self.tenant_account_match,
            "matched_iban": self.matched_iban or None,
            "matched_beneficiary": self.matched_beneficiary or None,
            "customer_confirmation_boost": self.customer_confirmation_boost,
        }
        if confidence:
            patch["payment_evidence_confidence"] = confidence
        return patch


def build_receipt_data(extraction: BankReceiptExtraction) -> Dict[str, Any]:
    """Structured receipt fields for long-lived metadata / staff review."""
    return {
        "bank_name":           extraction.bank_name or None,
        "amount":              extraction.amount or None,
        "currency":            extraction.currency or "SAR",
        "beneficiary_name":    extraction.beneficiary_name or None,
        "beneficiary_iban":    extraction.beneficiary_iban or None,
        "account_last_digits": extraction.account_last_digits or None,
        "reference_number":    extraction.reference_number or None,
        "transfer_datetime":   extraction.transfer_datetime or None,
        "receipt_type":        extraction.receipt_type or None,
    }


def _first_match(patterns: tuple[re.Pattern[str], ...], blob: str) -> str:
    for pat in patterns:
        m = pat.search(blob or "")
        if m:
            return (m.group(1) or "").strip()
    return ""


def _detect_bank(blob: str) -> str:
    lower = (blob or "").lower()
    for needle, label in _BANK_NEEDLES:
        if needle in lower:
            return label
    return ""


def _normalise_blob(text: Optional[str]) -> str:
    if not text:
        return ""
    t = str(text).strip()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    return re.sub(r"\s+", " ", t)


def extract_bank_receipt_fields(
    text: Optional[str],
    *,
    filename: Optional[str] = None,
) -> BankReceiptExtraction:
    """Structured extraction from OCR / vision / PDF text."""
    blob = _normalise_blob(text)
    if filename:
        blob = f"{blob}\n{filename}".strip()

    ext = BankReceiptExtraction()
    if not blob:
        return ext

    ext.bank_name = _detect_bank(blob)
    raw_amount = _first_match(_AMOUNT_RES, blob)
    ext.amount = raw_amount.replace(",", "") if raw_amount else ""
    ext.currency = "SAR"
    ext.transfer_datetime = _first_match(_DATE_RES, blob)
    ext.reference_number = _first_match(_REF_RES, blob)

    benef = _first_match(_BENEFICIARY_RES, blob)
    if benef:
        ext.beneficiary_name = benef.split("\n")[0].strip()[:80]

    ibans = extract_ibans(blob)
    if ibans:
        ext.beneficiary_iban = ibans[0]
    else:
        iban_m = _IBAN_RE.search(blob)
        if iban_m:
            ext.beneficiary_iban = re.sub(r"\s+", "", iban_m.group(1)).upper()

    if not ext.beneficiary_name:
        benefs = extract_beneficiaries(blob)
        if benefs:
            ext.beneficiary_name = benefs[0]

    lower = blob.lower()
    for m in _ACCOUNT_TAIL_RE.finditer(blob):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 4:
            ext.account_last_digits = digits[-4:]
            break

    norm = _normalise(blob)
    ext.has_pre_review_imperative = _body_has_pre_review_imperative(norm)

    score = 0.0
    if ext.bank_name:
        score += 0.15
    if ext.amount:
        score += 0.25
    if ext.beneficiary_name or ext.beneficiary_iban:
        score += 0.2
    if ext.transfer_datetime:
        score += 0.15
    if ext.reference_number:
        score += 0.15
    if any(m in lower for m in _SUCCESS_MARKERS):
        score += 0.1

    has_completion_signal = bool(
        ext.reference_number
        or ext.transfer_datetime
        or any(m in lower for m in _SUCCESS_MARKERS)
    )

    if ext.has_pre_review_imperative and not ext.reference_number:
        ext.receipt_type = _RECEIPT_TYPE_PRE_REVIEW
        ext.confidence = min(score, 0.45)
    elif (
        score >= 0.55
        and not ext.has_pre_review_imperative
        and has_completion_signal
    ):
        ext.receipt_type = _RECEIPT_TYPE_FINAL
        ext.confidence = min(0.95, score + 0.1)
    elif score >= 0.4:
        ext.receipt_type = _RECEIPT_TYPE_UNCLEAR
        ext.confidence = score
    else:
        ext.receipt_type = _RECEIPT_TYPE_UNCLEAR
        ext.confidence = score

    return ext


def compose_payment_received_reply(amount: str, *, currency: str = "SAR") -> str:
    amt = (amount or "").strip()
    if amt:
        unit = "ريال" if currency.upper() == "SAR" else currency
        return (
            f"وصل إشعار التحويل بمبلغ {amt} {unit}، وتم تسجيله. "
            "راح نجهز طلبك ونرسل لك تفاصيل الشحن قريب."
        )
    return (
        "وصل إشعار التحويل وتم تسجيله. "
        "راح نجهز طلبك ونرسل لك تفاصيل الشحن قريب."
    )


def compose_payment_evidence_received_reply(amount: str, *, currency: str = "SAR") -> str:
    amt = (amount or "").strip()
    if amt:
        unit = "ريال" if currency.upper() == "SAR" else currency
        return f"وصلني إيصال التحويل بمبلغ {amt} {unit}، جاري مراجعته وتأكيده."
    return "وصلني إيصال التحويل، جاري مراجعته وتأكيده."


def compose_payment_review_required_reply(amount: str = "") -> str:
    if amount:
        return (
            f"وصلني إيصال بمبلغ {amount} ريال، لكن بيانات المستفيد لا تطابق "
            "حساب المتجر المسجل. راح نراجعه ونرجع لك."
        )
    return (
        "وصلني إيصال التحويل، لكن بيانات المستفيد لا تطابق حساب المتجر "
        "المسجل. راح نراجعه ونرجع لك."
    )


def resolve_bank_transfer_receipt(
    text: Optional[str],
    *,
    tenant_accounts: Optional[TenantPaymentAccounts] = None,
    filename: Optional[str] = None,
    customer_confirmation: bool = False,
    legacy_pe_status: Optional[str] = None,
) -> BankReceiptResolution:
    """
    Evidence-based payment resolution for one inbound blob (+ optional
    customer confirmation boost from a follow-up text message).
    """
    accounts = tenant_accounts or TenantPaymentAccounts()
    extraction = extract_bank_receipt_fields(text, filename=filename)
    match = receipt_matches_tenant_accounts(
        accounts=accounts,
        receipt_text=text,
    )
    tenant_match = match.get("status") == "match"
    matched_iban = str(match.get("matched_iban") or "")
    matched_beneficiary = str(match.get("matched_beneficiary") or "")

    res = BankReceiptResolution(
        extraction=extraction,
        tenant_account_match=tenant_match,
        matched_iban=matched_iban,
        matched_beneficiary=matched_beneficiary,
        customer_confirmation_boost=bool(customer_confirmation),
    )

    has_accounts = accounts.has_accounts
    imperative = extraction.has_pre_review_imperative

    # Explicit pre-transfer screen (imperative CTA, no reference, no success).
    if (
        extraction.receipt_type == _RECEIPT_TYPE_PRE_REVIEW
        or (
            imperative
            and not extraction.reference_number
            and legacy_pe_status == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        )
    ) and not customer_confirmation:
        res.payment_state = PAYMENT_PENDING_CONFIRMATION
        res.payment_evidence_status = PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        res.reason = "pre_transfer_screen"
        return res

    if has_accounts and match.get("status") == "mismatch":
        res.payment_state = PAYMENT_REVIEW_REQUIRED
        res.payment_evidence_status = PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        res.reason = "beneficiary_or_iban_mismatch"
        res.reply_ar = compose_payment_review_required_reply(extraction.amount)
        return res

    high_final = (
        extraction.receipt_type == _RECEIPT_TYPE_FINAL
        or (
            extraction.amount
            and (extraction.beneficiary_name or extraction.beneficiary_iban)
            and extraction.bank_name
            and not imperative
            and (
                extraction.reference_number
                or extraction.transfer_datetime
            )
        )
    )

    if tenant_match and high_final:
        # High-confidence receipt alone, OR evidence + customer confirmation.
        if customer_confirmation:
            res.payment_state = PAYMENT_RECEIVED
            res.payment_evidence_status = PAYMENT_EVIDENCE_CONFIRMED
            res.reason = "tenant_match_with_confirmation"
            res.reply_ar = compose_payment_received_reply(
                extraction.amount, currency=extraction.currency,
            )
            return res
        if extraction.confidence >= 0.75:
            res.payment_state = PAYMENT_RECEIVED
            res.payment_evidence_status = PAYMENT_EVIDENCE_CONFIRMED
            res.reason = "tenant_match_final_receipt"
            res.reply_ar = compose_payment_received_reply(
                extraction.amount, currency=extraction.currency,
            )
            return res

    if tenant_match and extraction.amount:
        res.payment_state = PAYMENT_EVIDENCE_RECEIVED
        res.payment_evidence_status = PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        res.reason = "tenant_match_medium_confidence"
        res.reply_ar = compose_payment_evidence_received_reply(
            extraction.amount, currency=extraction.currency,
        )
        return res

    if not has_accounts and high_final:
        res.payment_state = PAYMENT_RECEIVED
        res.payment_evidence_status = PAYMENT_EVIDENCE_CONFIRMED
        res.reason = "final_receipt_no_tenant_accounts"
        res.reply_ar = compose_payment_received_reply(
            extraction.amount, currency=extraction.currency,
        )
        return res

    if (
        customer_confirmation
        and extraction.amount
        and not imperative
        and tenant_match
    ):
        if high_final:
            res.payment_state = PAYMENT_RECEIVED
            res.payment_evidence_status = PAYMENT_EVIDENCE_CONFIRMED
            res.reason = "customer_confirmation_with_prior_receipt_context"
            res.reply_ar = compose_payment_received_reply(
                extraction.amount, currency=extraction.currency,
            )
        else:
            res.payment_state = PAYMENT_EVIDENCE_RECEIVED
            res.payment_evidence_status = PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
            res.reason = "customer_confirmation_medium_receipt_context"
            res.reply_ar = compose_payment_evidence_received_reply(
                extraction.amount, currency=extraction.currency,
            )
        return res

    if legacy_pe_status == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW and not customer_confirmation:
        res.payment_state = PAYMENT_PENDING_CONFIRMATION
        res.payment_evidence_status = PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        res.reason = "legacy_pre_transfer"
        return res

    res.payment_state = PAYMENT_EVIDENCE_RECEIVED if extraction.amount else PAYMENT_PENDING_CONFIRMATION
    res.payment_evidence_status = (
        PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        if res.payment_state != PAYMENT_RECEIVED
        else PAYMENT_EVIDENCE_CONFIRMED
    )
    res.reason = "unclear_receipt"
    if extraction.amount:
        res.reply_ar = compose_payment_evidence_received_reply(extraction.amount)
    return res


def apply_resolution_to_metadata(
    metadata: Dict[str, Any],
    resolution: BankReceiptResolution,
) -> None:
    """Mutate inbound metadata in place when resolver upgrades evidence."""
    metadata.update(resolution.to_metadata_patch())
    hints = dict(metadata.get("payment_evidence_hints") or {})
    ext = resolution.extraction
    receipt_data = build_receipt_data(ext)
    metadata["receipt_data"] = receipt_data
    if ext.bank_name:
        hints["bank_name"] = ext.bank_name
    if ext.amount:
        hints["amount"] = ext.amount
    if ext.beneficiary_name:
        hints["beneficiary_name"] = ext.beneficiary_name
    if ext.beneficiary_iban:
        hints["beneficiary_iban"] = ext.beneficiary_iban
    if ext.transfer_datetime:
        hints["transfer_datetime"] = ext.transfer_datetime
    if ext.reference_number:
        hints["reference_number"] = ext.reference_number
    if hints:
        metadata["payment_evidence_hints"] = hints

    if resolution.payment_evidence_status == PAYMENT_EVIDENCE_CONFIRMED:
        metadata["image_kind"] = metadata.get("image_kind") or "payment_receipt"
        metadata["pdf_kind"] = metadata.get("pdf_kind") or "payment_receipt"
