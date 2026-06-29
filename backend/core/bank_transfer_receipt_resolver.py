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
    PAYMENT_EVIDENCE_AMOUNT_ONLY_INSUFFICIENT,
    PAYMENT_EVIDENCE_CONFIRMED,
    PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
    PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
    _body_has_pre_review_imperative,
    _filename_signals_receipt,
    _filename_signals_statement,
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
    from_account_masked: str = ""
    to_account: str = ""
    bank_type_line: str = ""
    vat_percentage: str = ""
    vat_amount: str = ""
    fee_amount: str = ""
    total_charge_amount: str = ""
    amount_parse_confidence: str = ""

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
        "from_account_masked": extraction.from_account_masked or None,
        "to_account":          extraction.to_account or None,
        "reference_number":    extraction.reference_number or None,
        "transfer_datetime":   extraction.transfer_datetime or None,
        "receipt_type":        extraction.receipt_type or None,
        "bank_transfer_type":  extraction.bank_type_line or None,
        "vat_percentage":      extraction.vat_percentage or None,
        "vat_amount":          extraction.vat_amount or None,
        "fee_amount":          extraction.fee_amount or None,
        "total_charge_amount": extraction.total_charge_amount or None,
        "amount_parse_confidence": extraction.amount_parse_confidence or None,
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
    try:
        from core.arabic_ocr_normalization import normalize_arabic_presentation_forms  # noqa: PLC0415

        t = normalize_arabic_presentation_forms(text)
    except Exception:  # noqa: BLE001
        t = str(text).strip()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
    return re.sub(r"\s+", " ", t)


def extract_bank_receipt_fields(
    text: Optional[str],
    *,
    filename: Optional[str] = None,
) -> BankReceiptExtraction:
    """Structured extraction from OCR / vision / PDF text."""
    from core.payment_receipt_field_parser import parse_payment_receipt_fields  # noqa: PLC0415

    parsed = parse_payment_receipt_fields(text, filename=filename)
    blob = _normalise_blob(text)
    if filename:
        blob = f"{blob}\n{filename}".strip()

    ext = BankReceiptExtraction()
    if not blob and not parsed.amount:
        return ext

    ext.bank_name = parsed.bank_name or _detect_bank(blob)
    ext.amount = parsed.amount
    ext.currency = parsed.currency or "SAR"
    ext.transfer_datetime = parsed.transfer_datetime
    ext.reference_number = parsed.reference_number
    ext.beneficiary_name = parsed.beneficiary_name
    ext.from_account_masked = parsed.from_account_masked
    ext.to_account = parsed.to_account
    ext.bank_type_line = parsed.bank_type_line
    ext.vat_percentage = parsed.vat_percentage
    ext.vat_amount = parsed.vat_amount
    ext.fee_amount = parsed.fee_amount
    ext.total_charge_amount = parsed.total_charge_amount
    ext.amount_parse_confidence = parsed.amount_confidence

    ibans = extract_ibans(blob)
    if ibans:
        ext.beneficiary_iban = ibans[0]
    elif parsed.to_account.startswith("SA"):
        ext.beneficiary_iban = parsed.to_account
    else:
        iban_m = _IBAN_RE.search(blob)
        if iban_m:
            ext.beneficiary_iban = re.sub(r"\s+", "", iban_m.group(1)).upper()

    if parsed.from_account_masked:
        digits = re.sub(r"\D", "", parsed.from_account_masked)
        if len(digits) >= 4:
            ext.account_last_digits = digits[-4:]

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

    fname_receipt = _filename_signals_receipt(filename)
    fname_statement = _filename_signals_statement(filename)

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

    has_completion_signal = any(m in lower for m in _SUCCESS_MARKERS) or (
        not fname_statement
        and fname_receipt
        and ext.reference_number
        and ext.amount
        and (ext.beneficiary_name or ext.beneficiary_iban)
        and ext.bank_name
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

    res.payment_state = PAYMENT_PENDING_CONFIRMATION
    if extraction.amount and not (
        extraction.beneficiary_name
        or extraction.beneficiary_iban
        or extraction.reference_number
    ):
        res.payment_evidence_status = PAYMENT_EVIDENCE_AMOUNT_ONLY_INSUFFICIENT
        res.reason = "amount_without_transfer_linkage"
    else:
        res.payment_evidence_status = PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        res.reason = "unclear_receipt"
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
