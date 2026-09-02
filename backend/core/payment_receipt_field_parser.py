"""
Deterministic labeled-field extraction for Saudi bank transfer PDF receipts.

Platform-wide — no LLM amount inference. Prefers explicit ``Amount: SAR …``
labels and excludes VAT/fee/charge/percentage lines.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.payment_receipt_field_parser")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Lines that must never supply the primary transfer amount.
# Note: ``Amount Transfer:`` is handled by a dedicated regex — do not
# treat the substring ``transfer amount`` as a fee/vat exclusion line.
_NON_TRANSFER_AMOUNT_LINE_RE = re.compile(
    r"(?:"
    r"vat\s*percentage|vat\s*amount|fee\s*amount|total\s*charge|"
    r"charge\s*amount|percentage\s*amount|"
    r"نسبة\s*ضريبة|مبلغ\s*ضريبة|إجمالي\s*الرسوم|"
    r"percentage\s*:?\s*\d+\s*%"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_AMOUNT_TRANSFER_RE = re.compile(
    r"(?:Amount\s*Transfer|مبلغ\s*التحويل)\s*:\s*"
    r"(?:SAR|SR|ريال|ر\.?\s*س)?\s*"
    r"(?P<value>\d{1,9}(?:[.,]\d{1,2})?)",
    re.IGNORECASE | re.UNICODE,
)

# Primary transfer amount — standalone Amount / المبلغ label only.
_PRIMARY_AMOUNT_LINE_RE = re.compile(
    r"(?P<label>^(?:Amount|المبلغ(?:\s*المحول)?)\s*:?)\s*"
    r"(?:SAR|SR|ريال|ر\.?\s*س)?\s*"
    r"(?P<value>\d{1,9}(?:[.,]\d{1,2})?)",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)

# Secondary: ``SAR 175`` on a non-excluded line near an Amount label.
_SAR_AMOUNT_RE = re.compile(
    r"(?:SAR|SR|ريال|ر\.?\s*س)\s*(?P<value>\d{1,9}(?:[.,]\d{1,2})?)"
    r"|(?P<value2>\d{1,9}(?:[.,]\d{1,2})?)\s*(?:SAR|SR|ريال|ر\.?\s*س)",
    re.IGNORECASE | re.UNICODE,
)

_REF_HASH_RE = re.compile(
    r"Reference\s*#\s*:\s*(?P<ref>[A-Za-z0-9]{10,48})",
    re.IGNORECASE,
)
_REF_LABEL_RE = re.compile(
    r"(?:"
    r"رقم\s*(?:العملية|المرجع|المرجعي|التحويل)"
    r"|reference\s*(?:number|no|#)?"
    r"|transaction\s*(?:id|ref(?:erence)?|number)"
    r"|number\s*transaction"
    r")"
    r"\s*[:#]\s*(?P<ref>[A-Za-z0-9][A-Za-z0-9\-]{4,47}[A-Za-z0-9])",
    re.IGNORECASE | re.UNICODE,
)
_REF_FT_RE = re.compile(r"\b(FT\d{8,})\b", re.IGNORECASE)

_DATE_RES = (
    re.compile(
        r"(?:Transaction\s*Date|تاريخ\s*(?:العملية|التحويل)?)\s*:\s*"
        r"(?P<d>\d{1,2}:\d{2}:\d{2}\s+\d{1,2}-\d{1,2}-\d{4})",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\b"),
    re.compile(r"\b(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\b"),
)

_FROM_ACCOUNT_RE = re.compile(
    r"From\s*Account\s*:\s*(?P<from_acc>[^\n\r]{4,80})",
    re.IGNORECASE,
)
_TO_ACCOUNT_RE = re.compile(
    r"Target\s*Account\s*:\s*(?P<to_acc>[^\n\r]{4,80})",
    re.IGNORECASE,
)
_BENEFICIARY_RE = re.compile(
    r"Beneficiary\s*:\s*(?P<name>[^\n\r]{3,80})",
    re.IGNORECASE | re.UNICODE,
)
_AR_BENEFICIARY_RE = re.compile(
    r"المستفيد\s*:\s*(?P<name>[^\n\r]{3,80})",
    re.UNICODE,
)
_RECEIVER_NAME_RE = re.compile(
    r"Receiver\s*Name\s*:\s*(?P<name>[^\n\r]{3,80})",
    re.IGNORECASE,
)
_RECEIVER_MOBILE_RE = re.compile(
    r"Receiver\s*:\s*(?P<mobile>\+?\d[\d\s\-]{8,20}\d)",
    re.IGNORECASE,
)
_CUSTOMER_MOBILE_RES = (
    re.compile(
        r"Customer\s*(?:Mobile|Phone)\s*:\s*(?P<mobile>\+?\d[\d\s\-]{8,20}\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"Mobile\s*(?:number|Number)\s*:\s*(?P<mobile>\+?\d[\d\s\-]{8,20}\d)",
        re.IGNORECASE,
    ),
)

_VAT_PCT_RE = re.compile(
    r"VAT\s*Percentage\s*:\s*(?P<pct>\d{1,2})\s*%",
    re.IGNORECASE,
)
_VAT_AMOUNT_RE = re.compile(
    r"VAT\s*Amount\s*:\s*(?:SAR|SR|ريال)?\s*(?P<val>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FEE_AMOUNT_RE = re.compile(
    r"Fee\s*Amount\s*:\s*(?:SAR|SR|ريال)?\s*(?P<val>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_TOTAL_CHARGE_RE = re.compile(
    r"Total\s*Charge\s*Amount\s*:\s*(?:SAR|SR|ريال)?\s*(?P<val>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BANK_TYPE_RE = re.compile(
    r"Bank\s*Name\s*/\s*Type\s*:\s*(?P<bank>[^\n\r]{3,120})",
    re.IGNORECASE | re.UNICODE,
)

_BANK_NEEDLES: tuple[tuple[str, str], ...] = (
    ("الراجحي", "مصرف الراجحي"),
    ("al rajhi", "Al Rajhi Bank"),
    ("rajhi", "Al Rajhi Bank"),
    ("الإنماء", "مصرف الإنماء"),
    ("alinma", "Alinma Bank"),
    ("الأهلي", "Al Ahli Bank"),
    ("alahli", "Al Ahli Bank"),
    ("mobilypay", "Mobily Pay"),
    ("mobily pay", "Mobily Pay"),
    ("stc pay", "STC Pay"),
    ("stcpay", "STC Pay"),
)

_AR_SENDER_RE = re.compile(
    r"(?:^من\s*:|^المحول\s*:|^Sender\s*:)\s*(?P<name>[^\n\r]{3,80})",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)

# Generic labeled transfer-text fields (SMS / app notification). Platform-wide
# — not a bank-brand parser. Requires explicit labels, not bare keywords.
_GENERIC_AMOUNT_LABEL_RE = re.compile(
    r"(?:المبلغ(?:\s*المحول)?|amount)\s*[:\-–]?\s*"
    r"(?:SAR|SR|USD|EUR|ريال|ر\.?\s*س)?\s*"
    r"(?P<value>\d{1,9}(?:[.,]\d{1,2})?)",
    re.IGNORECASE | re.UNICODE,
)
_GENERIC_FROM_RE = re.compile(
    r"(?:من(?:\s*(?:حساب|الحساب))?|from(?:\s*account)?)\s*[:\-–]\s*"
    r"(?P<acc>[^\n\r]{2,80})",
    re.IGNORECASE | re.UNICODE,
)
_GENERIC_TO_RE = re.compile(
    r"(?:(?:إلى|الى)(?:\s*(?:حساب|الحساب))?|to(?:\s*account)?|"
    r"target\s*account|beneficiary\s*account)\s*[:\-–]\s*(?P<acc>[^\n\r]{2,80})",
    re.IGNORECASE | re.UNICODE,
)
# Unlabeled SMS / app notifications: suffixes without colons.
# Platform-wide structural tokens (from/to/lam), not bank-branded.
_UNLABELED_FROM_RE = re.compile(
    r"(?:من|from)\s*(?P<acc>\d{3,6})",
    re.IGNORECASE | re.UNICODE,
)
_UNLABELED_TO_RE = re.compile(
    r"(?:لـ|ل|إلى|الى|to)\s*(?P<acc>\d{3,6})",
    re.IGNORECASE | re.UNICODE,
)
_UNLABELED_BENEFICIARY_RE = re.compile(
    r"(?:اسم\s+)?مستفيد\s+(?P<name>[^\d\n\r;؛:]{2,80})",
    re.UNICODE,
)
_CURRENCY_TOKEN_RE = re.compile(
    r"\b(SAR|SR|USD|EUR|ريال)\b",
    re.IGNORECASE | re.UNICODE,
)
_ACCOUNT_SUFFIX_RE = re.compile(
    r"(?:[*xX•●]+|x{2,}|آخر)\s*(?P<suf>\d{3,6})|(?P<tail>\d{4,6})\s*$",
)

_GENERIC_ACCOUNT_WORDS = frozenset({
    "account", "حساب", "from", "to", "target", "beneficiary", "reference",
})


@dataclass
class PaymentReceiptParsedFields:
    bank_name: str = ""
    bank_type_line: str = ""
    amount: str = ""
    currency: str = "SAR"
    vat_percentage: str = ""
    vat_amount: str = ""
    fee_amount: str = ""
    total_charge_amount: str = ""
    beneficiary_name: str = ""
    from_account_masked: str = ""
    to_account: str = ""
    transfer_datetime: str = ""
    reference_number: str = ""
    sender_person_name: str = ""
    receiver_mobile: str = ""
    beneficiary_mobile: str = ""
    customer_mobile: str = ""
    payer_mobile: str = ""
    amount_confidence: str = "low"
    source_account_suffix: str = ""
    dest_account_suffix: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _norm(text: str) -> str:
    if not text:
        return ""
    s = _NORM_RE.sub("", str(text).strip())
    return _WS_RE.sub(" ", s).strip()


def _clean_amount(value: str) -> str:
    raw = (value or "").strip().replace(",", "")
    if not raw:
        return ""
    try:
        num = float(raw)
    except ValueError:
        return raw
    if num == int(num):
        return str(int(num))
    return f"{num:.2f}".rstrip("0").rstrip(".")


def _line_is_non_transfer_amount(line: str) -> bool:
    return bool(_NON_TRANSFER_AMOUNT_LINE_RE.search(line or ""))


def _normalize_mobile(raw: str) -> str:
    compact = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if compact.startswith("966") and not compact.startswith("+"):
        compact = "+" + compact
    return compact[:20]


def _parse_primary_amount(blob: str) -> tuple[str, str]:
    """Return (amount, confidence)."""
    if not blob:
        return "", "absent"

    m = _AMOUNT_TRANSFER_RE.search(blob)
    if m:
        return _clean_amount(m.group("value")), "high"

    m = _PRIMARY_AMOUNT_LINE_RE.search(blob)
    if m:
        return _clean_amount(m.group("value")), "high"

    # Line scan: Amount label on its own line, not fee/vat/charge variants.
    for line in blob.splitlines():
        stripped = line.strip()
        if not stripped or _line_is_non_transfer_amount(stripped):
            continue
        if re.match(r"^Amount\s*:", stripped, re.IGNORECASE):
            sar_m = re.search(
                r"Amount\s*:\s*(?:SAR|SR|ريال|ر\.?\s*س)?\s*(\d{1,9}(?:[.,]\d{1,2})?)",
                stripped,
                re.IGNORECASE,
            )
            if sar_m:
                return _clean_amount(sar_m.group(1)), "high"

    best = ""
    best_val = -1.0
    for line in blob.splitlines():
        stripped = line.strip()
        if not stripped or _line_is_non_transfer_amount(stripped):
            continue
        if "%" in stripped and "amount" not in stripped.lower():
            continue
        for m in _SAR_AMOUNT_RE.finditer(stripped):
            val = m.group("value") or m.group("value2") or ""
            cleaned = _clean_amount(val)
            if not cleaned:
                continue
            try:
                num = float(cleaned)
            except ValueError:
                continue
            if num > best_val:
                best_val = num
                best = cleaned
    if best:
        return best, "medium"
    return "", "absent"


def _parse_reference(blob: str) -> str:
    if not blob:
        return ""
    m = _REF_HASH_RE.search(blob)
    if m:
        return (m.group("ref") or "").strip()
    m = _REF_LABEL_RE.search(blob)
    if m:
        ref = (m.group("ref") or "").strip()
        if ref.lower() not in {"reference", "ref", "number"}:
            return ref
    m = _REF_FT_RE.search(blob)
    if m:
        return m.group(1).strip()
    return ""


def _parse_datetime(blob: str) -> str:
    for pat in _DATE_RES:
        m = pat.search(blob or "")
        if m:
            return (m.group("d") if "d" in pat.groupindex else m.group(1)).strip()
    return ""


def _clean_person_name(raw: str) -> str:
    name = _norm(raw).split("\n")[0].strip()[:80]
    if not name:
        return ""
    lower = name.lower()
    if lower in _GENERIC_ACCOUNT_WORDS or lower.startswith("account"):
        return ""
    if re.fullmatch(r"[\W\d_]+", name):
        return ""
    return name


def _clean_from_account(raw: str) -> str:
    acc = (raw or "").strip().split("\n")[0].strip()[:80]
    if not acc:
        return ""
    lower = acc.lower().strip()
    if lower in _GENERIC_ACCOUNT_WORDS:
        return ""
    if re.match(r"^account\s*:?\s*$", lower):
        return ""
    if re.search(r"[xX*]{2,}", acc) or re.search(r"\d{4}", acc):
        return acc
    compact = re.sub(r"\s+", "", acc)
    if re.match(r"^SA\d{2}", compact, re.IGNORECASE):
        return compact.upper()
    return ""


def _detect_bank(blob: str) -> str:
    lower = (blob or "").lower()
    for needle, label in _BANK_NEEDLES:
        if needle in lower:
            return label
    return ""


def _account_suffix(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _ACCOUNT_SUFFIX_RE.search(text)
    if m:
        return str(m.group("suf") or m.group("tail") or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return digits[-4:]
    if len(digits) >= 3:
        return digits
    return ""


def parse_payment_receipt_fields(
    text: Optional[str],
    *,
    filename: Optional[str] = None,
) -> PaymentReceiptParsedFields:
    """Extract structured payment receipt fields from PDF/OCR text."""
    blob = (text or "").strip()
    if filename:
        blob = f"{blob}\n{filename}".strip()
    if not blob:
        return PaymentReceiptParsedFields()

    try:
        from core.arabic_ocr_normalization import normalize_arabic_presentation_forms  # noqa: PLC0415

        blob = normalize_arabic_presentation_forms(blob) or blob
    except Exception:
        logger.exception("[PAYMENT_RECEIPT_FIELD_PARSER] field_parse_failed")

    fields = PaymentReceiptParsedFields()

    amount, conf = _parse_primary_amount(blob)
    fields.amount = amount
    fields.amount_confidence = conf

    m = _VAT_PCT_RE.search(blob)
    if m:
        fields.vat_percentage = f"{m.group('pct')}%"

    m = _VAT_AMOUNT_RE.search(blob)
    if m:
        fields.vat_amount = _clean_amount(m.group("val"))

    m = _FEE_AMOUNT_RE.search(blob)
    if m:
        fields.fee_amount = _clean_amount(m.group("val"))

    m = _TOTAL_CHARGE_RE.search(blob)
    if m:
        fields.total_charge_amount = _clean_amount(m.group("val"))

    m = _BANK_TYPE_RE.search(blob)
    if m:
        fields.bank_type_line = m.group("bank").strip()
        fields.bank_name = fields.bank_type_line.split("/")[0].strip()

    if not fields.bank_name:
        fields.bank_name = _detect_bank(blob)

    m = _RECEIVER_NAME_RE.search(blob)
    if m:
        fields.beneficiary_name = _clean_person_name(m.group("name"))

    m = _BENEFICIARY_RE.search(blob) or _AR_BENEFICIARY_RE.search(blob)
    if m and not fields.beneficiary_name:
        fields.beneficiary_name = _clean_person_name(m.group("name"))

    m = _RECEIVER_MOBILE_RE.search(blob)
    if m:
        mobile = _normalize_mobile(m.group("mobile"))
        fields.receiver_mobile = mobile
        fields.beneficiary_mobile = mobile

    for pat in _CUSTOMER_MOBILE_RES:
        cm = pat.search(blob)
        if cm:
            fields.customer_mobile = _normalize_mobile(cm.group("mobile"))
            fields.payer_mobile = fields.customer_mobile
            break

    m = _FROM_ACCOUNT_RE.search(blob)
    if m:
        fields.from_account_masked = _clean_from_account(m.group("from_acc"))

    m = _AR_SENDER_RE.search(blob)
    if m:
        fields.sender_person_name = _clean_person_name(m.group("name"))

    m = _TO_ACCOUNT_RE.search(blob)
    if m:
        fields.to_account = re.sub(r"\s+", "", m.group("to_acc").strip())

    if not fields.amount:
        gm = _GENERIC_AMOUNT_LABEL_RE.search(blob)
        if gm:
            fields.amount = _clean_amount(gm.group("value"))
            if fields.amount:
                fields.amount_confidence = "medium"

    cur = _CURRENCY_TOKEN_RE.search(blob)
    if cur:
        token = str(cur.group(1) or "").upper()
        fields.currency = "SAR" if token in {"SR", "ريال"} else token

    if not fields.from_account_masked:
        gm = _GENERIC_FROM_RE.search(blob)
        if gm:
            fields.from_account_masked = _clean_from_account(gm.group("acc")) or gm.group("acc").strip()[:80]
    if not fields.to_account:
        gm = _GENERIC_TO_RE.search(blob)
        if gm:
            fields.to_account = re.sub(r"\s+", "", gm.group("acc").strip())

    if not fields.source_account_suffix:
        um = _UNLABELED_FROM_RE.search(blob)
        if um:
            suffix = str(um.group("acc") or "").strip()
            if suffix:
                fields.from_account_masked = fields.from_account_masked or suffix
                fields.source_account_suffix = suffix
    if not fields.dest_account_suffix:
        um = _UNLABELED_TO_RE.search(blob)
        if um:
            suffix = str(um.group("acc") or "").strip()
            if suffix:
                fields.to_account = fields.to_account or suffix
                fields.dest_account_suffix = suffix
    if not fields.beneficiary_name:
        um = _UNLABELED_BENEFICIARY_RE.search(blob)
        if um:
            fields.beneficiary_name = _clean_person_name(um.group("name"))

    fields.source_account_suffix = fields.source_account_suffix or _account_suffix(
        fields.from_account_masked
    )
    fields.dest_account_suffix = fields.dest_account_suffix or _account_suffix(
        fields.to_account
    )
    if not fields.dest_account_suffix:
        iban_m = re.search(r"\bSA\s*\d{2}(?:[\s\-]*\d){20}\b", blob, re.I)
        if iban_m:
            compact = re.sub(r"[\s\-]+", "", iban_m.group(0)).upper()
            fields.to_account = fields.to_account or compact
            fields.dest_account_suffix = compact[-4:]

    fields.reference_number = _parse_reference(blob)
    fields.transfer_datetime = _parse_datetime(blob)

    return fields


def parsed_fields_to_hints(fields: PaymentReceiptParsedFields) -> Dict[str, str]:
    """Map parsed fields to payment_evidence_hints keys."""
    hints: Dict[str, str] = {}
    if fields.bank_name:
        hints["bank_name"] = fields.bank_name
    if fields.bank_type_line and fields.bank_type_line != fields.bank_name:
        hints["bank_transfer_type"] = fields.bank_type_line
    if fields.amount:
        hints["amount"] = fields.amount
    if fields.transfer_datetime:
        hints["transfer_date"] = fields.transfer_datetime
    if fields.reference_number:
        hints["reference_number"] = fields.reference_number
    if fields.beneficiary_name:
        hints["beneficiary_name"] = fields.beneficiary_name
    if fields.receiver_mobile:
        hints["receiver_mobile"] = fields.receiver_mobile
        hints["beneficiary_mobile"] = fields.beneficiary_mobile
    if fields.customer_mobile:
        hints["customer_mobile"] = fields.customer_mobile
        hints["payer_mobile"] = fields.payer_mobile
    if fields.sender_person_name:
        hints["sender_name"] = fields.sender_person_name
    elif fields.from_account_masked:
        hints["from_account_masked"] = fields.from_account_masked
        hints["sender_name"] = fields.from_account_masked
    if fields.to_account:
        hints["to_account"] = fields.to_account
    if fields.vat_percentage:
        hints["vat_percentage"] = fields.vat_percentage
    if fields.vat_amount:
        hints["vat_amount"] = fields.vat_amount
    if fields.fee_amount:
        hints["fee_amount"] = fields.fee_amount
    if fields.total_charge_amount:
        hints["total_charge_amount"] = fields.total_charge_amount
    if fields.amount_confidence in {"high", "medium"}:
        hints["amount_parse_confidence"] = fields.amount_confidence
    if fields.currency:
        hints["currency"] = fields.currency
    if fields.source_account_suffix:
        hints["source_account_suffix"] = fields.source_account_suffix
    if fields.dest_account_suffix:
        hints["dest_account_suffix"] = fields.dest_account_suffix
    return hints


@dataclass(frozen=True)
class TransferTextEvidenceAssessment:
    sufficient: bool
    amount_only: bool
    review_state: str
    linkage_fields: Tuple[str, ...]
    fields: PaymentReceiptParsedFields


def assess_transfer_text_evidence(
    text: Optional[str],
    *,
    filename: Optional[str] = None,
) -> TransferTextEvidenceAssessment:
    """Classify structured transfer text as evidence vs amount-only.

    Sufficient merchant linkage requires at least one of destination
    suffix, beneficiary, reference, or IBAN — never amount alone.
    Never marks verified or settled.
    """
    fields = parse_payment_receipt_fields(text, filename=filename)
    linkage: List[str] = []
    if fields.dest_account_suffix or (fields.to_account and str(fields.to_account).upper().startswith("SA")):
        linkage.append("destination")
    if fields.beneficiary_name:
        linkage.append("beneficiary")
    if fields.reference_number:
        linkage.append("reference")
    amount_present = bool(fields.amount)
    extras = 0
    if fields.source_account_suffix:
        extras += 1
    if fields.transfer_datetime:
        extras += 1
    if fields.currency:
        extras += 1
    sufficient = bool(linkage) and (amount_present or extras >= 1)
    amount_only = bool(amount_present and not linkage)
    if sufficient:
        review = "pending_review"
    elif amount_only:
        review = "insufficient"
    else:
        review = "not_started"
    return TransferTextEvidenceAssessment(
        sufficient=sufficient,
        amount_only=amount_only,
        review_state=review,
        linkage_fields=tuple(linkage),
        fields=fields,
    )


__all__ = [
    "PaymentReceiptParsedFields",
    "TransferTextEvidenceAssessment",
    "assess_transfer_text_evidence",
    "parse_payment_receipt_fields",
    "parsed_fields_to_hints",
]
